"""
Orchestration for the SolarWinds Service Desk integration (INTEGRATIONS_ROADMAP.md 8.5/8.6).

Division of labour in this module family:

* :mod:`dojo.solarwinds.services` - pure. Finding + config -> payload dict.
* :mod:`dojo.solarwinds.client` - HTTP only. Headers, retries, pagination, the
  outgoing-priority guard.
* **this module** - everything that needs the database: routing resolution, the
  ticket link row, the inbound reconciliation, and the admin-facing alerting.
* :mod:`dojo.solarwinds.tasks` - Celery entry points, which are thin wrappers
  around the functions here so the logic stays testable synchronously.

Three constraints shape the code here and are not free to "simplify":

**R20 - the poll always sees a superset.** The API's ``Updated_at`` filter has no
arbitrary-timestamp granularity; the narrowest window available is one day. So the
same incidents come back on every 10-minute run. Reconciliation therefore decides
"did this change?" by comparing the incident's own ``state`` against the
``last_synced_state`` cursor on the link row, never from the fact that the API
returned it, and mitigating an already-mitigated finding is a checked no-op rather
than something the query is trusted to prevent.

**R6 - a rotated service-account token stops all sync silently.** Every failure
path here raises a subscribable notification (``solarwinds_update``), not just a log
line, and :func:`verify_instance_token` exists purely so the breakage is found on a
schedule instead of at the next finding.

**R22 residual - a misconfigured instance must not look like a transient failure.**
``SolarWindsConfigurationError``/``SolarWindsInvalidPriorityError`` mean a human has
to change something; they are alerted and dropped, never retried.
"""
import logging
from contextlib import suppress
from datetime import UTC

from django.urls import NoReverseMatch
from django.utils import timezone

from dojo.notifications.helper import create_notification
from dojo.solarwinds import services
from dojo.solarwinds.client import (
    SolarWindsAuthenticationError,
    SolarWindsClient,
    SolarWindsConfigurationError,
    SolarWindsError,
    SolarWindsInvalidPriorityError,
    SolarWindsRateLimitError,
)
from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product, SolarWinds_Ticket

logger = logging.getLogger(__name__)

# Username of the synthetic actor recorded as ``mitigated_by`` when the inbound poll
# closes a finding. Mirrors the JIRA webhook's own ``username="JIRA"`` actor
# (dojo/jira/helper.py process_resolution_from_jira), so "who closed this?" has the
# same shape of answer for both integrations.
SOLARWINDS_ACTOR_USERNAME = "SolarWinds"

# Poll window. Deliberately the smallest bucket the API offers rather than anything
# derived from the beat interval: per R20 a tighter window is not expressible, so
# correctness comes from the cursor comparison below and not from this number.
POLL_WINDOW_DAYS = 1

# The health check needs one authenticated round trip and nothing else. /sites is a
# config collection that resolves on every tenant (8.1) and one row is enough to
# prove the token works.
HEALTH_CHECK_COLLECTION = "sites"

# Notification event name. Backed by a real ``Notifications.solarwinds_update``
# field, so operators can subscribe to SolarWinds breakage specifically instead of
# having it land in the catch-all "other" bucket alongside everything else.
SOLARWINDS_NOTIFICATION_EVENT = "solarwinds_update"


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def log_solarwinds_alert(title, description, *, obj=None, url=None):
    """
    Raise an admin-facing notification for a SolarWinds failure.

    Uses the same mechanism JIRA's ``log_jira_alert``/``log_jira_generic_alert`` use -
    ``create_notification`` with a dedicated event - rather than a bare ``logger.error``,
    because R6's failure mode (a regenerated service-account token) produces no other
    visible symptom: pushes just stop.
    """
    logger.error("SolarWinds: %s - %s", title, description)
    if url is None and obj is not None:
        # Same courtesy log_jira_alert extends: make the alert clickable through to the
        # object it is about, so an operator is not left grepping for the finding id.
        with suppress(AttributeError, NoReverseMatch):
            url = obj.get_absolute_url()
    try:
        create_notification(
            event=SOLARWINDS_NOTIFICATION_EVENT,
            title=title[:250],
            description=description,
            icon="bullseye",
            source="SolarWinds Service Desk",
            url=url,
            obj=obj,
        )
    except Exception:
        # Never let the notification layer turn a reported failure into an
        # unreported one - the log line above has already landed.
        logger.exception("SolarWinds: failed to raise a notification for %r", title)


# ---------------------------------------------------------------------------
# Routing resolution
# ---------------------------------------------------------------------------

