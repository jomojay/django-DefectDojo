"""
Config-time tenant discovery for the SolarWinds Service Desk integration.

This module exists because of a gap in the API, documented in
INTEGRATIONS_ROADMAP.md 8.1 and confirmed against a live tenant on 2026-09-18:
four of the values an admin has to type into ``SolarWinds_Instance`` are
tenant-defined free text with **no server-side safety net**.

* ``open_state`` / ``resolved_states`` - workflow state LABELS. A wrong one is at
  least rejected with a 422 when the outbound push runs, so the failure is loud
  but late.
* ``severity_priority_map`` values - priority LABELS. A wrong one is **not**
  rejected: the API returns 200 and silently keeps the previous priority
  (R9/R19). Nothing downstream will ever tell the admin they typed it wrong.
* ``default_category`` - a category LABEL.

There is no ``/states`` or ``/priorities`` lookup endpoint (both 404), so the two
groups of values have fundamentally different discoverability, and this module
keeps that distinction explicit rather than papering over it:

**Exhaustive** - :func:`discover_collections` calls the real lookup endpoints for
sites, departments, categories and groups. What comes back is what the tenant
has, full stop.

**Observed, NOT exhaustive** - :func:`observe_incident_labels` samples existing
incidents and reports the distinct ``state`` / ``priority`` strings it saw. A
tenant whose incidents are all still "New" will not show "Resolved" here even
though "Resolved" is perfectly valid, and a tenant with no incidents at all
returns nothing. These are a typo-reducing hint, never an authoritative list, and
the model fields must stay free text. Anything rendering this **must** carry
:data:`OBSERVED_LABELS_CAVEAT` (or equivalent wording) next to it.

Everything here is callable before the instance row exists - see
:func:`build_probe_instance` - so a "test connection" action on the create form
works with nothing but a subdomain and a token. API failures come back inside
:class:`DiscoveryResult` rather than as exceptions, because the caller is a
config form: an admin who fat-fingers a subdomain should get a field-level error,
not a 500.
"""
import logging
from dataclasses import dataclass, field

from dojo.solarwinds.client import (
    COLLECTION_PATHS,
    SolarWindsClient,
    SolarWindsError,
)
from dojo.solarwinds.models import SolarWinds_Instance

logger = logging.getLogger(__name__)

# Sampling window for the label observation. The Updated_at filter has no
# arbitrary-timestamp granularity (R20), so this is one of the fixed buckets; the
# widest one gives the best chance of seeing a resolved/closed incident, which is
# exactly the label an admin most needs and least often has a fresh example of.
OBSERVED_SAMPLE_DAYS = 90
OBSERVED_SAMPLE_PER_PAGE = 100
# One page is plenty to enumerate the handful of distinct labels a tenant uses,
# and keeps a config-form action from walking a busy tenant's whole quarter (R7).
OBSERVED_SAMPLE_MAX_PAGES = 1

OBSERVED_LABELS_CAVEAT = (
    f"Observed on existing incidents in the last {OBSERVED_SAMPLE_DAYS} days - NOT a complete list. "
    "This API has no state or priority lookup endpoint, so a label that no current "
    "incident happens to use cannot be discovered and will not appear here. Confirm "
    "the full set in the Service Desk UI (Setup > Service Desk) before relying on it."
)


@dataclass
class DiscoveryResult:

    """
    What a tenant probe found.

    ``collections`` values are exhaustive; ``observed_states`` /
    ``observed_priorities`` are not - see the module docstring. ``errors`` maps a
    section name ("sites", "incidents", ...) to a human-readable failure, so a
    partial result is still useful: a token that can read incidents but not groups
    should still show the incident labels.
    """

    collections: dict = field(default_factory=dict)
    observed_states: list = field(default_factory=list)
    observed_priorities: list = field(default_factory=list)
    observed_sample_size: int = 0
    errors: dict = field(default_factory=dict)

    @property
    def ok(self):
        """Whether every section came back without an error."""
        return not self.errors

    @property
    def observed_caveat(self):
        """The wording that must accompany the observed labels wherever they are shown."""
        return OBSERVED_LABELS_CAVEAT

    @property
    def sites(self):
        return self.collections.get("sites", [])

    @property
    def departments(self):
        return self.collections.get("departments", [])

    @property
    def categories(self):
        return self.collections.get("categories", [])

    @property
    def groups(self):
        return self.collections.get("groups", [])


