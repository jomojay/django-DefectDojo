"""
Just-in-time provisioning pipeline steps for OIDC single sign-on.

These steps are wired into ``SOCIAL_AUTH_PIPELINE`` (see ``dojo/settings/settings.dist.py``) and
only run when SSO is enabled for the deployment. They exist to make three guarantees that the
stock ``social_core`` pipeline does not make on its own:

1. an identity may only sign in from an email domain the operator explicitly allowed;
2. an SSO identity is linked to a pre-existing local account only when the address it claims is
   provably owned by the directory, never on the strength of a self-asserted ``email`` claim;
3. an account created on first sign-in carries no privilege at all - an administrator grants
   access afterwards through the existing Authorized Users panels.

Every value these steps read (allowed domains, tenant) is per-deployment runtime configuration.
Nothing here is specific to any single organization.
"""

import logging

from social_core.exceptions import AuthFailed, AuthForbidden

logger = logging.getLogger(__name__)

# Claims the identity provider populates from the directory itself. Entra's generic "email" claim
# is sourced from the user's mail attribute and, for guest/external identities, is self-asserted
# and unverified - it is not safe to key an account association on. The claims below are owned by
# the tenant directory and cannot be chosen by the signing-in user.
DIRECTORY_OWNED_EMAIL_CLAIMS = ("upn", "preferred_username")

# Claim carrying an explicit verification signal. Entra does not emit it today, but other OIDC
# providers (and future Entra optional claims) do, so honour it when it is present.
EMAIL_VERIFIED_CLAIM = "email_verified"

_TRUTHY_CLAIM_VALUES = frozenset({"1", "true", "yes"})


