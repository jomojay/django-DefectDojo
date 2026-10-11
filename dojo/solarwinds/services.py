"""
Business logic for the SolarWinds Service Desk integration.

Everything here is pure: it turns a ``Finding`` plus configuration into a payload
dict, and never talks to the network or to HTTP request/response objects. The
Celery hook and the client own those concerns.

The field mapping is INTEGRATIONS_ROADMAP.md 8.5. Three shapes on the wire matter
and were each confirmed separately against a live tenant on 2026-09-18 - they do
*not* follow one uniform rule, so do not "tidy" them into consistency:

* ``state`` - a bare label string, and the key is ``state``, not the ``state_id``
  the OpenAPI spec documents. Numeric ids are rejected with a 422.
* ``priority`` - also a bare label string. An invalid one is accepted with a 200 and
  silently discarded, which is why ``client.py`` validates it before sending.
* ``category`` - a nested ``{"name": ...}`` object, exactly as the spec says. This
  one field does match the spec.
* ``assignee`` - a nested ``{"email": ...}`` object (spec-confirmed: ``anyOf`` of an
  object requiring ``email``, or an explicit nil). Same *family* as ``category``, but
  a different key - ``name`` identifies a category, an email identifies a person.
  Sourced from ``Product.technical_contact`` and omitted when there is not one; it is
  independent of ``group_assignee_id``, since a person and a support group are
  separate fields on a Samanage incident and not alternatives.

The other rule that matters is the **skip-on-unmapped-severity** decision: a finding
whose severity has no entry in ``severity_priority_map`` is not pushed at all. It is
not downgraded to a fallback priority and it does not create a ticket. That was a
deliberate call to keep Info findings out of the triage queue, and it generalises -
any severity a tenant has not mapped is skipped by the same rule.
"""
import logging

logger = logging.getLogger(__name__)

# SolarWinds truncates an over-long incident name with an ellipsis; truncate on our
# side instead so what we store and what the tenant shows agree.
INCIDENT_NAME_MAX_LENGTH = 255
INCIDENT_NAME_PREFIX = "[DefectDojo] "

# Tag applied to every incident this integration creates, so the tenant can filter
# DefectDojo-originated tickets without joining back to our database.
DEFECTDOJO_TAG = "defectdojo"


class SolarWindsSkipError(Exception):

    """Base class for 'this finding should not be pushed' conditions."""


class UnmappedSeverityError(SolarWindsSkipError):

    """
    The finding's severity has no entry in ``severity_priority_map``.

    Callers must treat this as "skip silently", not as an error to retry or to work
    around with a default priority (8.5).
    """


def is_severity_mapped(instance, severity):
    """Return whether ``severity`` has a priority label configured on ``instance``."""
    return instance.priority_for_severity(severity) is not None


def should_push_finding(finding, instance):
    """
    Return whether ``finding`` is eligible to be pushed to ``instance``.

    Eligibility here is only about configuration and severity mapping. Per-product
    routing, duplicate suppression and the "already has a ticket" check belong to the
    Celery hook, which owns database state.
    """
    if not instance.push_enabled:
        return False
    return is_severity_mapped(instance, finding.severity)


def build_incident_description(finding):
    """Build the incident body: the finding's own description, its severity, and a deep link."""
    from dojo.utils import get_full_url  # noqa: PLC0415 -- lazy import, avoids circular dependency

    lines = [
        f"Severity: {finding.severity}",
        f"DefectDojo finding: {get_full_url(finding.get_absolute_url())}",
    ]
    product = _product_for(finding)
    if product is not None:
        lines.insert(0, f"Product: {product}")

    description = (finding.description or "").strip()
    if description:
        lines.extend(["", description])
    return "\n".join(lines)


def build_incident_name(finding):
    """Build the incident title, truncated to the API's 255-char limit."""
    return f"{INCIDENT_NAME_PREFIX}{finding.title}"[:INCIDENT_NAME_MAX_LENGTH]


def build_tag_list(finding):
    """Build the comma-separated tag string linking the incident back to the finding."""
    return f"{DEFECTDOJO_TAG},finding-{finding.id}"


