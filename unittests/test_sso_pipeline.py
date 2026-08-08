"""
Unit tests for the just-in-time SSO provisioning pipeline (dojo/user/social_pipeline.py).

Everything the identity provider would supply is fed in as already-decoded claims: no HTTP call is
made to any IdP and none is mocked out at the transport level, because these steps run strictly
after social-auth has exchanged and validated the token. The authorization-code redirect, PKCE,
consent and reply-URL correctness are not unit-testable and are verified against a real tenant.
"""

import re

from social_core.exceptions import AuthFailed, AuthForbidden
from social_core.pipeline.user import create_user, get_username, user_details

from dojo.models import Product, Product_Type, User
from dojo.user.social_pipeline import (
    associate_by_verified_email,
    enforce_whitelisted_domain,
    enforce_zero_privilege_defaults,
)

from .dojo_test_case import DojoTestCase

# Mirrors social_core.storage.UserMixin.clean_username so the fake storage below behaves like the
# real social_django storage rather than like a permissive stub.
NO_SPECIAL_REGEX = re.compile(r"[^\w.@+_-]+", re.UNICODE)

ALLOWED_DOMAIN = "allowed.example"
OTHER_DOMAIN = "other.example"


class _FakeSocialLinks:

    """Stand-in for the UserSocialAuth queryset returned by get_social_auth_for_user()."""

    def __init__(self, *, present):
        self._present = present

    def exists(self):
        return self._present


class _FakeUserStorage:

    """
    Test double for social_django.storage.DjangoUserMixin.

    Every method that touches users delegates to the real Django user model, so the association and
    creation logic is exercised against real rows. Only the UserSocialAuth table is faked - that
    table only exists when social_django is in INSTALLED_APPS, which it is not while SSO is off.
    """

    def __init__(self, linked_usernames=()):
        self.linked_usernames = set(linked_usernames)

    def user_model(self):
        return User

    def username_max_length(self):
        return User._meta.get_field("username").max_length

    def clean_username(self, value):
        return NO_SPECIAL_REGEX.sub("", value)

    def user_exists(self, **kwargs):
        return User.objects.filter(**kwargs).exists()

    def get_username(self, user):
        return user.username

    def get_users_by_email(self, email):
        return User.objects.filter(is_active=True, email__iexact=email)

    def get_social_auth_for_user(self, user, provider=None):
        return _FakeSocialLinks(present=user.username in self.linked_usernames)

    def changed(self, user):
        user.save()


class _FakeStorage:

    """Stand-in for social_django.storage.DjangoStorage: a namespace holding the user storage."""

    def __init__(self, user_storage):
        self.user = user_storage


class _FakeStrategy:
    def __init__(self, user_storage, social_settings):
        self.storage = _FakeStorage(user_storage)
        self._settings = social_settings

    def setting(self, name, default=None, backend=None):
        return self._settings.get(name, default)

    def create_user(self, **fields):
        return User.objects.create_user(**fields)


class _FakeAzureBackend:

    """
    Minimal stand-in for social_core.backends.azuread_tenant.AzureADTenantOAuth2.

    Only the surface the pipeline steps actually use is implemented: the backend name, the
    settings lookup and the storage handle.
    """

    name = "azuread-tenant-oauth2"

    def __init__(self, whitelisted_domains=(), linked_usernames=(), **extra_settings):
        self._settings = {
            "WHITELISTED_DOMAINS": [domain.lower() for domain in whitelisted_domains],
            "USERNAME_IS_FULL_EMAIL": True,
            "FORCE_EMAIL_LOWERCASE": True,
            **extra_settings,
        }
        self.strategy = _FakeStrategy(_FakeUserStorage(linked_usernames), self._settings)

    def setting(self, name, default=None):
        return self._settings.get(name, default)


def entra_claims(email, *, upn=None, email_verified=None, name="Sample User"):
    """
    Build the decoded id_token claim set an Entra ID sign-in produces.

    ``upn`` defaults to ``email`` because that is the normal case for a member of the tenant. Pass
    a different ``upn`` to model a guest/external identity whose ``email`` claim is self-asserted.
    """
    given_name, _, family_name = name.partition(" ")
    claims = {
        "sub": "00000000-0000-0000-0000-00000000abcd",
        "oid": "00000000-0000-0000-0000-00000000abcd",
        "tid": "11111111-1111-1111-1111-111111111111",
        "name": name,
        "given_name": given_name,
        "family_name": family_name,
        "email": email,
        "upn": email if upn is None else upn,
    }
    if email_verified is not None:
        claims["email_verified"] = email_verified
    return claims