def get_product(finding):
    """Return the finding's Product, or ``None`` for a partially built object."""
    try:
        return finding.test.engagement.product
    except AttributeError:
        return None


def has_ticket(finding):
    """Return whether ``finding`` is already linked to a SolarWinds incident."""
    return SolarWinds_Ticket.objects.filter(finding=finding).exists()


def is_status_pushable(finding):
    """
    Return whether ``finding``'s current status warrants opening a new incident.

    Pure, no queries. Deliberately narrow: an incident is a request for someone to do
    work, so anything already dealt with (mitigated, false positive, out of scope, a
    duplicate of another finding, or explicitly risk accepted) must not create one.
    Without this gate, closing a finding would itself trigger a push, because the
    close path calls ``Finding.save()`` like every other write.

    There is intentionally **no** ``verified`` gate here, unlike JIRA's
    ``is_qualified()``. Volume control for this integration is ``severity_priority_map``
    (8.5); a second gate sourced from system settings would make "why did this finding
    not create a ticket?" materially harder to answer. See the risk register.
    """
    return bool(
        finding.active
        and not finding.is_mitigated
        and not finding.false_p
        and not finding.out_of_scope
        and not finding.duplicate
        and not finding.risk_accepted,
    )


def any_push_mapping_exists():
    """
    Return whether *any* product anywhere is mapped to a push-enabled instance.

    The cheap guard that makes calling this module from every ``Finding.save()``
    defensible. ``get_product(finding)`` walks ``test -> engagement -> product``, which
    is three queries on a finding that was not loaded with ``select_related`` (the
    single-save path does not; the batch importer already does). Every deployment that
    does not use this integration would otherwise pay that on every finding save, for
    nothing - whereas this is one query against a table that in practice holds a
    handful of rows, and it is the answer for all of them.
    """
    return SolarWinds_Product.objects.filter(solarwinds_instance__push_enabled=True).exists()


def get_mappings_for_product(product):
    """
    Return every push-eligible ``SolarWinds_Product`` row for ``product``.

    One query, ordered deterministically. ``select_related`` pulls the instance in the
    same round trip because the caller always needs its config.

    **Does not call** :func:`any_push_mapping_exists` - the caller is expected to have
    asked that already where it is the cheap short-circuit (:func:`get_product_mappings`
    below), and re-asking here would double that query on the hot save path and move
    the pinned baselines R27 documents. Call this directly only when the product is
    already in hand, as the finding forms do.
    """
    if product is None:
        return []
    return list(
        SolarWinds_Product.objects.filter(
            product=product,
            solarwinds_instance__push_enabled=True,
        )
        .select_related("solarwinds_instance")
        .order_by("solarwinds_instance__configuration_name", "id"),
    )


def get_product_mappings(finding):
    """
    Return every push-eligible ``SolarWinds_Product`` row for ``finding``'s product.

    ``any_push_mapping_exists()`` is checked **before** ``get_product(finding)`` on
    purpose: the walk to the product is three queries on a finding loaded without
    ``select_related``, and a deployment that never configured this integration must
    not pay them. See R27.
    """
    if not any_push_mapping_exists():
        return []
    return get_mappings_for_product(get_product(finding))


def is_push_all_findings(finding, *, mappings=None):
    """
    Return whether any mapping for this finding's product pushes every finding.

    The analogue of ``jira_services.is_push_all_issues()``. See
    :func:`resolve_push_target` for why this is consulted inside the save hook rather
    than at each call site the way JIRA's is.

    **Deliberately severity-blind**, unlike :func:`is_push_all_findings_auto`. This is
    a *configuration* question - "is this product wired up for automatic pushes at
    all?" - and it is used by ``post_process_findings_batch`` as a per-batch gate
    evaluated against ``findings[0]``. Making it severity-aware would mean one Low
    finding at the head of an import silently suppressed the Critical findings behind
    it. Severity is filtered per finding, inside :func:`resolve_push_target`.

    ``mappings`` is the batch escape hatch described on :func:`resolve_push_target`.
    """
    if mappings is None:
        mappings = get_product_mappings(finding)
    return any(mapping.push_all_findings for mapping in mappings)


def is_push_all_findings_auto(finding, *, mappings=None):
    """
    Return whether *this* finding would open an incident with nobody asking.

    The per-finding form of :func:`is_push_all_findings`: a ``push_all_findings``
    mapping exists **and** this finding's severity is in that instance's
    ``auto_push_severities``. Status and "already ticketed" are not consulted - this
    answers a configuration question, the same scope as JIRA's ``is_push_all_issues``.

    Used by the finding forms to show the manual checkbox as already-checked-and-
    disabled when the push is going to happen regardless, so the operator is not
    offered a choice they do not have.
    """
    return resolve_push_target(finding, push_requested=False, mappings=mappings) is not None


