---
title: "Microsoft Entra ID (OIDC)"
description: "Configure Microsoft Entra ID single sign-on on this DefectDojo build"
weight: 21
audience: opensource
---

This build of DefectDojo can authenticate users against **Microsoft Entra ID** (formerly Azure AD) over OIDC, alongside local username/password login. It is off by default: an instance that does not configure it behaves exactly as it did before — no extra app, no extra middleware, no extra login button.

Every value on this page is per-deployment configuration supplied through the environment. There are no tenant identifiers, domains or secrets baked into the code, the settings defaults, or the container images. Point the instance at whichever directory it should trust.

{{% alert title="Keep local login enabled" color="warning" %}}
`DD_CLASSIC_AUTH_ENABLED` stays `True` unless you have a very good reason. It is the break-glass path when the identity provider is unreachable or misconfigured. If you disable it and SSO then fails, nobody can log in.
{{% /alert %}}

## 1. Register the application in Entra ID

In the Entra admin center, create a single-tenant **App registration** with a **Web** platform:

| Setting | Value |
| --- | --- |
| Platform | Web |
| Redirect URI | `https://<your-defectdojo-host>/complete/azuread-tenant-oauth2/` |
| Credential | Client secret (or a certificate credential, which does not expire on a fixed schedule) |
| ID token claims | `email` / `preferred_username`, `oid`, `tid` |

Reserve one redirect URI per environment — staging and production each need their own entry, and the identity provider compares the value character for character.

Group claims are **not** required. Access is granted inside DefectDojo, not by directory group membership (see step 5).

Collect three values from the registration: the **Application (client) ID**, the **Directory (tenant) ID**, and the **client secret**.

## 2. Configure DefectDojo

| Environment variable | Purpose |
| --- | --- |
| `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED` | Turns the whole feature on. Default `False`. |
| `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY` | Application (client) ID. |
| `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET` | Client secret. See "Handling the client secret" below. |
| `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID` | Directory (tenant) ID. |
| `DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS` | Comma separated email domains allowed to sign in, e.g. `one.example,two.example`. Optional but recommended — see "Domain allow-list". |
| `DD_SOCIAL_AUTH_REDIRECT_IS_HTTPS` | Force the redirect URI to be built with `https`. Default `False`. |
| `DD_CLASSIC_AUTH_ENABLED` | Local username/password login. Default `True`. |

These settings must be identical on **every** container that loads `dojo.settings` — the web workers, the Celery worker, the Celery beat scheduler and the initializer. The initializer is the one that runs `manage.py migrate`, and the tables backing the identity links are only created when the flag is on there too.

### Docker Compose

The variables are already wired through `docker-compose.yml` for all four services. Set them in your `.env` file or in the shell environment:

```
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED=True
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY=<application-client-id>
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID=<directory-tenant-id>
DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET_FILE=/run/secrets/defectdojo-sso-client-secret
DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS=<one.example>,<two.example>
```

`dojo/settings/template-env` carries the same list, commented out, as a starting point.

### Kubernetes / Helm

The chart exposes an `sso` block. The client secret is never a Helm value: create the Secret out of band (or through your secret manager, External Secrets, Sealed Secrets, …) and reference it.

```yaml
sso:
  classicAuthEnabled: true
  azureAd:
    enabled: true
    clientId: "<application-client-id>"
    tenantId: "<directory-tenant-id>"
    whitelistedDomains: "<one.example>,<two.example>"
    redirectIsHttps: true
    clientSecret:
      secretName: defectdojo-sso-azuread
      secretKey: DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET
```

```bash
kubectl create secret generic defectdojo-sso-azuread \
  --from-literal=DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET='<client-secret>'
```

The chart injects the block into the uwsgi, Celery worker, Celery beat and initializer containers. Pods will not start until the Secret exists — that is deliberate.

### Handling the client secret

Never place the secret in `values.yaml`, in a committed `.env` file, or in the compose file. Two supported routes:

* **File indirection.** Any `DD_*` setting accepts a `_FILE` suffix, so `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET_FILE=/run/secrets/<name>` reads the value from a mounted file (Docker/Compose secrets, mounted Kubernetes Secrets, Vault Agent).
* **Kubernetes Secret reference.** The `sso.azureAd.clientSecret` block above.

## 3. Apply the database migration

The identity links live in their own table, added by the `social_django` migrations. No DefectDojo migration is added or changed. Run migrations after enabling the flag:

```bash
python manage.py migrate
```

With Compose or Helm the initializer does this for you, provided it also has the SSO variables set.

## 4. Sign in

The login page grows a **Sign in with Microsoft** button. With SSO configured, the password form is collapsed by default; append `?force_login_form` to the login URL to bring it back:

```
https://<your-defectdojo-host>/login?force_login_form
```

That query parameter only affects what is rendered. It cannot re-enable password authentication that `DD_CLASSIC_AUTH_ENABLED=False` has switched off.

## 5. Grant access

A user signing in for the first time is provisioned just-in-time with **no privileges at all**: not staff, not superuser, and not a member of any Product or Product Type. They can log in and will see nothing until an administrator grants access.

Grant it the same way you would for a locally created account, through the **Add Authorized User** panel on a Product or Product Type — see [Authorized Users](/admin/user_management/os__authorized_users/). Directory group membership is not consulted.

## Domain allow-list

`DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS` restricts which email domains may sign in, on top of the restriction the single-tenant app registration already imposes.

It also controls something less obvious. When an SSO identity arrives with an address that already belongs to a local DefectDojo account, DefectDojo will link the two — but only when **all** of the following hold:

* the allow-list is non-empty (linking is an explicit operator opt-in),
* the address sits inside the allow-list,
* the identity provider vouches for the address, either through an `email_verified` claim or by the address matching the directory-owned `upn` / `preferred_username` claim,
* exactly one local account matches,
* that account is not already linked to a different identity.

If a local account matches but any of those fail, the sign-in is refused rather than silently creating a second account with the same address. Leaving the allow-list empty therefore disables linking entirely: new sign-ins always get a fresh, zero-privilege account.

## Behind a reverse proxy

The identity provider compares the `redirect_uri` DefectDojo sends against the registered redirect URI exactly, so an `http://` value is rejected by a registration that lists `https://`. If TLS is terminated in front of DefectDojo, either:

* set `DD_SECURE_PROXY_SSL_HEADER=True` and have the proxy send `X-Forwarded-Proto: https`, or
* set `DD_SOCIAL_AUTH_REDIRECT_IS_HTTPS=True`.

## Turning it off

Set `DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED=False` and restart. The login button disappears and the password form comes back. Accounts provisioned through SSO remain as ordinary DefectDojo users; they simply have no way to authenticate until they are given a password or SSO is re-enabled.
