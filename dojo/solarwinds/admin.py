from django.contrib import admin

from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product, SolarWinds_Ticket


@admin.register(SolarWinds_Instance)
class SolarWindsInstanceAdmin(admin.ModelAdmin):

    """
    Admin support for SolarWinds_Instance.

    ``api_token`` is excluded rather than shown: it is stored encrypted, so the admin
    would render the ciphertext and - worse - saving the form would write that
    ciphertext back as if it were plaintext, double-encrypting the credential.
    Tokens are set through ``SolarWinds_Instance.set_api_token()``.
    """

    list_display = ("configuration_name", "subdomain", "region", "push_enabled", "poll_enabled")
    list_filter = ("region", "push_enabled", "poll_enabled")
    search_fields = ("configuration_name", "subdomain")
    exclude = ("api_token",)


@admin.register(SolarWinds_Product)
class SolarWindsProductAdmin(admin.ModelAdmin):

    """Admin support for the per-product routing overrides."""

    list_display = ("product", "solarwinds_instance", "push_all_findings")
    list_filter = ("solarwinds_instance", "push_all_findings")
    raw_id_fields = ("product",)


@admin.register(SolarWinds_Ticket)
class SolarWindsTicketAdmin(admin.ModelAdmin):

    """
    Admin support for the Finding <-> incident link table.

    ``solarwinds_instance`` is surfaced in the list and as a filter because incident
    ids are only unique within a tenant: "incident 9001" is ambiguous on its own, and
    an operator triaging a sync problem needs to know which tenant a row belongs to
    (see the risk register, R24).
    """

    list_display = (
        "incident_number", "incident_id", "solarwinds_instance", "finding",
        "last_synced_state", "external_updated_at", "created",
    )
    list_filter = ("solarwinds_instance", "last_synced_state")
    search_fields = ("incident_id", "incident_number")
    raw_id_fields = ("finding",)
