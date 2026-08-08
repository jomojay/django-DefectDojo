"""
Targeted coverage for the django-single-session interaction with single sign-on (roadmap risk R4).

``single_session`` hooks ``user_logged_in`` and, when ``SINGLE_USER_SESSION`` is on, deletes every
session already linked to the user. social-auth parks its OAuth ``state`` and PKCE code verifier in
the *anonymous* session before redirecting to the identity provider, so the question is whether
that pre-auth session survives long enough for the callback to complete.

``django.contrib.auth.login`` is the single place where the eviction is triggered, and social-auth
reaches it through the same ``do_login`` path a password login uses - so a password login through
the real view exercises exactly the same signal chain. No identity provider is contacted.
"""

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.urls import reverse

from dojo.models import User

from .dojo_test_case import DojoTestCase

# Session keys social_core.backends.oauth writes before redirecting to the identity provider.
OAUTH_STATE_KEY = "azuread-tenant-oauth2_state"
OAUTH_PKCE_KEY = "azuread-tenant-oauth2_code_verifier"


@override_settings(SINGLE_USER_SESSION=True)
class TestSingleSessionDoesNotEvictThePreAuthSession(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.password = "S1ngle-Session-Passw0rd!"
        self.user = User.objects.create_user(username="sso-single-session-user", password=self.password)

    def _client_with_pre_auth_session(self):
        """Return a client whose session already holds the OAuth state a redirect would have parked."""
        client = Client()
        session = client.session
        session[OAUTH_STATE_KEY] = "opaque-state-value"
        session[OAUTH_PKCE_KEY] = "opaque-code-verifier"
        session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        return client, session.session_key

    def _login(self, client):
        return client.post(
            reverse("login"),
            {"username": self.user.username, "password": self.password},
        )

    def test_oauth_state_written_before_login_is_still_readable_after_login(self):
        client, _ = self._client_with_pre_auth_session()

        response = self._login(client)

        self.assertEqual(302, response.status_code)
        self.assertIn("_auth_user_id", client.session)
        self.assertEqual("opaque-state-value", client.session.get(OAUTH_STATE_KEY))
        self.assertEqual("opaque-code-verifier", client.session.get(OAUTH_PKCE_KEY))

    def test_the_session_key_rotates_without_losing_the_pre_auth_data(self):
        # Django's login() calls cycle_key(): the pre-auth key is retired but its contents move to
        # the new key. Nothing about that is single-session specific, but it is the mechanism that
        # makes eviction of the "old" row harmless.
        client, pre_auth_key = self._client_with_pre_auth_session()

        self._login(client)

        self.assertNotEqual(pre_auth_key, client.session.session_key)
        self.assertFalse(Session.objects.filter(session_key=pre_auth_key).exists())
        self.assertTrue(Session.objects.filter(session_key=client.session.session_key).exists())

    def test_the_authenticated_session_remains_usable(self):
        client, _ = self._client_with_pre_auth_session()
        self._login(client)

        response = client.get(reverse("login"))

        # Still authenticated: no redirect back to the login wall from LoginRequiredMiddleware.
        self.assertEqual(200, response.status_code)
        self.assertIn("_auth_user_id", client.session)

    def test_single_session_is_actually_active_during_these_tests(self):
        # Control: without this, the tests above would still pass with the feature switched off.
        first, _ = self._client_with_pre_auth_session()
        self._login(first)
        first_key = first.session.session_key

        second, _ = self._client_with_pre_auth_session()
        self._login(second)

        self.assertFalse(Session.objects.filter(session_key=first_key).exists())
        self.assertTrue(Session.objects.filter(session_key=second.session.session_key).exists())


class TestSingleSessionDisabledByDefault(DojoTestCase):

    def test_default_deployment_does_not_evict_concurrent_sessions(self):
        # Documents the shipped default: SSO and single-session only interact when an operator
        # explicitly turns SINGLE_USER_SESSION on.
        self.assertFalse(settings.SINGLE_USER_SESSION)
