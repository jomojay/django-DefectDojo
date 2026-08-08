from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.middleware import LoginRequiredMiddleware
from dojo.models import User

from .dojo_test_case import DojoTestCase, versioned_fixtures

# social-auth's callback path. An anonymous browser lands here carrying the authorization code, so
# it has to stay outside the login wall or single sign-on cannot complete.
SSO_CALLBACK_PATH = "/complete/azuread-tenant-oauth2/"
SSO_BEGIN_PATH = "/login/azuread-tenant-oauth2/"


class TokenAuthenticatedView(APIView):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response({"username": request.user.username})


@versioned_fixtures
class TestLoginRequiredMiddlewareDdUser(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.admin = User.objects.get(username="admin")

    def test_sets_dd_user_for_session_authenticated_request(self):
        request = self.factory.get("/dashboard")
        request.user = self.admin

        middleware = LoginRequiredMiddleware(lambda _request: HttpResponse("OK"))
        fake_uwsgi = SimpleNamespace(set_logvar=Mock())

        with patch.dict("sys.modules", {"uwsgi": fake_uwsgi}):
            response = middleware(request)

        self.assertEqual(200, response.status_code)
        fake_uwsgi.set_logvar.assert_called_once_with("dd_user", str(self.admin))

    def test_sets_dd_user_for_drf_token_authenticated_request(self):
        token, _ = Token.objects.get_or_create(user=self.admin)

        request = self.factory.get(
            "/api/v2/mock-endpoint/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()

        middleware = LoginRequiredMiddleware(TokenAuthenticatedView.as_view())
        fake_uwsgi = SimpleNamespace(set_logvar=Mock())

        with patch.dict("sys.modules", {"uwsgi": fake_uwsgi}):
            response = middleware(request)

        self.assertEqual(200, response.status_code)
        fake_uwsgi.set_logvar.assert_called_once_with("dd_user", str(self.admin))


class TestLoginRequiredMiddlewareSsoPaths(DojoTestCase):

    """
    The single sign-on entry and callback paths must not be intercepted for anonymous users.

    These assertions run against the live LOGIN_EXEMPT_URLS, which dojo.middleware compiles at
    import time. They hold whether or not the SSO feature flag is on: with the flag off the paths
    simply 404 later in the stack, and with it on they resolve to social_django's views.
    """

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.middleware = LoginRequiredMiddleware(lambda _request: HttpResponse("OK"))

    def _anonymous_get(self, path):
        request = self.factory.get(path)
        request.user = AnonymousUser()
        return self.middleware(request)

    def test_sso_callback_path_is_login_exempt(self):
        response = self._anonymous_get(SSO_CALLBACK_PATH)

        self.assertEqual(200, response.status_code)

    def test_sso_begin_path_is_login_exempt(self):
        response = self._anonymous_get(SSO_BEGIN_PATH)

        self.assertEqual(200, response.status_code)

    def test_an_ordinary_path_still_requires_login(self):
        # Guards against the exempt patterns being written loosely enough to open up the whole app.
        response = self._anonymous_get("/dashboard")

        self.assertEqual(302, response.status_code)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))

    def test_the_disconnect_path_still_requires_login(self):
        response = self._anonymous_get("/disconnect/azuread-tenant-oauth2/")

        self.assertEqual(302, response.status_code)