def build_incident_payload(finding, instance, solarwinds_product=None):
    """
    Build the ``{"incident": {...}}`` payload for ``finding``.

    Raises :class:`UnmappedSeverityError` when the finding's severity has no priority
    mapping - the caller must skip the push rather than substitute a default.

    ``solarwinds_product`` is the optional per-product routing row; its non-empty
    fields override the instance defaults.
    """
    priority = instance.priority_for_severity(finding.severity)
    if priority is None:
        msg = (
            f"Severity {finding.severity!r} has no entry in the severity_priority_map of "
            f"SolarWinds instance '{instance}'; skipping push for finding {finding.id}."
        )
        raise UnmappedSeverityError(msg)

    incident = {
        "name": build_incident_name(finding),
        "description": build_incident_description(finding),
        # Bare label, key is "state" not "state_id" - see the module docstring.
        "state": instance.open_state,
        # Bare label. client.py validates it against the instance allowlist.
        "priority": priority,
        "tag_list": build_tag_list(finding),
    }

    if instance.default_category:
        # Nested object, unlike state/priority. Confirmed live.
        incident["category"] = {"name": instance.default_category}

    assignee_email = technical_contact_email(finding)
    if assignee_email:
        # Nested object keyed on "email", the same shape family as `category` (keyed on
        # "name") and unlike the bare labels `state`/`priority` take. Verified against
        # resolved_schema.json rather than assumed: POST /incidents' request body types
        # `assignee` as `anyOf[{required: [email], properties: {email: string/email}},
        # enum[nil, "", null]]`.
        #
        # Independent of `group_assignee_id` below - both may be present on one
        # incident. Samanage models a person and a support group as separate fields
        # (`assignee`/`assignee_id` vs `group_assignee`/`group_assignee_id`), so
        # neither overrides the other and they are deliberately not mutually exclusive
        # here. Omitted entirely when there is no technical contact or no email, the
        # same optional-field rule site_id/department_id/group_assignee_id follow: the
        # schema does accept an explicit null, but sending one would *clear* an
        # assignee rather than leave the tenant's own default alone.
        incident["assignee"] = {"email": assignee_email}

    site_id = _override(solarwinds_product, "site_id", instance.default_site_id)
    if site_id:
        incident["site_id"] = site_id

    department_id = _override(solarwinds_product, "department_id", instance.default_department_id)
    if department_id:
        incident["department_id"] = department_id

    group_assignee_id = _override(solarwinds_product, "group_assignee_id", "")
    if group_assignee_id:
        incident["group_assignee_id"] = group_assignee_id

    return {"incident": incident}


def build_correlation_comment(finding):
    """
    Build the private audit comment posted right after an incident is created.

    Kept separate from the incident description so the link survives even if someone
    edits the description in the Service Desk UI.
    """
    from dojo.utils import get_full_url  # noqa: PLC0415 -- lazy import, avoids circular dependency

    return (
        f"Created from DefectDojo finding {finding.id} "
        f"({finding.severity}): {get_full_url(finding.get_absolute_url())}"
    )


def technical_contact_email(finding):
    """
    Return the email of the finding's product technical contact, or ``""``.

    ``Product.technical_contact`` is a nullable FK to ``Dojo_User``, and a ``Dojo_User``
    can exist with a blank email, so both steps can come up empty independently and
    neither is an error - the field is simply omitted from the payload.

    Reads the relation rather than taking it as an argument because the only caller runs
    inside the Celery task, where the extra query is free. Do not hoist this into the
    request path without checking the pinned query-count baselines
    (``unittests/test_importers_performance.py``, R27).
    """
    product = _product_for(finding)
    if product is None:
        return ""
    contact = getattr(product, "technical_contact", None)
    return (getattr(contact, "email", "") or "").strip()


def _override(solarwinds_product, field, default):
    """Return the product-level value for ``field`` when set, else ``default``."""
    if solarwinds_product is None:
        return default
    return getattr(solarwinds_product, field, "") or default


def _product_for(finding):
    """Return the finding's Product, tolerating partially built objects in tests."""
    try:
        return finding.test.engagement.product
    except AttributeError:
        return None
