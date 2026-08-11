from dojo.organization.api.views import (
    OrganizationGroupViewSet,
    OrganizationMemberViewSet,
    OrganizationViewSet,
)


def add_organization_urls(router):
    router.register(r"organizations", OrganizationViewSet, basename="organization")
    # Route / basename pairs restored verbatim from
    # db1932c9e:dojo/organization/api/urls.py, matching the removal comment
    # these two replace.
    router.register(r"organization_members", OrganizationMemberViewSet, basename="organization_member")
    router.register(r"organization_groups", OrganizationGroupViewSet, basename="organization_group")
    return router
