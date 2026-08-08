"""
Tests for the SSO feature flag: what the settings module produces with it off and with it on.

``dojo/settings/settings.dist.py`` decides at import time whether ``social_django`` is installed,
whether the Azure backend is prepended, where ``SocialAuthExceptionMiddleware`` lands and which
paths stay login-exempt. None of that can be reached with ``override_settings``, so these tests
execute the settings module in an isolated namespace with a controlled environment and assert on
the result. Nothing is imported from an identity provider and no network call is made.
"""

import os
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from social_core.backends.azuread_tenant import AzureADTenantOAuth2
from social_core.registry import REGISTRY

from dojo.models import User
from dojo.user.ui.views import DojoLoginView

from .dojo_test_case import DojoTestCase

AZUREAD_BACKEND = "social_core.backends.azuread_tenant.AzureADTenantOAuth2"
MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
SOCIAL_EXCEPTION_MIDDLEWARE = "social_django.middleware.SocialAuthExceptionMiddleware"
AUTHENTICATION_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"
LOGIN_REQUIRED_MIDDLEWARE = "dojo.middleware.LoginRequiredMiddleware"

# Placeholder values only. Real client/tenant identifiers belong in the deployment environment.
SSO_ENV = {
    "DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED": "True",
    "DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY": "client-id-placeholder",
    "DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET": "client-secret-placeholder",
    "DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID": "tenant-id-placeholder",
    "DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS": " One.Example , two.example ",
}


def load_settings_namespace(**environment):
    """
    Execute settings.dist.py in a throwaway namespace under the given environment.

    The module only reads the environment and builds plain data structures, so running it a second
    time inside the test process has no effect on the settings Django is already using.
    """
    settings_path = Path(__file__).resolve().parent.parent / "dojo" / "settings" / "settings.dist.py"
    namespace = {
        "__file__": str(settings_path),
        "__name__": "dojo.settings.settings_dist_under_test",
    }
    source = settings_path.read_text(encoding="utf-8")
    with mock.patch.dict(os.environ, environment, clear=False):
        exec(compile(source, str(settings_path), "exec"), namespace)  # noqa: S102
    return namespace


def exempt_url_matchers(namespace):
    """Rebuild the regex list dojo.middleware.LoginRequiredMiddleware compiles from the settings."""
    return [re.compile(expression) for expression in namespace["LOGIN_EXEMPT_URLS"]]


