"""
Thin ``requests`` wrapper around the SolarWinds Service Desk (Samanage) API.

INTEGRATIONS_ROADMAP.md 8.3 is the spec. The parts that are not negotiable, all
confirmed against a live trial tenant on 2026-09-18:

* **Base URL is per tenant.** ``https://{subdomain}.samanage.com``. The shared
  regional hosts the OpenAPI spec advertises (``api.samanage.com`` and friends)
  return a bare 404 for ``/incidents`` even on accounts that have incidents.
* **Auth header is ``X-Samanage-Authorization: Bearer <token>``**, and the API
  version is pinned through ``Accept: application/vnd.samanage.v2.1+json``.
* **Every request path needs a ``.json`` extension.** The Accept header does not
  negotiate the format on its own: ``GET /sites`` is a 406 while ``GET /sites.json``
  is a 200, and the same holds for member and sub-resource paths on every verb.
  :meth:`SolarWindsClient._json_path` adds it centrally (R33).
* **``priority`` must be validated before it is sent.** An invalid ``state`` is
  rejected with a 422, but an invalid ``priority`` returns 200 and silently leaves
  the incident unchanged. The API will never tell us a priority was wrong, so this
  client refuses to send one that is not in the instance's
  ``severity_priority_map`` (risks R9/R19).
* **Pagination** is standard Samanage: ``?page=&per_page=`` plus ``Link`` and
  ``X-Total-Pages`` response headers.

Rate limits are 1000/min (Advanced) or 1500/min (Premier), so the polling path must
issue one filtered, paginated query per instance and never per-ticket GETs (R7).
"""
import logging
import random
import time
from datetime import timedelta
from urllib.parse import urljoin

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

# API version pin. Bumping this is a behavioural change, not a cosmetic one.
SAMANAGE_ACCEPT = "application/vnd.samanage.v2.1+json"

DEFAULT_TIMEOUT = 30
DEFAULT_PER_PAGE = 100
# Ceiling on pages walked in a single list call, so a misconfigured filter cannot
# turn one poll into a multi-thousand-request rate-limit incident (R7).
DEFAULT_MAX_PAGES = 50

# Retries apply to 429 and to transient upstream 5xx only - never to 4xx, which are
# deterministic and would just burn rate-limit budget.
DEFAULT_MAX_RETRIES = 4
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_CAP_SECONDS = 60.0
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

# The "Updated_at filter" query parameter only accepts these fixed day buckets, or an
# explicit date range (see the spec's /incidents GET parameters). There is no
# arbitrary-timestamp filter, which is why callers pass a datetime and this module
# snaps it up to a bucket that is guaranteed to cover it.
UPDATED_DAYS_BUCKETS = (1, 7, 14, 30, 60, 90)
UPDATED_SELECT_DATE_RANGE = "Select Date Range"

# Config-resource collections that really do have a lookup endpoint, unlike state
# and priority (8.1). These are exhaustive: what the endpoint returns is what the
# tenant has. Keyed by the response envelope key the API may wrap the list in -
# in practice it returns a bare array, but the wrapper is tolerated.
COLLECTION_PATHS = {
    "sites": "/sites",
    "departments": "/departments",
    "categories": "/categories",
    "groups": "/groups",
}

# Pages walked when listing a config collection. These are small, bounded lists
# (a tenant has tens of sites, not thousands), so the cap is tighter than the
# incident cap - a runaway here would only ever be a misbehaving API.
DEFAULT_COLLECTION_MAX_PAGES = 10


class SolarWindsError(Exception):

    """Base class for every SolarWinds client failure."""


class SolarWindsConfigurationError(SolarWindsError):

    """The instance is not usable - missing token, missing subdomain, empty priority map."""


class SolarWindsAuthenticationError(SolarWindsError):

    """The API rejected our credentials (401/403). Usually a rotated service-account token (R6)."""


class SolarWindsRateLimitError(SolarWindsError):

    """Still rate limited after exhausting the retry budget."""


class SolarWindsAPIError(SolarWindsError):

    """Any other non-2xx response."""

    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SolarWindsInvalidPriorityError(SolarWindsError):

    """
    An outgoing priority label is not in the instance's allowlist.

    Raised *before* the request leaves us. The API would accept the write with a 200
    and quietly discard the priority, so a caught exception here is the only signal
    that anything is wrong.
    """


def _format_samanage_date(value):
    """
    Format a date the way the ``updated_custom_gte``/``lte`` params expect.

    The spec's example is "Mar 2 2023" - short month, non-zero-padded day, year.
    Built by hand rather than with ``%-d``, which is glibc-specific.
    """
    return f"{value.strftime('%b')} {value.day} {value.year}"


