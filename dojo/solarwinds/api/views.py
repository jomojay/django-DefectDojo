"""
API viewsets for the SolarWinds Service Desk configuration.

Both are gated by ``UserHasConfigurationPermissionSuperuser``, the same class
``ToolConfigurationsViewSet`` and ``JiraInstanceViewSet`` use - it maps each HTTP
method onto the model's Django configuration permission
(``dojo.view_solarwinds_instance`` for GET, ``dojo.add_solarwinds_instance`` for
POST, ...), so the API and the UI answer to exactly the same grant.

``DojoModelViewSet`` rather than ``PrefetchDojoModelViewSet``: these have no
prefetchable nested relations worth the extra schema surface, and JIRA's instance
viewset - the closest analogue - is plain for the same reason.
"""
from django_filters.rest_framework import DjangoFilterBackend

from dojo.api_v2.views import DojoModelViewSet
from dojo.authorization import api_permissions as permissions
from dojo.solarwinds.api.serializer import (
    SolarWindsInstanceSerializer,
    SolarWindsProductSerializer,
)
from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product


class SolarWindsInstanceViewSet(DojoModelViewSet):

    """
    Tenant configurations.

    ``api_token`` is write-only (R21): it can be set and replaced here, but no
    response from this viewset ever contains it, in plaintext or as ciphertext.
    """

    serializer_class = SolarWindsInstanceSerializer
    queryset = SolarWinds_Instance.objects.none()
    filter_backends = (DjangoFilterBackend,)
    # Deliberately no api_token / service_account_email here - filterable fields are
    # reflected into the schema and usable as an oracle.
    filterset_fields = ["id", "configuration_name", "subdomain", "region", "push_enabled", "poll_enabled"]
    permission_classes = (permissions.UserHasConfigurationPermissionSuperuser,)

    def get_queryset(self):
        return SolarWinds_Instance.objects.all().order_by("id")


class SolarWindsProductViewSet(DojoModelViewSet):

    """Per-product routing overrides, analogous to ``jira_product_configurations``."""

    serializer_class = SolarWindsProductSerializer
    queryset = SolarWinds_Product.objects.none()
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ["id", "solarwinds_instance", "product", "push_all_findings"]
    permission_classes = (permissions.UserHasConfigurationPermissionSuperuser,)

    def get_queryset(self):
        return SolarWinds_Product.objects.all().order_by("id")