def is_manual_push_available(finding, *, mappings=None):
    """
    Return whether a human could meaningfully ask for this finding to be pushed now.

    True when the product has a push-enabled mapping **and** the finding's severity has
    a priority label in that instance's ``severity_priority_map`` - the eligibility
    rule for a manual push, which is looser than the auto one by exactly
    ``auto_push_severities``.

    This is what decides whether the checkbox appears at all. A checkbox that is always
    rendered and silently does nothing for an unmapped severity or an unconfigured
    product is worse than no checkbox: it reports a capability that is not there.
    """
    target = resolve_push_target(finding, push_requested=True, mappings=mappings)
    if target is None:
        return False
    instance, _mapping = target
    return services.should_push_finding(finding, instance)


def resolve_push_target(finding, *, push_requested=False, mappings=None):
    """
    Return the ``(instance, mapping)`` this finding should be pushed to, or ``None``.

    ``push_requested`` is the explicit per-finding trigger, and it is the whole of the
    auto/manual split:

    * **explicit request** - any mapping is a candidate, and the severity only has to
      be in ``severity_priority_map`` (checked downstream by
      ``services.should_push_finding``). A human looking at one finding and choosing to
      file it is the authority on whether it is worth a ticket.
    * **no request** (the ``push_all_findings`` config-driven mode) - the mapping must
      have ``push_all_findings`` set *and* the finding's severity must be listed in
      that instance's ``auto_push_severities``. Being merely mapped to a priority label
      is not enough. This is the volume control: without it, one SCA import of 1,500
      Critical-through-Low findings is 1,500 ``POST /incidents`` calls, since nothing
      in this API batches or summarises.

    ``mappings`` lets a caller that already resolved this product's mappings pass them
    in instead of having them re-queried. It exists for
    ``post_process_findings_batch``, where every finding in the batch belongs to the
    same product: without it a 5,000-finding import re-ran the same mapping lookup
    5,000 times. An empty list is a meaningful value ("resolved, and there are none"),
    so ``None`` - not falsiness - is what triggers a fresh lookup. Callers that cannot
    guarantee one product per batch must leave it unset. Note that ``mappings`` can be
    hoisted across a batch but the severity test below cannot: it is per finding.

    A product can legitimately carry a mapping on several instances (the uniqueness
    constraint is per instance+product), but ``SolarWinds_Ticket.finding`` is a
    OneToOne, so a finding can only ever hold one incident. Rather than silently
    picking whichever row the database returned first, pick the first in a stable
    order and say so in the log.
    """
    if mappings is None:
        mappings = get_product_mappings(finding)
    candidates = [
        mapping for mapping in mappings
        if push_requested or (
            mapping.push_all_findings
            and mapping.solarwinds_instance.is_auto_push_severity(finding.severity)
        )
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "Finding %s maps to %s push-enabled SolarWinds instances (%s); a finding can "
            "hold only one ticket, so pushing to '%s'. Remove the surplus product "
            "mappings to make this deterministic by configuration rather than by order.",
            finding.id,
            len(candidates),
            ", ".join(str(mapping.solarwinds_instance) for mapping in candidates),
            candidates[0].solarwinds_instance,
        )
    mapping = candidates[0]
    return mapping.solarwinds_instance, mapping


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

def push_finding(finding, *, push_requested=False, mappings=None, **kwargs):
    """
    Dispatch an asynchronous incident creation for ``finding``, if it is eligible.

    The gating here runs in the caller's (request) thread, so it is kept to at most two
    cheap queries and short-circuits before either of them whenever it can: a
    deployment with no SolarWinds product mappings pays one indexed lookup per finding
    save and dispatches nothing.

    ``mappings``, when given, is this product's already-resolved mapping rows (see
    :func:`resolve_push_target`) and removes the routing lookup from this call
    entirely. It is consumed here and deliberately **not** forwarded to the task:
    model instances are not a sane Celery argument, and ``push_finding_internal``
    re-resolves everything against fresh database state by design anyway. The
    genuinely per-finding checks below (:func:`is_status_pushable`,
    :func:`has_ticket`) differ finding by finding and are never hoisted.

    Returns the Celery ``AsyncResult`` when a task was dispatched, else ``None``.
    """
    if finding is None or finding.id is None:
        return None
    if not is_status_pushable(finding):
        logger.debug("SolarWinds: finding %s status is not pushable, skipping", finding.id)
        return None
    if resolve_push_target(finding, push_requested=push_requested, mappings=mappings) is None:
        return None
    if has_ticket(finding):
        logger.debug("SolarWinds: finding %s already has a ticket, skipping", finding.id)
        return None

    from dojo.celery_dispatch import dojo_dispatch_task  # noqa: PLC0415 -- lazy import, avoids circular dependency
    from dojo.solarwinds import tasks  # noqa: PLC0415 -- lazy import, avoids circular dependency

    logger.debug("SolarWinds: dispatching push for finding %s", finding.id)
    return dojo_dispatch_task(
        tasks.push_finding_to_solarwinds,
        finding.id,
        push_requested=push_requested,
        **kwargs,
    )