def _normalize_email(value):
    """Return a lowercased, stripped address, or an empty string when there is nothing usable."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _email_domain(email):
    """Return the domain part of an address, or None when the address is not a single-@ address."""
    if email.count("@") != 1:
        return None
    domain = email.rsplit("@", 1)[1]
    return domain or None


def _allowed_domains(backend):
    """
    Return the configured allow-list of email domains, lowercased.

    Resolves ``SOCIAL_AUTH_<BACKEND>_WHITELISTED_DOMAINS`` and falls back to the backend-agnostic
    ``SOCIAL_AUTH_WHITELISTED_DOMAINS``, matching what ``social_core``'s own ``auth_allowed`` step
    consumes. An empty list means the operator applied no domain restriction.
    """
    configured = backend.setting("WHITELISTED_DOMAINS", []) or []
    return {_normalize_email(domain) for domain in configured if _normalize_email(domain)}


def _claim_is_truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_CLAIM_VALUES
    return bool(value)


def _email_is_directory_owned(email, response):
    """
    Return True when the address can be trusted as belonging to the signing-in principal.

    Trust comes from one of two places: an explicit ``email_verified`` claim, or the address
    matching one of the directory-owned claims the provider issues for the account itself.
    """
    if response is None:
        return False

    verified_claim = response.get(EMAIL_VERIFIED_CLAIM)
    if verified_claim is not None:
        return _claim_is_truthy(verified_claim)

    return any(
        _normalize_email(response.get(claim)) == email
        for claim in DIRECTORY_OWNED_EMAIL_CLAIMS
        if response.get(claim)
    )


def _local_accounts_claiming(storage, email):
    """
    Return every local account carrying this address, whether or not it is active.

    ``storage.get_users_by_email()`` goes through social_django's ``filter_active_users()`` and so
    only ever sees ``is_active=True`` rows. The username-uniqueness check that
    ``social_core.pipeline.user.get_username`` performs later
    (``storage.user_exists(username=...)`` -> ``filter_users()``) is *not* active-filtered. A
    deactivated account is therefore invisible to the link/collision decision while still colliding
    at creation time, and ``get_username`` resolves that collision by appending a uuid - which would
    hand a deactivated person a brand new, active, suffixed account instead of refusing the login.
    """
    user_model = storage.user_model()
    email_field = getattr(user_model, "EMAIL_FIELD", "email")
    return list(user_model._default_manager.filter(**{f"{email_field}__iexact": email}))


def enforce_whitelisted_domain(backend, details, **kwargs):
    """
    Reject the authentication unless the claimed address sits in the configured domain allow-list.

    ``social_core.pipeline.social_auth.auth_allowed`` already applies the same allow-list, but it
    silently permits an identity that carries no email at all. DefectDojo cannot provision or
    identify such a user, so this step additionally requires a syntactically usable address.
    """
    email = _normalize_email(details.get("email"))
    if not email:
        logger.warning(
            "SSO login refused for backend %s: the identity provider returned no email address",
            backend.name,
        )
        raise AuthForbidden(backend)

    domain = _email_domain(email)
    if domain is None:
        logger.warning(
            "SSO login refused for backend %s: %s is not a usable email address",
            backend.name, email,
        )
        raise AuthForbidden(backend)

    allowed = _allowed_domains(backend)
    if not allowed:
        # Not fatal: a single-tenant app registration already constrains who can obtain a token,
        # and the backend validates the tid claim against the configured tenant. It does mean
        # account linking stays disabled - see associate_by_verified_email.
        logger.warning(
            "SSO backend %s has no email domain allow-list configured; "
            "relying on the identity provider's tenant restriction alone",
            backend.name,
        )
        return

    if domain not in allowed:
        logger.warning(
            "SSO login refused for backend %s: domain %s is not in the configured allow-list",
            backend.name, domain,
        )
        raise AuthForbidden(backend)


def associate_by_verified_email(backend, details, response=None, user=None, **kwargs):
    """
    Link an SSO identity to an existing local account, but only when that is provably safe.

    This is the hardened replacement for ``social_core.pipeline.social_auth.associate_by_email``,
    which links on a bare email match and is therefore an account-takeover vector whenever the
    provider does not verify addresses. Linking here requires all of:

    * an explicit domain allow-list to be configured (an operator opt-in to linking at all),
    * the address to sit inside that allow-list,
    * the address to be verified, or to match a directory-owned claim (see
      ``_email_is_directory_owned``),
    * exactly one matching local account,
    * that account to be active,
    * that account to not already be bound to a different identity on this backend.

    When no local account matches, the step does nothing and ``create_user`` provisions a fresh,
    zero-privilege account. When a local account matches but any condition above fails, the login
    is refused rather than silently creating a second account shadowing the first.
    """
    if user:
        # social_user already resolved the identity through the stored uid association.
        return None

    email = _normalize_email(details.get("email"))
    if not email:
        return None

    storage = backend.strategy.storage.user
    candidates = _local_accounts_claiming(storage, email)
    if not candidates:
        # Nothing claims the address, so create_user may provision a fresh account - as long as the
        # username it will derive is actually free. With USERNAME_IS_FULL_EMAIL that username *is*
        # the address, and get_username resolves a collision by appending a uuid instead of
        # reporting it, which would produce exactly the shadow account this step exists to prevent.
        if backend.setting("USERNAME_IS_FULL_EMAIL", default=False) and storage.user_exists(username=email):
            logger.warning(
                "SSO login refused for backend %s: %s does not match any local account by email but "
                "an existing local account already holds that username",
                backend.name, email,
            )
            raise AuthForbidden(backend)
        return None

    if any(not candidate.is_active for candidate in candidates):
        # Deactivation is how DefectDojo revokes access, so this is a revoked account. Falling
        # through to create_user would re-admit the person under a uuid-suffixed username, because
        # the username collision check downstream is not active-filtered even though the email
        # lookup is. Refuse instead; an administrator reactivates the account if that is intended.
        logger.warning(
            "SSO login refused for backend %s: %s belongs to a deactivated local account",
            backend.name, email,
        )
        raise AuthForbidden(backend)

    allowed = _allowed_domains(backend)
    if not allowed:
        logger.warning(
            "SSO login refused for backend %s: %s matches an existing local account but no email "
            "domain allow-list is configured, so account linking is disabled",
            backend.name, email,
        )
        raise AuthForbidden(backend)

    if _email_domain(email) not in allowed:
        logger.warning(
            "SSO login refused for backend %s: %s matches an existing local account but its domain "
            "is not in the configured allow-list",
            backend.name, email,
        )
        raise AuthForbidden(backend)

    if not _email_is_directory_owned(email, response):
        logger.warning(
            "SSO login refused for backend %s: %s matches an existing local account but the "
            "identity provider did not assert ownership of that address",
            backend.name, email,
        )
        raise AuthForbidden(backend)

    if len(candidates) > 1:
        logger.warning(
            "SSO login refused for backend %s: %s matches more than one local account",
            backend.name, email,
        )
        msg = "The given email address is associated with more than one account"
        raise AuthFailed(backend, msg)

    matched = candidates[0]
    if storage.get_social_auth_for_user(matched, provider=backend.name).exists():
        # The account is already bound to a different subject on this backend. Adding a second
        # binding would let two distinct directory identities drive one DefectDojo account.
        logger.warning(
            "SSO login refused for backend %s: local account %s is already linked to a different "
            "identity on this backend",
            backend.name, matched.username,
        )
        raise AuthForbidden(backend)

    logger.info(
        "SSO backend %s linked identity %s to existing local account %s",
        backend.name, email, matched.username,
    )
    return {"user": matched, "is_new": False}


def enforce_zero_privilege_defaults(backend, user=None, *, is_new=False, **kwargs):
    """
    Strip every privilege from an account provisioned on first sign-in.

    Open-source DefectDojo authorizes on ``is_superuser`` / ``is_staff`` plus per-Product and
    per-Product Type ``authorized_users`` membership. A just-in-time provisioned user gets none of
    them: an administrator grants access afterwards through the Authorized Users panels. Django's
    ``create_user`` already defaults these flags to False; this step makes that an invariant of the
    pipeline rather than an implementation detail, and produces the audit record for the event.
    """
    if user is None or not is_new:
        return

    changed_fields = []
    if user.is_superuser:
        user.is_superuser = False
        changed_fields.append("is_superuser")
    if user.is_staff:
        user.is_staff = False
        changed_fields.append("is_staff")

    if changed_fields:
        user.save(update_fields=changed_fields)
        logger.warning(
            "SSO backend %s provisioned %s with elevated flags %s; they were reset",
            backend.name, user.username, ", ".join(changed_fields),
        )

    logger.info(
        "SSO backend %s provisioned new user %s with no privileges; "
        "grant access through the Authorized Users panels",
        backend.name, user.username,
    )