class TestSsoFlagDisabled(DojoTestCase):

    """With the flag off nothing about the deployment changes - that is the dark-launch guarantee."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.namespace = load_settings_namespace(DD_SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_ENABLED="False")

    def test_flag_is_off(self):
        self.assertFalse(self.namespace["AZUREAD_SSO_ENABLED"])

    def test_social_django_is_not_installed(self):
        self.assertNotIn("social_django", self.namespace["INSTALLED_APPS"])

    def test_only_the_model_backend_authenticates(self):
        self.assertEqual((MODEL_BACKEND,), self.namespace["AUTHENTICATION_BACKENDS"])

    def test_no_social_auth_middleware(self):
        self.assertNotIn(SOCIAL_EXCEPTION_MIDDLEWARE, self.namespace["MIDDLEWARE"])

    def test_no_social_auth_context_processors(self):
        processors = self.namespace["TEMPLATES"][0]["OPTIONS"]["context_processors"]
        self.assertFalse([processor for processor in processors if processor.startswith("social_django.")])

    def test_no_pipeline_is_configured(self):
        self.assertNotIn("SOCIAL_AUTH_PIPELINE", self.namespace)

    def test_no_client_secret_is_exposed_as_a_setting(self):
        social_settings = [name for name in self.namespace if name.startswith("SOCIAL_AUTH_")]
        self.assertEqual([], social_settings)

    def test_the_live_test_run_has_sso_disabled(self):
        # Guards the assumption the rendering tests below rely on.
        self.assertFalse(getattr(settings, "AZUREAD_SSO_ENABLED", False))


class TestSsoFlagEnabled(DojoTestCase):

    """With the flag on the plumbing is complete and correctly ordered."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.namespace = load_settings_namespace(**SSO_ENV)

    def test_flag_is_on(self):
        self.assertTrue(self.namespace["AZUREAD_SSO_ENABLED"])

    def test_social_django_is_installed(self):
        self.assertIn("social_django", self.namespace["INSTALLED_APPS"])

    def test_azure_backend_is_first_and_the_model_backend_stays_second(self):
        # Local username/password login is never removed: ModelBackend has to survive.
        self.assertEqual((AZUREAD_BACKEND, MODEL_BACKEND), self.namespace["AUTHENTICATION_BACKENDS"])

    def test_exception_middleware_sits_between_authentication_and_login_required(self):
        middleware = list(self.namespace["MIDDLEWARE"])
        self.assertIn(SOCIAL_EXCEPTION_MIDDLEWARE, middleware)
        self.assertLess(
            middleware.index(AUTHENTICATION_MIDDLEWARE),
            middleware.index(SOCIAL_EXCEPTION_MIDDLEWARE),
        )
        self.assertLess(
            middleware.index(SOCIAL_EXCEPTION_MIDDLEWARE),
            middleware.index(LOGIN_REQUIRED_MIDDLEWARE),
        )

    def test_context_processors_are_registered(self):
        processors = self.namespace["TEMPLATES"][0]["OPTIONS"]["context_processors"]
        self.assertIn("social_django.context_processors.backends", processors)
        self.assertIn("social_django.context_processors.login_redirect", processors)

    def test_credentials_come_from_the_environment(self):
        self.assertEqual("client-id-placeholder", self.namespace["SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY"])
        self.assertEqual("client-secret-placeholder", self.namespace["SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET"])
        self.assertEqual("tenant-id-placeholder", self.namespace["SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID"])

    def test_whitelisted_domains_are_split_normalised_and_trimmed(self):
        self.assertEqual(
            ["one.example", "two.example"],
            self.namespace["SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_WHITELISTED_DOMAINS"],
        )

    def test_empty_whitelist_produces_an_empty_list_not_a_blank_entry(self):
        namespace = load_settings_namespace(**SSO_ENV | {"DD_SOCIAL_AUTH_AZUREAD_WHITELISTED_DOMAINS": ""})
        self.assertEqual([], namespace["SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_WHITELISTED_DOMAINS"])

    def test_jsonfield_storage_is_enabled(self):
        self.assertTrue(self.namespace["SOCIAL_AUTH_JSONFIELD_ENABLED"])

    def test_https_redirect_uri_can_be_forced_without_a_code_change(self):
        # The identity provider matches redirect_uri against the registered reply URL exactly, so a
        # deployment behind a TLS-terminating proxy has to be able to force https from the
        # environment alone.
        self.assertFalse(self.namespace["SOCIAL_AUTH_REDIRECT_IS_HTTPS"])

        namespace = load_settings_namespace(**SSO_ENV | {"DD_SOCIAL_AUTH_REDIRECT_IS_HTTPS": "True"})

        self.assertTrue(namespace["SOCIAL_AUTH_REDIRECT_IS_HTTPS"])

    def test_exceptions_are_handled_by_the_middleware_even_under_debug(self):
        self.assertFalse(self.namespace["SOCIAL_AUTH_RAISE_EXCEPTIONS"])

    def test_pipeline_keeps_every_default_step_and_adds_the_dojo_steps(self):
        pipeline = list(self.namespace["SOCIAL_AUTH_PIPELINE"])
        expected = [
            "social_core.pipeline.social_auth.social_details",
            "social_core.pipeline.social_auth.social_uid",
            "social_core.pipeline.social_auth.auth_allowed",
            "dojo.user.social_pipeline.enforce_whitelisted_domain",
            "social_core.pipeline.social_auth.social_user",
            "dojo.user.social_pipeline.associate_by_verified_email",
            "social_core.pipeline.user.get_username",
            "social_core.pipeline.user.create_user",
            "dojo.user.social_pipeline.enforce_zero_privilege_defaults",
            "social_core.pipeline.social_auth.associate_user",
            "social_core.pipeline.social_auth.load_extra_data",
            "social_core.pipeline.user.user_details",
        ]
        self.assertEqual(expected, pipeline)

    def test_the_stock_associate_by_email_step_is_not_used(self):
        # It links on a bare email match; dojo.user.social_pipeline.associate_by_verified_email is
        # the hardened replacement (roadmap risk R3).
        self.assertNotIn(
            "social_core.pipeline.social_auth.associate_by_email",
            self.namespace["SOCIAL_AUTH_PIPELINE"],
        )

    def test_callback_path_is_login_exempt(self):
        matchers = exempt_url_matchers(self.namespace)
        path = f"{self.namespace['URL_PREFIX']}complete/azuread-tenant-oauth2/"
        self.assertTrue(any(matcher.match(path) for matcher in matchers), path)

    def test_begin_path_is_login_exempt(self):
        matchers = exempt_url_matchers(self.namespace)
        path = f"{self.namespace['URL_PREFIX']}login/azuread-tenant-oauth2/"
        self.assertTrue(any(matcher.match(path) for matcher in matchers), path)

    def test_callback_and_begin_paths_are_login_exempt_behind_a_url_prefix(self):
        # The pre-existing unprefixed r"complete/" entry only matches when URL_PREFIX is empty, so
        # this asserts the prefix-aware entries really are what make a prefixed deployment work.
        namespace = load_settings_namespace(**SSO_ENV | {"DD_URL_PREFIX": "dojo/"})
        matchers = exempt_url_matchers(namespace)

        self.assertEqual("dojo/", namespace["URL_PREFIX"])
        for path in ("dojo/complete/azuread-tenant-oauth2/", "dojo/login/azuread-tenant-oauth2/"):
            self.assertTrue(any(matcher.match(path) for matcher in matchers), path)

    def test_disconnect_path_is_not_login_exempt(self):
        # Unlinking an identity is an authenticated action and must stay behind the login wall.
        matchers = exempt_url_matchers(self.namespace)
        path = f"{self.namespace['URL_PREFIX']}disconnect/azuread-tenant-oauth2/"
        self.assertFalse(any(matcher.match(path) for matcher in matchers), path)