def push_finding_explicitly(finding, *, mappings=None):
    """
    Dispatch an explicitly-requested push from a caller that saved the finding already.

    For the REST API, where ``ModelSerializer.update()`` calls ``instance.save()``
    itself and there is no seam to thread ``push_to_solarwinds`` through the way the UI
    views do. Those views pass the flag *into* ``Finding.save()``; this is the same
    trigger, applied one step later.

    **The auto-push guard is the point of this function, not a tidy-up.** The save that
    already happened ran the post-save hook, which resolves ``push_all_findings``
    itself - so for a finding that auto-pushes, a task is already in flight. Dispatching
    a second one would have two workers pass ``push_finding_internal``'s "does this
    finding have a ticket yet?" check before either writes the link row, and create two
    incidents in the tenant: R25's race, manufactured deliberately rather than suffered
    by chance. When the finding would auto-push, the explicit request is redundant by
    construction and is dropped.

    Returns the Celery ``AsyncResult`` when a task was dispatched, else ``None``.
    """
    if finding is None or finding.id is None:
        return None
    if is_push_all_findings_auto(finding, mappings=mappings):
        logger.debug(
            "SolarWinds: finding %s auto-pushes already; not dispatching the explicit "
            "request a second time", finding.id,
        )
        return None
    return push_finding(finding, push_requested=True, mappings=mappings)


def push_finding_internal(finding, *, push_requested=False, client=None):
    """
    Create the SolarWinds incident for ``finding`` and write the link row.

    Runs inside the Celery task, so every check is repeated against fresh database
    state: the gating in :func:`push_finding` happened in a different transaction and
    the finding may have been closed, deleted or already ticketed since.

    Returns the created :class:`~dojo.solarwinds.models.SolarWinds_Ticket`, or ``None``
    when the push was deliberately skipped. Raises only client errors, which the task
    translates into alerts and (for rate limits) retries.
    """
    target = resolve_push_target(finding, push_requested=push_requested)
    if target is None:
        logger.debug("SolarWinds: no push target for finding %s any more, skipping", finding.id)
        return None
    instance, mapping = target

    if not is_status_pushable(finding):
        logger.debug("SolarWinds: finding %s is no longer pushable, skipping", finding.id)
        return None

    # The "never double-create" guard. Checked here rather than relying on the
    # OneToOne constraint, because the constraint would only fire *after* the incident
    # had already been created in the tenant.
    if has_ticket(finding):
        logger.debug("SolarWinds: finding %s already has a ticket, skipping", finding.id)
        return None

    if not services.should_push_finding(finding, instance):
        # push_enabled off, or the severity is unmapped. Both are intentional
        # configuration outcomes (8.5/R22), not failures.
        logger.debug(
            "SolarWinds: finding %s (%s) is not eligible for instance '%s', skipping",
            finding.id, finding.severity, instance,
        )
        return None

    try:
        payload = services.build_incident_payload(finding, instance, solarwinds_product=mapping)
    except services.UnmappedSeverityError as exc:
        # Belt and braces: should_push_finding() already covers this, but the builder
        # is the authority and this must stay a silent skip, never an error (8.5).
        logger.debug("SolarWinds: %s", exc)
        return None

    owns_client = client is None
    client = client or SolarWindsClient(instance)
    try:
        created = client.create_incident(payload)
        # Normalise to a string before testing, so the check is "did we get
        # characters?" rather than a truthiness test on the raw value: an id of 0 is
        # falsy, and dropping the link to a real incident is the worst outcome here.
        raw_id = _incident_field(created, "id")
        incident_id = "" if raw_id is None else str(raw_id).strip()
        if not incident_id:
            msg = (
                f"SolarWinds accepted the incident for finding {finding.id} but returned no "
                f"id, so the ticket cannot be linked or ever reconciled: {created!r:.500}"
            )
            raise SolarWindsError(msg)

        ticket, was_created = SolarWinds_Ticket.objects.get_or_create(
            finding=finding,
            defaults={
                "solarwinds_instance": instance,
                "incident_id": str(incident_id),
                "incident_number": str(_incident_field(created, "number") or ""),
                # Seed the reconciliation cursor with the state we just asked for, so
                # the first poll does not read "New" as a change worth acting on.
                "last_synced_state": instance.open_state,
                "external_updated_at": timezone.now(),
            },
        )
        if not was_created:
            # Another worker linked this finding between our check and this write. The
            # incident we just created is now orphaned in the tenant and no code will
            # ever touch it again, so a human has to close it.
            log_solarwinds_alert(
                f"Duplicate SolarWinds incident created for finding {finding.id}",
                f"Finding {finding.id} was already linked to incident {ticket.incident_id} by "
                f"a concurrent sync, so incident {incident_id} (just created in "
                f"'{instance}') is orphaned and needs closing manually.",
                obj=finding,
            )
            return ticket

        _post_correlation_comment(client, finding, ticket, instance)
        logger.info(
            "SolarWinds: created incident %s (%s) in '%s' for finding %s",
            ticket.incident_id, ticket.incident_number or "-", instance, finding.id,
        )
        return ticket
    finally:
        if owns_client:
            client.close()


