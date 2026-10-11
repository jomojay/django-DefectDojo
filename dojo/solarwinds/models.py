"""
Models for the SolarWinds Service Desk (Samanage) ITSM integration.

See INTEGRATIONS_ROADMAP.md 8.2 for the design rationale. Two things about these
models are load-bearing and were confirmed against a live trial tenant on
2026-09-18 rather than read off the OpenAPI spec (which is wrong on both points):

* ``subdomain`` - the working API base URL is the tenant's own host
  (``https://<subdomain>.samanage.com``), not the shared regional host the spec
  advertises. ``region`` is retained for support-contact purposes only and is
  deliberately NOT used to build a URL.
* ``open_state`` / ``resolved_states`` / ``severity_priority_map`` /
  ``default_category`` store tenant-configured **label strings**, not numeric IDs.
  There is no ``/states`` or ``/priorities`` lookup endpoint, so IDs would be
  undiscoverable, and the labels vary per tenant (the trial tenant had no
  "In Progress" state and no "Urgent" priority).

``api_token`` is stored encrypted via ``dojo_crypto_encrypt`` (8.4) - use
:meth:`SolarWinds_Instance.set_api_token` / :meth:`~SolarWinds_Instance.get_api_token`
rather than touching the field directly. This is the deliberate departure from
``dojo/jira/models.py``, which persists its credential in plaintext (risk R11).
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from dojo.solarwinds.validators import validate_subdomain

# Hostname suffix every Service Desk tenant lives on. The per-tenant subdomain is
# prefixed to this to form the API base URL (see 8.1 - the documented shared hosts
# api.samanage.com / apieu.samanage.com / apiau.samanage.com 404 on /incidents).
SAMANAGE_HOST_SUFFIX = "samanage.com"

# Retained for support/escalation purposes only; see the class docstring.
SOLARWINDS_REGION_CHOICES = (
    ("US", "US"),
    ("EU", "EU"),
    ("AU", "APJ"),
)


class SolarWinds_Instance(models.Model):

    """
    A configured SolarWinds Service Desk tenant DefectDojo can push incidents to.

    **Two fields, two different questions - do not collapse them.**
    ``severity_priority_map`` answers "which severities can *ever* become a ticket in
    this tenant, and under which priority label?". ``auto_push_severities`` answers
    "which of those fire on their own when a product has ``push_all_findings`` set?".
    A severity in the first but not the second is still pushable - only by a human
    choosing a specific finding, never automatically.

    ``auto_push_severities`` **defaults to an empty list, deliberately not to every key
    of the severity map.** Every switch in this module defaults to off
    (``push_enabled``, ``poll_enabled``), and the failure mode of a permissive default
    here is not a stray ticket but a flood: one SCA or SAST import of ~1,500 findings
    becomes ~1,500 individual ``POST /incidents`` calls, because this API has no batch
    or summary incident and ``SolarWinds_Ticket.finding`` is a OneToOne. An
    administrator opting each severity in explicitly is the cheap half of that trade.
    """

    configuration_name = models.CharField(
        max_length=255,
        unique=True,
        help_text=_("Name to identify this SolarWinds Service Desk configuration."),
    )
    subdomain = models.CharField(
        max_length=255,
        validators=[validate_subdomain],
        verbose_name=_("Tenant subdomain"),
        help_text=_(
            "Bare tenant subdomain, e.g. 'acme' for https://acme.samanage.com/. This is "
            "the real API base URL; the shared regional hosts do not serve ticket resources.",
        ),
    )
    region = models.CharField(
        max_length=3,
        choices=SOLARWINDS_REGION_CHOICES,
        default="US",
        help_text=_(
            "Region the tenant is hosted in. Recorded for support/escalation purposes "
            "only - the API base URL is built from the subdomain, not from this.",
        ),
    )
    api_token = models.CharField(
        max_length=900,
        help_text=_(
            "API token of a dedicated service-account admin user. Stored encrypted. "
            "Regenerating a token invalidates every prior token issued to that user, "
            "so do not use a shared personal account.",
        ),
    )
    service_account_email = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Email of the service account the API token belongs to (for auditing)."),
    )
    default_site_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Default Site id applied to incidents when a product has no override."),
    )
    default_department_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Default Department id applied to incidents when a product has no override."),
    )
    default_category = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Default category"),
        help_text=_(
            "Category LABEL, e.g. 'Security'. Unlike state and priority this is written "
            'as a nested {"name": ...} object on the wire.',
        ),
    )
    open_state = models.CharField(
        max_length=64,
        help_text=_(
            "Tenant workflow state LABEL used for newly created incidents, e.g. 'New'. "
            "Read it off a real incident in the tenant - state names vary and are not "
            "discoverable through any lookup endpoint.",
        ),
    )
    resolved_states = models.JSONField(
        default=list,
        help_text=_(
            "List of tenant workflow state LABELS that count as resolved/closed, "
            'e.g. ["Resolved", "Closed"]. Drives inbound reconciliation.',
        ),
    )
    severity_priority_map = models.JSONField(
        default=dict,
        help_text=_(
            "DefectDojo severity -> SolarWinds priority LABEL, "
            'e.g. {"Critical": "Critical", "High": "High"}. A severity with no entry '
            "here is never pushed at all. An invalid priority label is silently "
            "ignored by the API, so this map is also the client-side allowlist.",
        ),
    )
    auto_push_severities = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Auto-push severities"),
        help_text=_(
            "Severities that a product with 'Push all findings' opens incidents for "
            'automatically, e.g. ["Critical", "High"]. Must be a subset of the severity '
            "map above. Severities that are mapped but NOT listed here stay pushable, "
            "but only when a human explicitly asks for a specific finding. Empty means "
            "nothing is ever pushed automatically.",
        ),
    )
    push_enabled = models.BooleanField(
        default=False,
        help_text=_("Allow DefectDojo to create incidents in this tenant."),
    )
    poll_enabled = models.BooleanField(
        default=False,
        help_text=_("Allow the scheduled poll to reconcile resolved incidents back into findings."),
    )

    class Meta:
        ordering = ["configuration_name"]
        verbose_name = _("SolarWinds Instance")
        verbose_name_plural = _("SolarWinds Instances")

    def __str__(self):
        return self.configuration_name

    def save(self, *args, **kwargs):
        self.subdomain = (self.subdomain or "").strip().lower()
        return super().save(*args, **kwargs)

    # -- URL ---------------------------------------------------------------

    @property
    def base_url(self):
        """
        Return the API base URL for this tenant.

        Always the tenant's own host - never a shared regional host, which 404s on
        /incidents and the other ticket resources (8.1).
        """
        return f"https://{self.subdomain}.{SAMANAGE_HOST_SUFFIX}"

    # -- Credentials -------------------------------------------------------

    def set_api_token(self, plaintext):
        """Encrypt and store ``plaintext`` as the API token."""
        from dojo.utils import dojo_crypto_encrypt  # noqa: PLC0415 -- lazy import, avoids circular dependency

        self.api_token = dojo_crypto_encrypt(plaintext) or ""

    def get_api_token(self):
        """Return the decrypted API token, or an empty string if unset/undecryptable."""
        from dojo.utils import prepare_for_view  # noqa: PLC0415 -- lazy import, avoids circular dependency

        return prepare_for_view(self.api_token) or ""

    # -- Label helpers -----------------------------------------------------

    @property
    def priority_allowlist(self):
        """
        Return the set of priority labels this instance is allowed to send.

        The API returns 200 and silently keeps the old value when handed an unknown
        priority label (8.1, R9/R19), so every outbound write is checked against this
        set client-side - it is the only place a typo or a stale mapping can be caught.
        """
        values = self.severity_priority_map.values() if isinstance(self.severity_priority_map, dict) else ()
        return frozenset(value for value in values if value)

    def priority_for_severity(self, severity):
        """Return the priority label mapped to ``severity``, or ``None`` when unmapped."""
        if not isinstance(self.severity_priority_map, dict):
            return None
        return self.severity_priority_map.get(severity) or None

    def is_resolved_state(self, state_label):
        """Return whether ``state_label`` is one of this tenant's resolved/closed states."""
        if not state_label or not isinstance(self.resolved_states, list):
            return False
        return state_label in self.resolved_states

    def is_auto_push_severity(self, severity):
        """
        Return whether ``severity`` opens an incident *without* a human asking.

        This is the volume control, and it is deliberately a narrower question than
        :meth:`priority_for_severity`. ``severity_priority_map`` answers "can this
        severity ever become a ticket, and under what priority label?"; this answers
        "should a ``push_all_findings`` product fire one off on its own?". Splitting
        them is what lets an operator auto-file Critical/High while keeping Medium/Low
        reachable one finding at a time - a single SCA import of 1,500 findings is
        1,500 individual ``POST /incidents`` calls otherwise, since there is no
        batching or summarisation anywhere in this API (one finding, one incident, by
        the OneToOne on ``SolarWinds_Ticket.finding``).

        Empty is the default and means "nothing fires automatically". See the class
        docstring note on why that is not "everything in the severity map".
        """
        if not severity or not isinstance(self.auto_push_severities, list):
            return False
        return severity in self.auto_push_severities

    # -- Validation --------------------------------------------------------

    def clean(self):
        super().clean()
        from dojo.models import SEVERITIES  # noqa: PLC0415 -- lazy import, avoids circular dependency

        errors = {}

        if not isinstance(self.resolved_states, list) or any(
            not isinstance(state, str) or not state.strip() for state in self.resolved_states
        ):
            errors["resolved_states"] = _("Enter a list of non-empty workflow state labels.")

        if not isinstance(self.severity_priority_map, dict):
            errors["severity_priority_map"] = _("Enter an object mapping severities to priority labels.")
        elif self.push_enabled and not self.severity_priority_map:
            # An empty map plus push_enabled is a configuration that can never
            # succeed: client._validate_priority refuses to send an unvalidated
            # priority, so every push would raise SolarWindsConfigurationError at
            # run time. Catch it here instead of at the first finding (R22).
            errors["severity_priority_map"] = _(
                "Map at least one severity to a priority label before enabling push. "
                "With an empty map every push is refused, because the API accepts an "
                "invalid priority with a 200 and silently discards it, so there is "
                "nothing to validate an outgoing label against.",
            )
        else:
            unknown = sorted(set(self.severity_priority_map) - set(SEVERITIES))
            blank = sorted(
                key for key, value in self.severity_priority_map.items()
                if not isinstance(value, str) or not value.strip()
            )
            if unknown:
                errors["severity_priority_map"] = _(
                    "Unknown DefectDojo severities: %(keys)s. Valid severities are %(valid)s.",
                ) % {"keys": ", ".join(unknown), "valid": ", ".join(SEVERITIES)}
            elif blank:
                errors["severity_priority_map"] = _(
                    "These severities have no priority label: %(keys)s. Remove the key "
                    "entirely to skip pushing that severity.",
                ) % {"keys": ", ".join(blank)}

        # auto_push_severities must be a subset of the severity map's keys. Auto-pushing
        # a severity with no priority label is nonsensical rather than merely useless:
        # the payload builder raises UnmappedSeverityError for it, so the push is
        # silently dropped in a Celery task. Reject the combination at configuration
        # time, where someone is looking at the screen, rather than at the first finding.
        if not isinstance(self.auto_push_severities, list) or any(
            not isinstance(severity, str) or not severity.strip()
            for severity in self.auto_push_severities
        ):
            errors["auto_push_severities"] = _("Enter a list of severity names, or an empty list.")
        elif "severity_priority_map" not in errors and isinstance(self.severity_priority_map, dict):
            unmapped = sorted(set(self.auto_push_severities) - set(self.severity_priority_map))
            if unmapped:
                errors["auto_push_severities"] = _(
                    "These severities are set to auto-push but have no priority label in "
                    "the severity map: %(keys)s. Map them first, or remove them here. "
                    "Auto-push severities must be a subset of the severity map's keys.",
                ) % {"keys": ", ".join(unmapped)}

        if errors:
            raise ValidationError(errors)