class TestSsoButtonMarkup(DojoTestCase):

    """
    The SSO button has to submit a POST, not follow a link.

    social-app-django decorates its ``social:begin`` view with ``@require_POST`` (and does not
    exempt it from CSRF), so a plain anchor returns 405. The rendered page cannot be asserted here
    because the URL only exists when the app is installed, so the templates are checked at the
    source level - which is exactly the regression worth guarding.
    """

    TEMPLATES = (
        Path(__file__).resolve().parent.parent / "dojo" / "templates" / "dojo" / "login.html",
        Path(__file__).resolve().parent.parent / "dojo" / "templates_classic" / "dojo" / "login.html",
    )

    def test_both_skins_submit_the_sso_request_as_a_post_with_a_csrf_token(self):
        for template in self.TEMPLATES:
            with self.subTest(template=template.name):
                source = template.read_text(encoding="utf-8")

                self.assertIn(
                    "<form method=\"POST\" action=\"{% url 'social:begin' 'azuread-tenant-oauth2' %}\">",
                    source,
                )
                self.assertIn("{% csrf_token %}", source)
                self.assertNotIn("href=\"{% url 'social:begin'", source)

    def test_both_skins_gate_the_button_on_the_feature_flag(self):
        for template in self.TEMPLATES:
            with self.subTest(template=template.name):
                source = template.read_text(encoding="utf-8")

                self.assertIn("{% if AZUREAD_SSO_ENABLED %}", source)
                self.assertIn("{% if SHOW_CLASSIC_AUTH_FORM %}", source)


class TestLoginPageWithoutSso(DojoTestCase):

    """Rendering assertions against the running configuration, which has SSO off."""

    def test_login_page_has_no_sso_button(self):
        response = Client().get(reverse("login"))

        self.assertEqual(200, response.status_code)
        self.assertNotContains(response, "Sign in with Microsoft")
        self.assertNotContains(response, "sso-azuread-tenant-oauth2")

    def test_login_page_still_renders_the_password_form(self):
        response = Client().get(reverse("login"))

        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')


