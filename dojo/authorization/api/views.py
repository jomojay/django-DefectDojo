"""
ViewSets for the role catalogue and global-role grants
(INTEGRATIONS_ROADMAP.md §7.7).

``RoleViewSet`` is read-only: the five roles are seeded by migration
``0106_role_model`` and the action set behind each one is a pure function
(``roles_permissions.get_roles_with_permissions()``), not data — so there is
nothing to create or edit through the API.

``GlobalRoleViewSet`` stays **superuser-only**. A ``Global_Role`` whose
``Role.is_owner`` is set now confers ownership over every product and product
type in the deployment (PRs 2-3), which makes this endpoint the privilege-
escalation surface of the whole system. ``IsSuperUser`` here is deliberate and
must not be relaxed to ``IsSuperUserOrGlobalOwner``: a global Owner being able
to mint more global Owners is exactly the loop to keep closed.
"""
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema_view
from rest_framework import viewsets
from rest_framework.permissions import DjangoModelPermissions, IsAuthenticated

from dojo.api_v2.views import PrefetchDojoModelViewSet, schema_with_prefetch
from dojo.authorization.api.serializer import GlobalRoleSerializer, RoleSerializer
from dojo.authorization.api_permissions import IsSuperUser
from dojo.authorization.models import Global_Role, Role


# Authorization: authenticated users
class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RoleSerializer
    queryset = Role.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "name"]
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return Role.objects.all().order_by("id")


# Authorization: superuser
@extend_schema_view(**schema_with_prefetch())
class GlobalRoleViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = GlobalRoleSerializer
    queryset = Global_Role.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "user", "group", "role"]
    permission_classes = (IsSuperUser, DjangoModelPermissions)

    def get_queryset(self):
        return Global_Role.objects.all().order_by("id")