def details_from(claims):
    """Mirror AzureADOAuth2.get_user_details() so the pipeline receives the same shape it would live."""
    fullname = claims.get("name", "")
    return {
        "username": fullname,
        "email": claims.get("email", claims.get("upn")),
        "fullname": fullname,
        "first_name": claims.get("given_name", ""),
        "last_name": claims.get("family_name", ""),
    }


class TestEnforceWhitelistedDomain(DojoTestCase):

    def test_allows_address_inside_the_allow_list(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        details = details_from(entra_claims(f"person@{ALLOWED_DOMAIN}"))

        self.assertIsNone(enforce_whitelisted_domain(backend, details))

    def test_allows_address_regardless_of_case_and_whitespace(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN.upper()])
        details = details_from(entra_claims(f"  Person@{ALLOWED_DOMAIN.upper()}  "))

        self.assertIsNone(enforce_whitelisted_domain(backend, details))

    def test_rejects_address_outside_the_allow_list(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        details = details_from(entra_claims(f"person@{OTHER_DOMAIN}"))

        with self.assertRaises(AuthForbidden):
            enforce_whitelisted_domain(backend, details)

    def test_rejects_lookalike_domain_suffix(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        details = details_from(entra_claims(f"person@evil-{ALLOWED_DOMAIN}"))

        with self.assertRaises(AuthForbidden):
            enforce_whitelisted_domain(backend, details)

    def test_rejects_identity_without_an_email(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])

        with self.assertRaises(AuthForbidden):
            enforce_whitelisted_domain(backend, {"email": None})

    def test_rejects_malformed_email(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])

        with self.assertRaises(AuthForbidden):
            enforce_whitelisted_domain(backend, {"email": f"person@@{ALLOWED_DOMAIN}"})

    def test_allows_any_domain_when_no_allow_list_is_configured(self):
        # The single-tenant app registration is then the only restriction, which is a supported
        # (warned about) deployment shape.
        backend = _FakeAzureBackend(whitelisted_domains=[])
        details = details_from(entra_claims(f"person@{OTHER_DOMAIN}"))

        self.assertIsNone(enforce_whitelisted_domain(backend, details))