def _bucket_days_for(since, *, now=None):
    """
    Return the smallest allowed day bucket that fully covers ``since``, or ``None``.

    ``None`` means the window is wider than the largest bucket (90 days) and the
    caller should fall back to an explicit date range.
    """
    now = now or timezone.now()
    elapsed = now - since
    # Round up: a 0.1-day-old change still needs the 1-day bucket.
    elapsed_days = max(1, -(-elapsed // timedelta(days=1)))
    for bucket in UPDATED_DAYS_BUCKETS:
        if elapsed_days <= bucket:
            return bucket
    return None


class SolarWindsClient:

    """
    Per-instance API client.

    Construct one per :class:`~dojo.solarwinds.models.SolarWinds_Instance` and reuse it
    for the duration of a task; it holds a ``requests.Session`` so connections are
    pooled across the pages of a poll.
    """

    def __init__(self, instance, *, session=None, timeout=DEFAULT_TIMEOUT, max_retries=DEFAULT_MAX_RETRIES):
        self.instance = instance
        self.timeout = timeout
        # Clamped: a negative value would skip the request loop entirely and leave
        # _handle_response with nothing to inspect.
        self.max_retries = max(0, max_retries)
        self._session = session or requests.Session()
        self._owns_session = session is None

    # -- context manager ---------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        """Close the underlying session if this client created it."""
        if self._owns_session:
            self._session.close()

    # -- plumbing ----------------------------------------------------------

    @property
    def base_url(self):
        return self.instance.base_url

    def _build_headers(self):
        token = self.instance.get_api_token()
        if not token:
            msg = (
                f"SolarWinds instance '{self.instance}' has no usable API token "
                f"(missing, or it failed to decrypt with the current DB_KEY)."
            )
            raise SolarWindsConfigurationError(msg)
        return {
            "X-Samanage-Authorization": f"Bearer {token}",
            "Accept": SAMANAGE_ACCEPT,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _json_path(path):
        """
        Append the ``.json`` extension every Samanage endpoint requires.

        The ``Accept: application/vnd.samanage.v2.1+json`` header alone is **not**
        enough. A path without the extension is answered with a bare 406 Not
        Acceptable regardless of the Accept header - confirmed live on 2026-09-19
        for a collection (``/sites``), a member (``/incidents/{id}``) and a
        sub-resource collection (``/incidents/{id}/comments``), on GET, PUT and
        POST alike. The rule is uniform, so it is applied here rather than spelled
        out at each call site (R33).

        The extension goes on the *path* only. Query parameters are handed to
        ``requests`` through ``params=`` and are appended after this URL is built,
        so pagination composes as ``/incidents.json?page=2&per_page=100``. Already
        suffixed paths are left alone so this stays idempotent.
        """
        path, separator, query = path.partition("?")
        path = path.rstrip("/")
        if not path.endswith(".json"):
            path = f"{path}.json"
        return f"{path}{separator}{query}"

    def _url(self, path):
        # urljoin with an explicit leading slash keeps a caller-supplied path from
        # ever escaping the tenant host.
        return urljoin(self.base_url + "/", self._json_path(path).lstrip("/"))

    def _sleep_seconds(self, attempt, response):
        """Honour ``Retry-After`` when the server sends one, else exponential backoff with jitter."""
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                return min(float(retry_after), RETRY_BACKOFF_CAP_SECONDS)
            except (TypeError, ValueError):
                logger.debug("SolarWinds sent an unparseable Retry-After: %r", retry_after)
        backoff = min(RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt), RETRY_BACKOFF_CAP_SECONDS)
        # Full jitter - staggers concurrent workers so they do not re-collide after
        # the same backoff and immediately re-trip the limit.
        return random.uniform(0, backoff)  # noqa: S311 -- jitter, not a security decision

    def _request(self, method, path, *, params=None, json_body=None):
        """
        Issue one API call, retrying 429s, transient 5xx and transport errors with backoff.

        Three failure classes, deliberately treated differently:

        * **Transport errors** (``requests.RequestException`` - connection reset, read
          timeout, DNS failure): no response was ever received, so nothing is known
          about whether the tenant is healthy. A connection blip is the single most
          common transient failure there is, and it retries on the same budget as a
          429. ``_sleep_seconds`` takes ``response=None`` for exactly this - there is no
          ``Retry-After`` to honour, so it falls through to jittered exponential
          backoff. Only once the budget is exhausted does this become a
          ``SolarWindsAPIError``.
        * **Retryable statuses** (429/502/503/504): the tenant answered and said "later".
        * **Everything else, including every other 4xx**: deterministic. Retrying a
          malformed payload burns rate-limit budget (R7) to get the same answer, so
          these fall straight through to :meth:`_handle_response`.

        Returns the ``requests.Response`` so callers that need pagination headers can
        read them; body parsing is left to the caller.
        """
        url = self._url(path)
        headers = self._build_headers()
        last_response = None
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_response = None
                last_exception = exc
                if attempt < self.max_retries:
                    delay = self._sleep_seconds(attempt, None)
                    logger.warning(
                        "SolarWinds %s %s failed before a response (%s); retrying in %.2fs "
                        "(attempt %s/%s)",
                        method, url, exc.__class__.__name__, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                break

            last_response = response
            last_exception = None

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                delay = self._sleep_seconds(attempt, response)
                logger.warning(
                    "SolarWinds %s %s returned %s; retrying in %.2fs (attempt %s/%s)",
                    method, url, response.status_code, delay, attempt + 1, self.max_retries,
                )
                time.sleep(delay)
                continue
            break

        if last_response is None:
            msg = (
                f"SolarWinds request failed: {method} {url} "
                f"(no response after {self.max_retries + 1} attempt(s))"
            )
            raise SolarWindsAPIError(msg) from last_exception

        return self._handle_response(last_response, method, url)

    def _handle_response(self, response, method, url):
        status = response.status_code
        if 200 <= status < 300:
            return response
        if status in {401, 403}:
            msg = (
                f"SolarWinds rejected our credentials ({status}) for {method} {url}. "
                f"The service-account token may have been regenerated."
            )
            raise SolarWindsAuthenticationError(msg)
        if status == 429:
            msg = f"SolarWinds rate limit not cleared after {self.max_retries} retries: {method} {url}"
            raise SolarWindsRateLimitError(msg)
        body = (response.text or "")[:2000]
        msg = f"SolarWinds returned {status} for {method} {url}: {body}"
        raise SolarWindsAPIError(msg, status_code=status, body=body)

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            msg = f"SolarWinds returned a non-JSON body for {response.request.method} {response.url}"
            raise SolarWindsAPIError(msg, status_code=response.status_code, body=(response.text or "")[:2000]) from exc

    # -- priority guard ----------------------------------------------------

    def _validate_priority(self, payload):
        """
        Reject an outgoing priority that is not in the instance's allowlist.

        This is the whole reason writes go through the client rather than straight to
        ``requests``: the API's response to a bad priority is a 200 with the old value
        still in place, so there is no post-hoc way to detect the mistake.
        """
        if not isinstance(payload, dict):
            return
        incident = payload.get("incident")
        if not isinstance(incident, dict) or "priority" not in incident:
            return

        priority = incident["priority"]
        allowlist = self.instance.priority_allowlist
        if not allowlist:
            msg = (
                f"SolarWinds instance '{self.instance}' has an empty severity_priority_map, "
                f"so the outgoing priority {priority!r} cannot be validated. Refusing to "
                f"send it - the API would accept the write and silently drop the priority."
            )
            raise SolarWindsConfigurationError(msg)
        if priority not in allowlist:
            msg = (
                f"Priority {priority!r} is not configured on SolarWinds instance "
                f"'{self.instance}' (allowed: {sorted(allowlist)}). Refusing to send it - "
                f"the API returns 200 and silently keeps the previous priority."
            )
            raise SolarWindsInvalidPriorityError(msg)

    # -- incidents ---------------------------------------------------------

    def create_incident(self, payload):
        """POST a new incident. ``payload`` is the full ``{"incident": {...}}`` envelope."""
        self._validate_priority(payload)
        response = self._request("POST", "/incidents", json_body=payload)
        return self._json(response)

    def get_incident(self, incident_id):
        """Fetch a single incident by its internal id."""
        response = self._request("GET", f"/incidents/{incident_id}")
        return self._json(response)

    def update_incident(self, incident_id, payload):
        """PUT changes onto an existing incident."""
        self._validate_priority(payload)
        response = self._request("PUT", f"/incidents/{incident_id}", json_body=payload)
        return self._json(response)

    def list_incidents_updated_since(
        self,
        since=None,
        *,
        days=None,
        date_from=None,
        date_to=None,
        per_page=DEFAULT_PER_PAGE,
        layout="short",
        max_pages=DEFAULT_MAX_PAGES,
        extra_params=None,
    ):
        """
        Return every incident updated within the requested window.

        The API has no arbitrary-timestamp filter. It offers either a fixed day bucket
        (``updated=<1|7|14|30|60|90>``) or an explicit, day-granular date range
        (``updated=Select Date Range`` + ``updated_custom_gte``/``updated_custom_lte``).
        Passing ``since`` snaps up to the smallest bucket that covers it, falling back
        to a date range beyond 90 days - so the result is always a superset of what was
        asked for, never a subset. Callers must therefore re-check each incident's own
        ``updated_at`` rather than assuming the window is tight.

        One filter is required: an unfiltered full scan would blow the rate limit (R7).
        """
        params = {"per_page": per_page}
        if layout:
            params["layout"] = layout

        params.update(self._build_updated_filter(since=since, days=days, date_from=date_from, date_to=date_to))
        if extra_params:
            params.update(extra_params)

        return self._paginate(
            "/incidents",
            params=params,
            per_page=per_page,
            max_pages=max_pages,
            envelope_keys=("incidents", "data", "results"),
        )

    # -- pagination --------------------------------------------------------

    def _paginate(self, path, *, params, per_page, max_pages, envelope_keys):
        """
        Walk every page of a list endpoint and return the concatenated items.

        Stops on ``X-Total-Pages`` when the API sends it, on a short page when it
        does not, and unconditionally at ``max_pages`` so a misconfigured filter
        cannot turn one call into a rate-limit incident (R7).
        """
        items = []
        page = 1
        while True:
            response = self._request("GET", path, params={**params, "page": page})
            batch = self._as_item_list(self._json(response), envelope_keys)
            items.extend(batch)

            total_pages = self._total_pages(response)
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(batch) < per_page:
                # No X-Total-Pages header: a short page is the end of the results.
                break

            if page >= max_pages:
                logger.warning(
                    "SolarWinds %s listing for '%s' stopped at the %s-page cap; "
                    "narrow the window or raise max_pages.",
                    path, self.instance, max_pages,
                )
                break
            page += 1

        return items

    def _build_updated_filter(self, *, since, days, date_from, date_to):
        if days is not None:
            if days not in UPDATED_DAYS_BUCKETS:
                msg = f"days must be one of {UPDATED_DAYS_BUCKETS}, got {days!r}"
                raise ValueError(msg)
            return {"updated": str(days)}

        if date_from is not None:
            return {
                "updated": UPDATED_SELECT_DATE_RANGE,
                "updated_custom_gte": _format_samanage_date(date_from),
                "updated_custom_lte": _format_samanage_date(date_to or timezone.now()),
            }

        if since is not None:
            bucket = _bucket_days_for(since)
            if bucket is not None:
                return {"updated": str(bucket)}
            return {
                "updated": UPDATED_SELECT_DATE_RANGE,
                "updated_custom_gte": _format_samanage_date(since),
                "updated_custom_lte": _format_samanage_date(timezone.now()),
            }

        msg = (
            "list_incidents_updated_since() requires one of since/days/date_from. "
            "An unfiltered scan would exhaust the API rate limit."
        )
        raise ValueError(msg)

    @staticmethod
    def _as_item_list(body, envelope_keys=()):
        """Normalise a list response, which is a bare JSON array on this API."""
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in envelope_keys:
                if isinstance(body.get(key), list):
                    return body[key]
        return []

    @classmethod
    def _as_incident_list(cls, body):
        """Normalise the ``/incidents`` list response."""
        return cls._as_item_list(body, ("incidents", "data", "results"))

    @staticmethod
    def _total_pages(response):
        raw = response.headers.get("X-Total-Pages")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.debug("SolarWinds sent an unparseable X-Total-Pages: %r", raw)
            return None

    # -- config collections ------------------------------------------------

    def list_collection(self, name, *, per_page=DEFAULT_PER_PAGE, max_pages=DEFAULT_COLLECTION_MAX_PAGES):
        """
        Return every row of one config collection (``sites``/``departments``/
        ``categories``/``groups``).

        These four are the only tenant lookups that are both real endpoints and
        exhaustive. State and priority labels have no endpoint at all (8.1) and can
        only be sampled off existing incidents - see
        :mod:`dojo.solarwinds.discovery`.
        """
        try:
            path = COLLECTION_PATHS[name]
        except KeyError:
            msg = f"Unknown SolarWinds collection {name!r}; expected one of {sorted(COLLECTION_PATHS)}"
            raise ValueError(msg) from None

        return self._paginate(
            path,
            params={"per_page": per_page},
            per_page=per_page,
            max_pages=max_pages,
            envelope_keys=(name, "data", "results"),
        )

    # -- comments ----------------------------------------------------------

    def add_comment(self, incident_id, body, *, is_private=True):
        """
        Post a comment on an incident.

        Private by default: these are DefectDojo correlation/audit notes, not something
        the requester should be emailed about. Note the API does not fire notifications
        for comments unless ``add_callbacks=true`` is passed, which we deliberately do
        not.
        """
        payload = {"comment": {"body": body, "is_private": bool(is_private)}}
        response = self._request("POST", f"/incidents/{incident_id}/comments", json_body=payload)
        return self._json(response)