def _post_correlation_comment(client, finding, ticket, instance):
    """
    Post the private audit comment, without letting its failure undo the link row.

    The incident exists and the link row is written by this point. A failed comment is
    a missing audit note, not a reason to retry the whole push and create a second
    incident - so it is alerted and swallowed here rather than propagated.
    """
    try:
        client.add_comment(
            ticket.incident_id,
            services.build_correlation_comment(finding),
            is_private=True,
        )
    except SolarWindsError:
        logger.exception(
            "SolarWinds: incident %s for finding %s was created but its correlation "
            "comment failed", ticket.incident_id, finding.id,
        )
        log_solarwinds_alert(
            f"SolarWinds correlation comment failed for incident {ticket.incident_id}",
            f"Incident {ticket.incident_id} in '{instance}' was created for finding "
            f"{finding.id} and is linked, but the private correlation comment could not "
            f"be posted. The ticket has no back-link to DefectDojo in its comments.",
            obj=finding,
        )


def _incident_field(body, name):
    """Read one field off a create/get incident response, tolerating either envelope."""
    if not isinstance(body, dict):
        return None
    incident = body.get("incident")
    if isinstance(incident, dict) and name in incident:
        return incident.get(name)
    return body.get(name)


# ---------------------------------------------------------------------------
# Inbound reconciliation
# ---------------------------------------------------------------------------

class ReconcileSummary:

    """Counters from one reconciliation pass, for logging and for assertions in tests."""

    def __init__(self):
        self.seen = 0
        self.matched = 0
        self.mitigated = 0
        self.reactivated = 0
        self.cursor_advanced = 0
        self.skipped = 0

    def __repr__(self):
        return (
            f"<ReconcileSummary seen={self.seen} matched={self.matched} "
            f"mitigated={self.mitigated} reactivated={self.reactivated} "
            f"cursor_advanced={self.cursor_advanced} skipped={self.skipped}>"
        )


def reconcile_resolved_incidents(instance, *, client=None, days=POLL_WINDOW_DAYS):
    """
    Pull recently updated incidents for ``instance`` and reconcile them into findings.

    Idempotent by construction, which R20 makes mandatory rather than merely tidy: the
    API cannot be asked for "changed in the last ten minutes", so every run re-reads
    the same day's incidents. Whether an incident actually changed is decided by
    comparing its ``state`` to the ``last_synced_state`` cursor on the link row.

    Both directions across the resolved boundary are reconciled, not just resolution -
    see :func:`_reconcile_one`. The name is kept for the beat entry it backs.

    One batched, filtered, paginated query per instance - never a GET per ticket (R7).
    """
    summary = ReconcileSummary()
    tickets = {
        ticket.incident_id: ticket
        for ticket in SolarWinds_Ticket.objects.filter(
            solarwinds_instance=instance,
        ).select_related("finding")
    }
    if not tickets:
        logger.debug("SolarWinds: instance '%s' has no linked tickets, nothing to poll", instance)
        return summary

    owns_client = client is None
    client = client or SolarWindsClient(instance)
    try:
        incidents = client.list_incidents_updated_since(days=days)
    finally:
        if owns_client:
            client.close()

    for incident in incidents:
        summary.seen += 1
        if not isinstance(incident, dict):
            continue
        incident_id = incident.get("id")
        ticket = tickets.get(str(incident_id)) if incident_id is not None else None
        if ticket is None:
            # Expected and frequent: the day-wide window returns the tenant's whole
            # day of incident activity, most of which DefectDojo never created.
            continue
        summary.matched += 1
        _reconcile_one(instance, ticket, incident, summary)

    logger.info("SolarWinds: reconciled instance '%s': %r", instance, summary)
    return summary