class SolarWinds_Product(models.Model):

    """Per-product routing overrides, analogous to ``JIRA_Project``."""

    solarwinds_instance = models.ForeignKey(
        "dojo.SolarWinds_Instance",
        on_delete=models.CASCADE,
        related_name="solarwinds_products",
    )
    product = models.ForeignKey(
        "dojo.Product",
        on_delete=models.CASCADE,
        related_name="solarwinds_products",
    )
    site_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Overrides the instance default Site id for this product."),
    )
    department_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Overrides the instance default Department id for this product."),
    )
    group_assignee_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Support group incidents from this product are assigned to."),
    )
    push_all_findings = models.BooleanField(
        default=False,
        help_text=_("Push every mapped-severity finding, not only those pushed explicitly."),
    )

    class Meta:
        verbose_name = _("SolarWinds Product Mapping")
        verbose_name_plural = _("SolarWinds Product Mappings")
        constraints = [
            # One mapping per (instance, product): two rows would make routing
            # ambiguous with no deterministic tie-break.
            models.UniqueConstraint(
                fields=["solarwinds_instance", "product"],
                name="unique_solarwinds_instance_product",
            ),
        ]

    def __str__(self):
        return f"{self.product} -> {self.solarwinds_instance}"


class SolarWinds_Ticket(models.Model):

    """Link row between a Finding and the SolarWinds incident created from it."""

    finding = models.OneToOneField(
        "dojo.Finding",
        on_delete=models.CASCADE,
        related_name="solarwinds_ticket",
    )
    solarwinds_instance = models.ForeignKey(
        "dojo.SolarWinds_Instance",
        on_delete=models.CASCADE,
        related_name="tickets",
        null=True,
        blank=True,
        help_text=_(
            "Tenant this incident lives in. Added in Epic 3 because incident ids are "
            "only unique *within* a tenant: the inbound poll joins the incidents one "
            "instance returned against these rows by incident_id, and without this "
            "scope a second configured tenant that happened to reuse an id would "
            "mitigate the wrong finding. Nullable only so the migration is additive - "
            "every row this integration writes sets it.",
        ),
    )
    incident_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text=_("SolarWinds internal incident id, used for all API calls."),
    )
    incident_number = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Human-facing incident number shown in the Service Desk UI."),
    )
    last_synced_state = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Workflow state LABEL last seen by the inbound poll - the reconciliation cursor."),
    )
    external_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("updated_at reported by SolarWinds the last time this ticket was polled."),
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("SolarWinds Ticket")
        verbose_name_plural = _("SolarWinds Tickets")

    def __str__(self):
        return f"{self.incident_number or self.incident_id} <- Finding {self.finding_id}"
