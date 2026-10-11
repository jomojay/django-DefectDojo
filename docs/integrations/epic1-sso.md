# Epic 1 — SSO Core: Entra ID (Azure AD) OIDC

**Scope:** the Entra app-registration request template and the full technical spec for Microsoft Entra ID OIDC single sign-on (§5-§6 of the delivery plan). **Status: Shipped**, local E2E verified 2026-08-08 — see [`task-list.md`](task-list.md) for the PR-by-PR completion record. See [`INTEGRATIONS_ROADMAP.md`](../../INTEGRATIONS_ROADMAP.md) for the big picture, the Finalized Decisions this spec implements, and the Documentation Map explaining how all the pieces fit together.

**Convention:** risk IDs (`R#`) referenced below are defined in [`risk-register.md`](risk-register.md). "§7" refers to [`epic0-rbac-reactivation.md`](epic0-rbac-reactivation.md); "§8" to [`epic2-3-solarwinds.md`](epic2-3-solarwinds.md); "§2"/"§3"/"§10" refer to sections in the master `INTEGRATIONS_ROADMAP.md`.

---

## 5. Entra App-Registration Request Template (send to the target org's identity/IT team)

Reusable per-deployment — fill in the actual host and tenant when standing this up against a given organization's Entra tenant.

- **Type:** single-tenant App registration, Web platform, OIDC.
- **Redirect URI (reserve one per environment — staging + prod):** `https://<defectdojo-host>/complete/azuread-tenant-oauth2/`
- **Credential:** client secret, or (preferred, no expiry-driven outages) a certificate credential — delivered via a secure channel into the secret store, never email/chat.
- **ID token claims needed for MVP:** `email`/`preferred_username`, `oid`, `tid`. **Group claims are NOT required for this delivery** (only for the deferred Epic 1.5 — if requesting ahead of time to save a round trip, see §10 (in the master `INTEGRATIONS_ROADMAP.md`) for the exact Graph-claims config Pro's own integration uses — **note:** this cross-reference appears to be stale in the original document; §10 as written is the Rollout Plan and does not contain Graph-claims config content. Flagged here rather than silently resolved.).
- **Values to get back:** Application (client) ID, Directory (tenant) ID, the secret or certificate.
- **Optional, for a polished break-glass experience:** none needed from IT — DefectDojo Pro's own convention (`/login?force_login_form` appended to the URL) is a good UX pattern to replicate locally; see §6.5 below.

---

## 6. Epic 1 — SSO Core: Technical Spec

### 6.1 Packages

Add to `requirements.txt`: `social-auth-app-django` (pulls in `social-auth-core`, which provides the `azuread_tenant.AzureADTenantOAuth2` backend). Verify the current version against Django 5.2 compatibility at implementation time — pin it, don't float. `requests` and `cryptography` (already present) satisfy the transitive needs.

### 6.2 Settings — `dojo/settings/settings.dist.py`

New env defaults (near the existing `env = environ.Env(...)` block, ~line 155-175):
```python
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED=(bool, False),
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY=(str, ""),
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET=(str, ""),
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID=(str, ""),
DD_CLASSIC_AUTH_ENABLED=(bool, True),
```

Wire the existing dangling flag at line 486 — replace `CLASSIC_AUTH_ENABLED = True` with `CLASSIC_AUTH_ENABLED = env("DD_CLASSIC_AUTH_ENABLED")`.

Conditionally append the app/backend (mirroring the existing `django_prometheus` conditional pattern at line 991):
```python
AZUREAD_SSO_ENABLED = env("DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED")
if AZUREAD_SSO_ENABLED:
    INSTALLED_APPS = (*INSTALLED_APPS, "social_django")
    AUTHENTICATION_BACKENDS = (
        "social_core.backends.azuread_tenant.AzureADTenantOAuth2",
        *AUTHENTICATION_BACKENDS,  # ModelBackend stays -> local login unaffected
    )
    SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY = env("DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY")
    SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET = env("DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET")
    SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID = env("DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID")
    SOCIAL_AUTH_JSONFIELD_ENABLED = True
    SOCIAL_AUTH_PIPELINE = (...)  # see 6.4
```
Add `social_django.context_processors.backends` and `.login_redirect` to `TEMPLATES[0]["OPTIONS"]["context_processors"]`.

**Middleware:** insert `social_django.middleware.SocialAuthExceptionMiddleware` after `AuthenticationMiddleware` (line 814) but before `dojo.middleware.LoginRequiredMiddleware` (line 818), or auth-flow exceptions will redirect-loop instead of rendering.

**URLs:** mount `path("", include("social_django.urls", namespace="social"))` in the root urlconf. `LOGIN_EXEMPT_URLS` already contains `r"complete/"` and `r"oauth2/idpresponse"` (`settings.dist.py:517,519`) — verify these resolve correctly against `URL_PREFIX`; normally no new entry is needed.

### 6.3 Migrations

**Net new `dojo` migrations: zero.** Running `social_django`'s bundled migrations creates its own `UserSocialAuth` table, which holds the Entra subject-ID (`oid`/`sub`) → `Dojo_User` link — this is sufficient for login; no field needs to be added to `Dojo_User` (which is a `proxy=True` model and can't hold new fields anyway) or `UserContactInfo`.