def _reconcile_one(instance, ticket, incident, summary):
    """
    Apply one incident's state to its linked finding, then advance the cursor.

    A ticket's life is not one-way. Both directions across the resolved boundary are
    acted on, decided purely from the cursor:

    * not-resolved -> resolved: mitigate the finding.
    * resolved -> not-resolved: a technician reopened the incident for more work, so
      reactivate the finding. Without this the cursor still moved to record the reopen,
      and when the incident was resolved a second time ``_mitigate_from_incident``
      short-circuited on ``finding.is_mitigated`` - leaving the finding permanently
      closed while the ticket carried on, with only a debug line to show for it.
    * no crossing: cursor only.
    """
    state = _state_label(incident.get("state"))
    state_changed = state != ticket.last_synced_state
    is_resolved = bool(state) and instance.is_resolved_state(state)
    was_resolved = bool(ticket.last_synced_state) and instance.is_resolved_state(ticket.last_synced_state)

    if is_resolved and state_changed:
        if _mitigate_from_incident(instance, ticket, incident, state):
            summary.mitigated += 1
        else:
            summary.skipped += 1
    elif is_resolved:
        # Already recorded this exact resolved state on a previous run. This is the
        # common case under R20's superset behaviour and must stay a no-op: acting
        # here would re-close the finding and add a second note on every poll.
        logger.debug(
            "SolarWinds: incident %s is still '%s', already recorded; no-op",
            ticket.incident_id, state,
        )
    elif state and state_changed and was_resolved:
        if _reactivate_from_incident(instance, ticket, incident, state):
            summary.reactivated += 1
        else:
            summary.skipped += 1

    if _advance_cursor(ticket, state, incident):
        summary.cursor_advanced += 1


def _mitigate_from_incident(instance, ticket, incident, state):
    """
    Mitigate ``ticket.finding`` because its incident reached a resolved state.

    Returns whether the finding was actually closed.

    Goes through ``finding_helper.close_finding`` rather than setting ``active`` /
    ``is_mitigated`` / ``mitigated`` by hand. That helper is the canonical close path
    and additionally handles the endpoint/location statuses, risk-acceptance removal,
    the external-issue close, the JIRA status sync and the ``finding_closed``
    notification - all of which a hand-rolled version would silently skip. The JIRA
    webhook's own inbound path predates it and does set the fields by hand; that is
    not the pattern to copy.

    The human verdict wins: a finding a person marked false positive or out of scope is
    left alone, and its existing ``false_p``/``out_of_scope``/``duplicate`` flags are
    passed straight back into ``close_finding`` so the close cannot clear them.
    """
    from dojo.finding import helper as finding_helper  # noqa: PLC0415 -- lazy import, avoids circular dependency

    finding = ticket.finding

    if finding.is_mitigated:
        # Second idempotency layer, independent of the cursor. Protects the case where
        # the cursor was lost or the finding was closed through the UI in between.
        logger.debug(
            "SolarWinds: finding %s is already mitigated; incident %s needs no action",
            finding.id, ticket.incident_id,
        )
        return False

    if finding.false_p or finding.out_of_scope:
        logger.info(
            "SolarWinds: incident %s reached '%s' but finding %s is marked "
            "false positive/out of scope; leaving the human verdict in place",
            ticket.incident_id, state, finding.id,
        )
        return False

    mitigated_at = _parse_timestamp(incident.get("updated_at")) or timezone.now()
    actor = get_solarwinds_actor()

    finding_helper.close_finding(
        finding=finding,
        user=actor,
        is_mitigated=True,
        mitigated=mitigated_at,
        mitigated_by=actor,
        # Pass the current values through rather than hardcoding False: close_finding
        # assigns these unconditionally, so hardcoding would quietly clear a flag a
        # human set.
        false_p=finding.false_p,
        out_of_scope=finding.out_of_scope,
        duplicate=finding.duplicate,
        note_entry=(
            f"Mitigated automatically: SolarWinds Service Desk incident "
            f"{ticket.incident_number or ticket.incident_id} reached state '{state}' "
            f"in '{instance}'."
        ),
    )
    logger.info(
        "SolarWinds: mitigated finding %s from incident %s reaching '%s'",
        finding.id, ticket.incident_id, state,
    )
    return True