def build_probe_instance(subdomain, api_token, *, region="US", configuration_name="(unsaved)"):
    """
    Build an **unsaved** :class:`~dojo.solarwinds.models.SolarWinds_Instance` that the
    client can authenticate with.

    This is what makes "test connection" work on the create form, before anything has
    been written to the database. The token is put through ``set_api_token()`` rather
    than assigned, so the object behaves exactly like a persisted one - the client
    decrypts what it reads, and a plaintext assignment here would fail to decrypt and
    look like a bad credential.

    The subdomain is **not** validated here; the caller (a form) owns that, and a bad
    value simply produces a failed probe rather than a request to somewhere else -
    ``validate_subdomain`` is what stops the host being retargeted.
    """
    instance = SolarWinds_Instance(
        configuration_name=configuration_name,
        subdomain=(subdomain or "").strip().lower(),
        region=region,
    )
    instance.set_api_token(api_token or "")
    return instance


def _as_options(rows):
    """
    Normalise an API collection into ``[{"id": ..., "name": ...}]``.

    Ids come back as integers and are stringified, because every field they feed
    (``default_site_id``, ``SolarWinds_Product.site_id``, ...) is a ``CharField``.
    Rows with no usable name are dropped rather than rendered as blank options.
    """
    options = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        identifier = row.get("id")
        options.append({"id": "" if identifier is None else str(identifier), "name": name})
    return sorted(options, key=lambda option: option["name"].lower())


def _label_of(value):
    """
    Return the label string from an incident's ``state`` / ``priority`` field.

    Writes take a bare label (8.1), but reads are not guaranteed to mirror that, so
    a nested ``{"name": ...}`` is tolerated here. Anything else is ignored rather
    than stringified - a dict rendered with ``str()`` would be offered to an admin
    as a copyable "label" and would silently no-op on the next push.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


def discover_collections(instance, *, client=None, names=None):
    """
    Fetch the exhaustive lookup collections.

    Returns ``({name: [{"id", "name"}]}, {name: error_message})``. One failing
    collection does not abort the others: a service account can legitimately be
    allowed to read sites but not groups.
    """
    names = names or tuple(COLLECTION_PATHS)
    collections = {}
    errors = {}

    with _client_for(instance, client) as api:
        for name in names:
            try:
                collections[name] = _as_options(api.list_collection(name))
            except SolarWindsError as exc:
                logger.warning("SolarWinds discovery of %s failed for '%s': %s", name, instance, exc)
                errors[name] = str(exc)

    return collections, errors


def observe_incident_labels(instance, *, client=None, days=OBSERVED_SAMPLE_DAYS):
    """
    Sample recent incidents and report the distinct state / priority labels seen.

    Returns ``(states, priorities, sample_size)``. **Not exhaustive** - see the
    module docstring and :data:`OBSERVED_LABELS_CAVEAT`. An empty result means
    "nothing to learn from", never "this tenant has no states".

    Raises :class:`~dojo.solarwinds.client.SolarWindsError` subclasses; the
    aggregating :func:`discover_tenant_configuration` is what turns those into a
    reportable error.
    """
    with _client_for(instance, client) as api:
        incidents = api.list_incidents_updated_since(
            days=days,
            per_page=OBSERVED_SAMPLE_PER_PAGE,
            max_pages=OBSERVED_SAMPLE_MAX_PAGES,
        )

    states = set()
    priorities = set()
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        if state := _label_of(incident.get("state")):
            states.add(state)
        if priority := _label_of(incident.get("priority")):
            priorities.add(priority)

    return sorted(states), sorted(priorities), len(incidents)


def discover_tenant_configuration(instance, *, client=None):
    """
    Run the whole probe and return a :class:`DiscoveryResult`.

    Never raises for an API-level failure - a bad token, an unreachable tenant or a
    rate limit all land in ``result.errors``, keyed by section. That is deliberate:
    every caller is a config surface, and an admin correcting a typo should see the
    message next to the field rather than an error page.
    """
    result = DiscoveryResult()

    # One session for the whole probe: five endpoints on the same host.
    with _client_for(instance, client) as api:
        result.collections, result.errors = discover_collections(instance, client=api)
        try:
            states, priorities, sample_size = observe_incident_labels(instance, client=api)
        except SolarWindsError as exc:
            logger.warning("SolarWinds label observation failed for '%s': %s", instance, exc)
            result.errors["incidents"] = str(exc)
        else:
            result.observed_states = states
            result.observed_priorities = priorities
            result.observed_sample_size = sample_size

    return result


def _client_for(instance, client):
    """
    Return a context manager yielding a usable client.

    A caller-supplied client is borrowed, not owned: ``_BorrowedClient`` skips the
    ``close()`` so the outer ``with`` in :func:`discover_tenant_configuration` keeps
    its session alive across all five calls.
    """
    if client is not None:
        return _BorrowedClient(client)
    return SolarWindsClient(instance)


class _BorrowedClient:

    """Context manager that yields a client it must not close."""

    def __init__(self, client):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc_info):
        return False
