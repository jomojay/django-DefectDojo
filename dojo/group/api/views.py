"""
ViewSets for group management (INTEGRATIONS_ROADMAP.md §7.7).

Ported from the pre-3.0 ``dojo/api_v2/views.py`` (commit ``db1932c9e``).

``DojoGroupMemberViewSet`` disables ``PATCH``: object-level authorization for a
membership row is decided from the *whole* payload (which group, which user,
which role), so a partial update cannot be authorized correctly. The same rule
applies to every member/group viewset in this PR.
"""
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from dojo.api_v2.views import PrefetchDojoModelViewSet, schema_with_prefetch
from dojo.authorization import api_permissions as permissions
from dojo.authorization.models import Dojo_Group, Dojo_Group_Member
from dojo.group.api.serializer import DojoGroupMemberSerializer, DojoGroupSerializer
from dojo.group.queries import get_authorized_group_members, get_authorized_groups


# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
class DojoGroupViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = DojoGroupSerializer
    queryset = Dojo_Group.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "name", "social_provider"]
    permission_classes = (
        IsAuthenticated,
        permissions.UserHasDojoGroupPermission,
    )

    def get_queryset(self):
        return get_authorized_groups("view").distinct()


# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
class DojoGroupMemberViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = DojoGroupMemberSerializer
    queryset = Dojo_Group_Member.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "group_id", "user_id"]
    permission_classes = (
        IsAuthenticated,
        permissions.UserHasDojoGroupMemberPermission,
    )

    def get_queryset(self):
        return get_authorized_group_members("view").distinct()

    def destroy(self, request, *args, **kwargs):
        # "A group must keep at least one Owner", delete half.
        #
        # Deliberately BEYOND the pre-3.0 source, which guarded only the demote
        # path (in the serializer) and left destroy unguarded. This fork's group
        # UI does guard both (dojo/group/ui/views.py:delete_group_member), and an
        # API that can strand a group with no Owner while the UI refuses to is
        # the exact UI/API gap this epic exists to close. A group with no Owner
        # can never be administered again by anybody short of a superuser, and
        # no DB constraint expresses it.
        instance = self.get_object()
        if instance.role.is_owner:
            owners = Dojo_Group_Member.objects.filter(
                group=instance.group, role__is_owner=True,
            ).count()
            if owners <= 1:
                return Response(
                    "There must be at least one owner",
                    status=status.HTTP_400_BAD_REQUEST,
                )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        exclude=True,
    )
    def partial_update(self, request, pk=None):
        # Object authorization won't work if not all data is provided
        response = {"message": "Patch function is not offered in this path."}
        return Response(response, status=status.HTTP_405_METHOD_NOT_ALLOWED)