def _reactivate_from_incident(instance, ticket, incident, state):
    """
    Reactivate ``ticket.finding`` because its incident left the resolved states.

    Returns whether the finding was actually reopened.

    **Only reverses this integration's own closures.** The guard is
    ``finding.mitigated_by``: :func:`_mitigate_from_incident` records
    :func:`get_solarwinds_actor` there, so that actor's name is the evidence that the
    close being undone is the one the poll made. A finding a *person* closed - after
    verifying the issue was genuinely fixed, for reasons that need have nothing to do
    with the ticket - is left exactly as it is, and only the cursor moves. That is the
    same "the human verdict wins" rule ``_mitigate_from_incident`` already applies to
    ``false_p``/``out_of_scope``, applied in the other direction: this integration may
    undo itself and nothing else.

    Note what that rule costs, deliberately: if an operator closes a finding by hand
    while its ticket is still open, the ticket's later reopen will not reactivate it.
    Silently doing nothing to a human's finding is the safer of the two failure modes -
    the alternative is an automation overruling a verdict it has no way to evaluate.

    Goes through ``finding_helper.reopen_finding`` for the same reason the mitigation
    goes through ``close_finding``: endpoint/location statuses, risk-acceptance removal,
    the external-issue reopen and the JIRA sync all have to happen too, and a
    hand-rolled ``active=True`` would skip every one of them.
    """
    from dojo.finding import helper as finding_helper  # noqa: PLC0415 -- lazy import, avoids circular dependency

    finding = ticket.finding

    if not finding.is_mitigated and finding.active:
        # Nothing to undo. Reached whenever a human reopened the finding first, or the
        # cursor was seeded resolved on a finding that was never closed.
        logger.debug(
            "SolarWinds: incident %s reopened as '%s' but finding %s is already open; no-op",
            ticket.incident_id, state, finding.id,
        )
        return False

    if finding.false_p or finding.out_of_scope:
        logger.info(
            "SolarWinds: incident %s reopened as '%s' but finding %s is marked "
            "false positive/out of scope; leaving the human verdict in place",
            ticket.incident_id, state, finding.id,
        )
        return False

    actor = get_solarwinds_actor()
    if finding.mitigated_by_id != actor.id:
        logger.info(
            "SolarWinds: incident %s reopened as '%s', but finding %s was closed by "
            "'%s', not by this integration; leaving it closed and advancing the cursor "
            "only",
            ticket.incident_id, state, finding.id, finding.mitigated_by or "an unrecorded user",
        )
        return False

    finding_helper.reopen_finding(
        finding=finding,
        user=actor,
        note_entry=(
            f"Reactivated automatically: SolarWinds Service Desk incident "
            f"{ticket.incident_number or ticket.incident_id} was reopened as state "
            f"'{state}' in '{instance}'."
        ),
    )
    logger.info(
        "SolarWinds: reactivated finding %s from incident %s reopening as '%s'",
        finding.id, ticket.incident_id, state,
    )
    return True


def _advance_cursor(ticket, state, incident):
    """
    Record the state and timestamp last seen for ``ticket``, if either moved.

    The cursor advances for *every* observed state, not just resolved ones, so the
    next run can tell an unchanged ticket from a changed one instead of re-evaluating
    it from scratch. Nothing is written when nothing moved: under R20 the same
    incidents reappear every ten minutes, and blind saves would be a write per ticket
    per poll forever.
    """
    external_updated_at = _parse_timestamp(incident.get("updated_at"))
    updates = {}
    if state is not None and state != ticket.last_synced_state:
        updates["last_synced_state"] = state[:64]
    if external_updated_at is not None and external_updated_at != ticket.external_updated_at:
        updates["external_updated_at"] = external_updated_at

    if not updates:
        return False
    for field, value in updates.items():
        setattr(ticket, field, value)
    ticket.save(update_fields=[*updates])
    return True