class TestAssociateByVerifiedEmail(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.email = f"existing@{ALLOWED_DOMAIN}"
        self.local_user = User.objects.create_user(
            username="existing-local-account",
            email=self.email,
            password="not-used-by-sso",  # noqa: S106
        )

    def test_creates_instead_of_linking_when_no_local_account_matches(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(f"brand-new@{ALLOWED_DOMAIN}")

        result = associate_by_verified_email(backend, details_from(claims), response=claims)

        self.assertIsNone(result)

    def test_links_when_the_address_matches_the_directory_owned_upn(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email)

        result = associate_by_verified_email(backend, details_from(claims), response=claims)

        self.assertEqual({"user": self.local_user, "is_new": False}, result)

    def test_links_when_the_provider_asserts_email_verified(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        # upn deliberately differs: the explicit verification claim is what carries the trust here.
        claims = entra_claims(self.email, upn=f"someone-else@{ALLOWED_DOMAIN}", email_verified=True)

        result = associate_by_verified_email(backend, details_from(claims), response=claims)

        self.assertEqual({"user": self.local_user, "is_new": False}, result)

    def test_accepts_string_valued_email_verified_claim(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email, upn=f"someone-else@{ALLOWED_DOMAIN}", email_verified="true")

        result = associate_by_verified_email(backend, details_from(claims), response=claims)

        self.assertEqual({"user": self.local_user, "is_new": False}, result)

    def test_refuses_when_the_address_is_only_self_asserted(self):
        # Guest/external identity: the email claim points at the existing local account but the
        # directory-owned upn does not. This is the account-takeover case (roadmap risk R3).
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email, upn=f"attacker@{ALLOWED_DOMAIN}")

        with self.assertRaises(AuthForbidden):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_refuses_when_email_verified_is_explicitly_false(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email, upn=self.email, email_verified=False)

        with self.assertRaises(AuthForbidden):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_refuses_to_link_when_no_allow_list_is_configured(self):
        backend = _FakeAzureBackend(whitelisted_domains=[])
        claims = entra_claims(self.email)

        with self.assertRaises(AuthForbidden):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_refuses_to_link_when_the_domain_is_outside_the_allow_list(self):
        backend = _FakeAzureBackend(whitelisted_domains=[OTHER_DOMAIN])
        claims = entra_claims(self.email)

        with self.assertRaises(AuthForbidden):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_refuses_to_link_an_account_already_bound_to_another_identity(self):
        backend = _FakeAzureBackend(
            whitelisted_domains=[ALLOWED_DOMAIN],
            linked_usernames=[self.local_user.username],
        )
        claims = entra_claims(self.email)

        with self.assertRaises(AuthForbidden):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_refuses_when_the_address_is_ambiguous(self):
        User.objects.create_user(username="second-local-account", email=self.email)
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email)

        with self.assertRaises(AuthFailed):
            associate_by_verified_email(backend, details_from(claims), response=claims)

    def test_is_a_no_op_when_the_uid_association_already_resolved_the_user(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email)

        result = associate_by_verified_email(
            backend, details_from(claims), response=claims, user=self.local_user,
        )

        self.assertIsNone(result)

    def test_ignores_inactive_local_accounts(self):
        self.local_user.is_active = False
        self.local_user.save()
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(self.email)

        self.assertIsNone(associate_by_verified_email(backend, details_from(claims), response=claims))


class TestEnforceZeroPrivilegeDefaults(DojoTestCase):

    def test_new_user_keeps_no_privileges(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        user = User.objects.create_user(username=f"new@{ALLOWED_DOMAIN}", email=f"new@{ALLOWED_DOMAIN}")

        enforce_zero_privilege_defaults(backend, user=user, is_new=True)

        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(Product.objects.filter(authorized_users__pk=user.pk).exists())
        self.assertFalse(Product_Type.objects.filter(authorized_users__pk=user.pk).exists())

    def test_privileges_present_on_a_new_user_are_stripped(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        user = User.objects.create_user(
            username=f"elevated@{ALLOWED_DOMAIN}",
            email=f"elevated@{ALLOWED_DOMAIN}",
            is_staff=True,
            is_superuser=True,
        )

        enforce_zero_privilege_defaults(backend, user=user, is_new=True)

        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_existing_users_are_left_alone(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        user = User.objects.create_user(
            username="long-standing-admin",
            email=f"admin@{ALLOWED_DOMAIN}",
            is_staff=True,
            is_superuser=True,
        )

        enforce_zero_privilege_defaults(backend, user=user, is_new=False)

        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_is_a_no_op_without_a_user(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])

        self.assertIsNone(enforce_zero_privilege_defaults(backend, user=None, is_new=True))


class TestProvisioningPipelineEndToEnd(DojoTestCase):

    """
    Run the DefectDojo-specific steps in the order settings.SOCIAL_AUTH_PIPELINE declares, together
    with the stock social_core steps that decide create-vs-link.

    The three steps that need the UserSocialAuth table (social_user, associate_user,
    load_extra_data) are left out: they are stock social_core code operating on a table this test
    run does not create, and the create-vs-link decision does not pass through them.
    test_sso_settings_flag asserts that the configured pipeline really does contain these steps in
    this relative order.
    """

    def _run(self, backend, claims, user=None, extra_details=None):
        details = details_from(claims)
        details.update(extra_details or {})
        strategy = backend.strategy

        enforce_whitelisted_domain(backend, details)

        result = associate_by_verified_email(backend, details, response=claims, user=user) or {}
        user = result.get("user", user)
        is_new = result.get("is_new", False)

        username = get_username(strategy, details, backend, user=user)["username"]
        created = create_user(strategy, details, backend, user=user, username=username)
        user = created.get("user", user)
        is_new = created.get("is_new", is_new)

        enforce_zero_privilege_defaults(backend, user=user, is_new=is_new)
        user_details(strategy, details, backend, user=user)
        return user, is_new

    def test_first_sign_in_provisions_a_zero_privilege_account(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        email = f"newcomer@{ALLOWED_DOMAIN}"

        user, is_new = self._run(backend, entra_claims(email, name="New Comer"))

        self.assertTrue(is_new)
        self.assertEqual(email, user.username)
        self.assertEqual(email, user.email)
        self.assertEqual("New", user.first_name)
        self.assertEqual("Comer", user.last_name)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_second_sign_in_of_the_same_address_reuses_the_provisioned_account(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        email = f"returning@{ALLOWED_DOMAIN}"

        first, first_is_new = self._run(backend, entra_claims(email))
        second, second_is_new = self._run(backend, entra_claims(email))

        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(1, User.objects.filter(email__iexact=email).count())

    def test_the_identity_provider_cannot_grant_itself_privileges(self):
        # user_details protects is_staff/is_superuser, and even if it did not the zero-privilege
        # step already ran. Claims are attacker-influenced input; treat them as such.
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        claims = entra_claims(f"ambitious@{ALLOWED_DOMAIN}")

        user, _ = self._run(
            backend, claims, extra_details={"is_superuser": True, "is_staff": True, "is_active": True},
        )

        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)

    def test_rejected_domain_never_reaches_account_creation(self):
        backend = _FakeAzureBackend(whitelisted_domains=[ALLOWED_DOMAIN])
        email = f"outsider@{OTHER_DOMAIN}"

        with self.assertRaises(AuthForbidden):
            self._run(backend, entra_claims(email))

        self.assertFalse(User.objects.filter(email__iexact=email).exists())