### 6.4 Provisioning pipeline — `dojo/user/social_pipeline.py` (new file)

Standard social-auth pipeline (`social_details → social_uid → auth_allowed → social_user → associate_by_email → create_user → associate_user → load_extra_data → user_details`) with two custom behaviors:

- **Domain restriction:** `SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_WHITELISTED_DOMAINS` — read from an env var (e.g. `DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS`, comma-split), so the allowed domain(s) are set per-deployment rather than compiled in. Never hardcode a specific organization's domain in code.
- **Zero-privilege default:** new SSO users are created with `is_staff=False`, `is_superuser=False`, no `authorized_users` memberships. An admin grants access afterward via the existing Authorized Users panel.

**Security note (R3):** `associate_by_email` links an SSO identity to any local account with a matching email — gate it strictly to the whitelisted domain, and require `email_verified` from the token claims, to avoid an account-takeover vector via a spoofed/unverified email.

### 6.5 Login UI

Both template skins need a conditional SSO button (verified: currently no SSO block in either):
- `dojo/templates/dojo/login.html` (~line 126)
- `dojo/templates_classic/dojo/login.html`

```django
{% if AZUREAD_SSO_ENABLED %}
  <a class="btn btn-secondary" href="{% url 'social:begin' 'azuread-tenant-oauth2' %}">{% trans "Sign in with Microsoft" %}</a>
{% endif %}
{% if CLASSIC_AUTH_ENABLED %}{# existing password form #}{% endif %}
```
`DojoLoginView.get_context_data` (`dojo/user/ui/views.py:57`) needs extending to pass both flags. `form_valid` is untouched — SSO never enters this view.

**Break-glass UX (optional polish, not required for MVP):** DefectDojo Pro documents `/login?force_login_form` as a way to force the password form even when SSO is configured — worth replicating as a query-param check in the login view for a familiar, low-effort fallback path.

### 6.6 `django-single-session` interaction

`single_session` (gated by `SINGLE_USER_SESSION`, default off) hooks `user_logged_in`, which social-auth's standard `login()` call fires — the happy path works without changes. **One targeted test needed (R4):** confirm the pre-auth anonymous session social-auth uses to hold OAuth state/PKCE isn't evicted mid-redirect when single-session is enabled.

### 6.7 Testing

- `test_sso_pipeline.py` — mocked Entra claims through the pipeline; assert create-vs-link, zero-privilege default, domain-whitelist rejection.
- `test_sso_settings_flag.py` — flag off → no `social_django` in apps, no button; flag on → backend present.
- Extend `test_middleware_login_required.py` — assert the callback path stays login-exempt.
- Classic-login regression — password login still works with the Azure backend prepended.
- **Mock all IdP HTTP calls.** The real authorization-code redirect, redirect-URI/reply-URL correctness, consent, and PKCE are not unit-testable — reserved for manual verification against the real tenant in PR 5.

### 6.8 PR Sequence

| PR | Content | Blocked on Entra app reg? |
|---|---|---|
| **1** | Package + conditional `INSTALLED_APPS`/`AUTHENTICATION_BACKENDS`, env scaffolding, wire `CLASSIC_AUTH_ENABLED`, run `social_django` migrations | No |
| **2** | Login button (both skins), `SocialAuthExceptionMiddleware` placement | No |
| **3** | `social_pipeline.py`, unit tests against mocked claims | No |
| **4** | Helm `extraEnv`/secret plumbing, operator docs | No |
| **5** | Real client ID/secret/tenant ID, staging E2E, single-session verification | **Yes — only this PR** |

### 6.9 Secrets across deployment surfaces

- **Dev/compose:** `DD_*` via `docker-compose.yml` `environment:` blocks. Local secret goes in `docker/extra_settings/local_settings.py`, which is copied to `dojo/settings/local_settings.py` on release-mode boot (`docker/extra_settings/README.md`; `dojo/settings/settings.py:9` already `include(optional("local_settings.py"))`).
- **Prod/Helm:** client secret as a K8s Secret via `extraEnv[].valueFrom.secretKeyRef` (django/uwsgi/celery components expose `extraEnv: []` in `helm/defectdojo/values.yaml`). Never a plaintext Helm value.
