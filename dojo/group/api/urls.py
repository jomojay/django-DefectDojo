from dojo.group.api import GROUP_MEMBERS_PATH, GROUPS_PATH
from dojo.group.api.views import DojoGroupMemberViewSet, DojoGroupViewSet


def add_group_urls(router):
    # Route / basename pairs restored verbatim from the pre-3.0 registrations
    # (db1932c9e:dojo/urls.py) and the "moved to Pro" comments in dojo/urls.py.
    router.register(GROUPS_PATH, DojoGroupViewSet, basename="dojo_group")
    router.register(GROUP_MEMBERS_PATH, DojoGroupMemberViewSet, basename="dojo_group_member")
    return router