def _state_label(value):
    """
    Normalise the incident's ``state`` into a label string.

    States write as bare labels (8.1) and read back the same way, but a nested
    ``{"name": ...}`` shape is tolerated: ``category`` is nested on this API, so the
    asymmetry is real and a layout change here would otherwise reconcile nothing at
    all, silently.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() or None if isinstance(name, str) else None
    return None


def _parse_timestamp(value):
    """Parse an API timestamp into an aware datetime, or ``None`` if unusable."""
    if not value or not isinstance(value, str):
        return None
    from django.utils.dateparse import parse_datetime  # noqa: PLC0415 -- lazy import, keeps module import cheap

    try:
        parsed = parse_datetime(value)
    except ValueError:
        logger.debug("SolarWinds sent an unparseable timestamp: %r", value)
        return None
    if parsed is None:
        logger.debug("SolarWinds sent an unrecognised timestamp format: %r", value)
        return None
    if timezone.is_naive(parsed):
        # Assume UTC rather than the local zone: this is a wire timestamp from another
        # system, and TIME_ZONE is a display preference of this one.
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


def get_solarwinds_actor():
    """
    Return (creating if needed) the synthetic user credited with automated closures.

    Mirrors the JIRA webhook's ``username="JIRA"`` actor. Created with an unusable
    password so the row cannot be logged into - the JIRA precedent leaves the password
    empty, which is a weaker default for no benefit.
    """
    from dojo.models import Dojo_User  # noqa: PLC0415 -- lazy import, avoids circular dependency

    actor, created = Dojo_User.objects.get_or_create(
        username=SOLARWINDS_ACTOR_USERNAME,
        defaults={"first_name": "SolarWinds", "last_name": "Service Desk"},
    )
    if created:
        actor.set_unusable_password()
        actor.save(update_fields=["password"])
    return actor


# ---------------------------------------------------------------------------
# Health check (R6)
# ---------------------------------------------------------------------------

def verify_instance_token(instance, *, client=None):
    """
    Make one cheap authenticated call and return whether the credentials still work.

    This closes R6. A regenerated service-account token produces no symptom other than
    sync quietly stopping - outbound pushes fail in a background task and the poll
    finds nothing - so the only way an operator learns about it in reasonable time is a
    scheduled probe that alerts.

    Returns ``True`` when the token authenticated. Auth failures and configuration
    problems return ``False`` after alerting; any other API error is alerted and also
    returns ``False``, because an instance we cannot reach is not an instance we can
    claim is healthy.
    """
    owns_client = client is None
    client = client or SolarWindsClient(instance)
    try:
        client.list_collection(HEALTH_CHECK_COLLECTION, per_page=1, max_pages=1)
    except SolarWindsAuthenticationError as exc:
        log_solarwinds_alert(
            f"SolarWinds token rejected for '{instance}'",
            f"The API rejected the service-account credentials for SolarWinds instance "
            f"'{instance}' ({instance.base_url}). All ticket push and resolution sync for "
            f"this tenant is stopped until the token is replaced. Regenerating a token "
            f"invalidates every prior token for that user, so this is the expected symptom "
            f"of someone rotating the service account's token. Details: {exc}",
        )
        return False
    except SolarWindsConfigurationError as exc:
        log_solarwinds_alert(
            f"SolarWinds instance '{instance}' is misconfigured",
            f"The health check could not run against '{instance}': {exc}",
        )
        return False
    except SolarWindsError as exc:
        log_solarwinds_alert(
            f"SolarWinds health check failed for '{instance}'",
            f"The health check call to '{instance}' ({instance.base_url}) failed: {exc}. "
            f"The token may still be valid; the tenant may be unreachable or rate limiting.",
        )
        return False
    else:
        logger.debug("SolarWinds: instance '%s' authenticated successfully", instance)
        return True
    finally:
        if owns_client:
            client.close()


def instances_needing_health_check():
    """Return every instance that has sync switched on in either direction."""
    from django.db.models import Q  # noqa: PLC0415 -- lazy import, keeps module import cheap

    return SolarWinds_Instance.objects.filter(Q(push_enabled=True) | Q(poll_enabled=True))


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Failures a retry cannot fix: they need a configuration change or a new token. The
# client already retries 429 and transient 5xx at the HTTP layer with backoff, so the
# Celery task only ever needs to decide "is another attempt pointless?".
PERMANENT_ERRORS = (
    SolarWindsAuthenticationError,
    SolarWindsConfigurationError,
    SolarWindsInvalidPriorityError,
)

# Worth another attempt on a longer timescale than the client's in-request budget
# (4 attempts, capped at 60s of backoff) can cover.
TRANSIENT_ERRORS = (SolarWindsRateLimitError,)
