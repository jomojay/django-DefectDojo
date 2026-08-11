from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from dojo.api_v2.serializers import ReportGenerateOptionSerializer, ReportGenerateSerializer
from dojo.api_v2.views import PrefetchDojoModelViewSet, report_generate, schema_with_prefetch
from dojo.authorization import api_permissions as permissions
from dojo.authorization.models import Product_Type_Group, Product_Type_Member
from dojo.models import Endpoint, Product_Type
from dojo.product_type.api.serializer import (
    ProductTypeGroupSerializer,
    ProductTypeMemberSerializer,
    ProductTypeSerializer,
)
from dojo.product_type.queries import (
    get_authorized_product_type_groups,
    get_authorized_product_type_members,
    get_authorized_product_types,
)
from dojo.utils import async_delete, get_setting


# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
class ProductTypeViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = ProductTypeSerializer
    queryset = Product_Type.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = [
        "id",
        "name",
        "critical_product",
        "key_product",
        "created",
        "updated",
    ]
    permission_classes = (
        IsAuthenticated,
        permissions.UserHasProductTypePermission,
    )

    def get_queryset(self):
        return get_authorized_product_types(
            "view",
        ).distinct()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if get_setting("ASYNC_OBJECT_DELETE"):
            async_del = async_delete()
            async_del.delete(instance)
        else:
            with Endpoint.allow_endpoint_init():  # TODO: Delete this after the move to Locations
                instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        request=ReportGenerateOptionSerializer,
        responses={status.HTTP_200_OK: ReportGenerateSerializer},
    )
    @action(
        detail=True, methods=["post"],
        # IsAuthenticated only: report generation requires View permission,
        # enforced by the permission-filtered get_queryset(). The viewset's
        # permission_classes would check Edit (POST), which is too restrictive.
        permission_classes=[IsAuthenticated],
    )
    def generate_report(self, request, pk=None):
        product_type = self.get_object()

        options = {}
        # prepare post data
        report_options = ReportGenerateOptionSerializer(
            data=request.data,
        )
        if report_options.is_valid():
            options["include_finding_notes"] = report_options.validated_data[
                "include_finding_notes"
            ]
            options["include_finding_images"] = report_options.validated_data[
                "include_finding_images"
            ]
            options[
                "include_executive_summary"
            ] = report_options.validated_data["include_executive_summary"]
            options[
                "include_table_of_contents"
            ] = report_options.validated_data["include_table_of_contents"]
        else:
            return Response(
                report_options.errors, status=status.HTTP_400_BAD_REQUEST,
            )

        data = report_generate(request, product_type, options)
        report = ReportGenerateSerializer(data)
        return Response(report.data)


# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
class ProductTypeMemberViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = ProductTypeMemberSerializer
    queryset = Product_Type_Member.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "product_type_id", "user_id"]
    permission_classes = (
        IsAuthenticated,
        permissions.UserHasProductTypeMemberPermission,
    )

    def get_queryset(self):
        return get_authorized_product_type_members(
            "view",
        ).distinct()

    def destroy(self, request, *args, **kwargs):
        # The delete half of the "a product type must keep at least one Owner"
        # invariant; the demote half lives in ProductTypeMemberSerializer.
        instance = self.get_object()
        if instance.role.is_owner:
            owners = Product_Type_Member.objects.filter(
                product_type=instance.product_type, role__is_owner=True,
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


# Authorization: object-based
@extend_schema_view(**schema_with_prefetch())
class ProductTypeGroupViewSet(
    PrefetchDojoModelViewSet,
):
    serializer_class = ProductTypeGroupSerializer
    queryset = Product_Type_Group.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "product_type_id", "group_id"]
    permission_classes = (
        IsAuthenticated,
        permissions.UserHasProductTypeGroupPermission,
    )

    def get_queryset(self):
        return get_authorized_product_type_groups(
            "view",
        ).distinct()

    @extend_schema(
        exclude=True,
    )
    def partial_update(self, request, pk=None):
        # Object authorization won't work if not all data is provided
        response = {"message": "Patch function is not offered in this path."}
        return Response(response, status=status.HTTP_405_METHOD_NOT_ALLOWED)