class TestLoginViewContextFlags(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def _context(self, query_string=""):
        request = self.factory.get(f"/login{query_string}")
        view = DojoLoginView()
        view.setup(request)
        return view.get_context_data(form=view.get_form())

    def test_without_sso_the_password_form_is_shown(self):
        context = self._context()

        self.assertFalse(context["AZUREAD_SSO_ENABLED"])
        self.assertTrue(context["CLASSIC_AUTH_ENABLED"])
        self.assertTrue(context["SHOW_CLASSIC_AUTH_FORM"])

    @override_settings(AZUREAD_SSO_ENABLED=True)
    def test_with_sso_the_password_form_is_collapsed_by_default(self):
        context = self._context()

        self.assertTrue(context["AZUREAD_SSO_ENABLED"])
        self.assertTrue(context["CLASSIC_AUTH_ENABLED"])
        self.assertFalse(context["SHOW_CLASSIC_AUTH_FORM"])

    @override_settings(AZUREAD_SSO_ENABLED=True)
    def test_force_login_form_reveals_the_password_form(self):
        context = self._context("?force_login_form")

        self.assertTrue(context["SHOW_CLASSIC_AUTH_FORM"])

    @override_settings(AZUREAD_SSO_ENABLED=True, CLASSIC_AUTH_ENABLED=False)
    def test_force_login_form_cannot_re_enable_disabled_password_login(self):
        context = self._context("?force_login_form")

        self.assertFalse(context["CLASSIC_AUTH_ENABLED"])
        self.assertFalse(context["SHOW_CLASSIC_AUTH_FORM"])


class _MinimalDefaultStrategy:

    """
    Stand-in for the strategy social_django's AppConfig.ready() installs into social_core's REGISTRY.

    django.contrib.auth.load_backend() instantiates every configured backend with no arguments, so
    a social_core backend can only sit in AUTHENTICATION_BACKENDS at all because that default
    strategy exists. Reproducing it here exercises the same code path without pulling social_django
    into this test process' app registry, which cannot be done with override_settings (re-running
    every AppConfig.ready() re-registers dojo's watson search models and fails).
    """

    def request_data(self, merge=True):  # noqa: FBT002
        return {}

    def absolute_uri(self, path=None):
        return path


class TestClassicAuthGate(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.password = "Un1t-Test-Passw0rd!"
        self.user = User.objects.create_user(username="classic-login-user", password=self.password)

    def test_password_login_works_with_the_azure_backend_prepended(self):
        # Regression guard: prepending a social-auth backend must not shadow ModelBackend. Django
        # tries the Azure backend first; it returns None for username/password credentials and the
        # request falls through to ModelBackend.
        REGISTRY.default_strategy = _MinimalDefaultStrategy()
        self.addCleanup(REGISTRY.reset)

        with override_settings(AUTHENTICATION_BACKENDS=(AZUREAD_BACKEND, MODEL_BACKEND)):
            logged_in = Client().login(username=self.user.username, password=self.password)

        self.assertTrue(logged_in)

    def test_azure_backend_rejects_username_and_password_credentials(self):
        REGISTRY.default_strategy = _MinimalDefaultStrategy()
        self.addCleanup(REGISTRY.reset)

        backend = AzureADTenantOAuth2()

        self.assertIsNone(backend.authenticate(username=self.user.username, password=self.password))

    def test_password_login_succeeds_by_default(self):
        client = Client()

        response = client.post(
            reverse("login"),
            {"username": self.user.username, "password": self.password},
        )

        self.assertEqual(302, response.status_code)
        self.assertIn("_auth_user_id", client.session)

    @override_settings(CLASSIC_AUTH_ENABLED=False)
    def test_password_login_is_refused_when_classic_auth_is_disabled(self):
        # The flag has to be a real gate, not only a rendering hint: with the form hidden, a POST
        # reaching this view is by definition not coming from the rendered page.
        client = Client()

        response = client.post(
            reverse("login"),
            {"username": self.user.username, "password": self.password},
        )

        # PermissionDenied, rendered by dojo.views.custom_unauthorized_view - which upstream wires
        # to handler403 but returns with status 400. Asserting the observed contract, not the
        # intended one, so this test does not quietly pass if the gate stops firing.
        self.assertEqual(400, response.status_code)
        self.assertNotIn("_auth_user_id", client.session)
