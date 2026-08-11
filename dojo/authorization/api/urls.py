from dojo.authorization.api import GLOBAL_ROLES_PATH, ROLES_PATH
from dojo.authorization.api.views import GlobalRoleViewSet, RoleViewSet


def add_authorization_urls(router):
    # Route / basename pairs restored verbatim from the pre-3.0 registrations
    # (db1932c9e:dojo/urls.py) and the "moved to Pro" comments in dojo/urls.py.
    router.register(ROLES_PATH, RoleViewSet, basename="role")
    router.register(GLOBAL_ROLES_PATH, GlobalRoleViewSet, basename="global_role")
    return router
