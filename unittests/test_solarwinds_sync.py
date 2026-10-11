"""
Integration and idempotency tests for Epic 3 - the SolarWinds ticket sync itself.

Companion to ``test_solarwinds.py`` (models/payload/client helpers) and
``test_solarwinds_config.py`` (UI/API/discovery). Nothing here duplicates those; this
file covers the two flows that actually move data:

* **outbound** - a finding save opening an incident, writing the link row and posting
  the private correlation comment.
* **inbound** - the Celery-beat poll reading incident state back and mitigating the
  linked finding.

**No network.** The Samanage API is faked at the socket boundary: a :class:`FakeSession`
is handed to the real :class:`~dojo.solarwinds.client.SolarWindsClient`, so the real
headers, the real priority guard, the real pagination loop and the real payload builder
all run - only ``requests`` does not. That is deliberate rather than convenient. The
codebase's other external-API convention is VCR cassettes (``DojoVCRTestCase``, used by
the JIRA suite), which requires recording against a live tenant first; there is no
committed SolarWinds cassette and recording one would need real credentials, so the
injected-session seam ``SolarWindsClient(instance, session=...)`` - which exists in
``client.py`` precisely for this - is used instead. No new HTTP-mocking dependency is
introduced either way.

**What the idempotency tests are protecting.** Per R20 the ``Updated_at`` filter has no
sub-day granularity, so the poll re-reads the same day's incidents every ten minutes
forever. "Mitigated exactly once across two poll runs" is therefore not an edge case,
it is the normal steady state, and it is asserted on the note and history counts rather
than just on the finding's flags - re-closing an already-closed finding would look
correct on ``is_mitigated`` while quietly appending a duplicate note on every run.
"""
import contextlib
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlparse

import crum
import requests
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dojo.celery import app
from dojo.finding import helper as finding_helper
from dojo.models import (
    Dojo_User,
    Finding,
    Product,
    SolarWinds_Instance,
    SolarWinds_Product,
    SolarWinds_Ticket,
    System_Settings,
)
from dojo.notifications.models import Alerts
from dojo.solarwinds import client as sw_client
from dojo.solarwinds import helper as sw_helper
from dojo.solarwinds import tasks as sw_tasks
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeResponse:

    """Minimal stand-in for ``requests.Response``, only what ``client.py`` reads."""

    def __init__(self, status_code=200, json_body=None, headers=None, text=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}
        self.text = text if text is not None else ""
        self.url = ""
        self.request = None

    def json(self):
        if self._json_body is None:
            msg = "no json body"
            raise ValueError(msg)
        return self._json_body


class FakeSession:

    """
    Routes ``session.request(...)`` to canned responses by ``(METHOD, path)``.

    A route value may be a single :class:`FakeResponse` (returned for every call) or a
    list, which is consumed one entry per call - that is how "the same poll runs twice"
    and "page 1 then page 2" are expressed. Every call is recorded in :attr:`calls` so
    tests can assert on what went over the wire, including that the correlation comment
    really was marked private.

    A route entry may also be a ``requests.RequestException`` *instance*, which is
    raised instead of returned. That is the transport-failure seam: a connection reset
    or read timeout never produces a response at all, and the client has to treat that
    differently from any status code it could inspect.

    Routes and :meth:`calls_to` are keyed by the *bare* path (``/incidents``), but every
    request is first checked for the mandatory ``.json`` extension that the live API
    requires - see :meth:`_route_path`. That check is what makes each test in this
    module a wire-format regression guard as well as a behaviour test (R33).
    """

    JSON_SUFFIX = ".json"

    def __init__(self, routes=None, default=None):
        self.routes = dict(routes or {})
        self.default = default or FakeResponse(200, json_body=[])
        self.calls = []
        self.closed = False

    @classmethod
    def _route_path(cls, method, url):
        """
        Assert the outgoing path carries ``.json``, and return it stripped for routing.

        The live tenant answers a path without the extension with a 406 Not Acceptable
        no matter what the ``Accept`` header says (R33). A mock happily serves the bare
        path, which is exactly how the missing suffix survived four passes of green
        tests - so the harness refuses to serve one at all.
        """
        path = urlparse(url).path
        if not path.endswith(cls.JSON_SUFFIX):
            msg = (
                f"{method.upper()} {url} was sent without the mandatory "
                f"'{cls.JSON_SUFFIX}' extension; the live tenant answers that with "
                f"406 Not Acceptable (R33)."
            )
            raise AssertionError(msg)
        return path[: -len(cls.JSON_SUFFIX)]

    def request(self, method, url, **kwargs):
        path = self._route_path(method, url)
        key = (method.upper(), path)
        self.calls.append({"method": method.upper(), "url": url, "path": path, **kwargs})
        route = self.routes.get(key, self.default)
        if isinstance(route, list):
            if not route:
                msg = f"FakeSession ran out of queued responses for {key}"
                raise AssertionError(msg)
            route = route.pop(0)
        if isinstance(route, requests.RequestException):
            raise route
        return route

    def close(self):
        self.closed = True

    # -- assertion helpers -------------------------------------------------

    def calls_to(self, method, path):
        return [call for call in self.calls if call["method"] == method.upper() and call["path"] == path]


def incident_body(incident_id=9001, number="INC-9001", state="New", updated_at="2026-09-18T10:00:00Z"):
    """Build a response body shaped like the live API's: a bare incident object."""
    return {"id": incident_id, "number": number, "state": state, "updated_at": updated_at}


def list_page(items, total_pages=1):
    """Build a paginated list response with the headers the live tenant sends (R8)."""
    return FakeResponse(200, json_body=items, headers={"X-Total-Pages": str(total_pages)})


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class SolarWindsSyncTestCase(DojoTestCase):

    """Builds a realistic instance + product mapping + finding, and a client seam."""

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.instance = SolarWinds_Instance(
            configuration_name="acme-servicedesk",
            subdomain="acme",
            open_state="New",
            resolved_states=["Resolved", "Closed"],
            severity_priority_map={
                "Critical": "Critical", "High": "High", "Medium": "Medium", "Low": "Low",
            },
            # Every mapped severity auto-pushes here, so the suite below exercises the
            # push machinery rather than the severity gate. `auto_push_severities`
            # defaults to [] on the model - deliberately, see the model docstring - so
            # a base case that left it empty would make every outbound test in this file
            # pass for the wrong reason (nothing pushed, nothing asserted about why).
            # The gate itself is tested explicitly in TestAutoPushSeveritySplit.
            auto_push_severities=["Critical", "High", "Medium", "Low"],
            push_enabled=True,
            poll_enabled=True,
        )
        self.instance.set_api_token("service-account-token")
        self.instance.save()

        self.finding = Finding.objects.filter(active=True, is_mitigated=False).first()
        self.product = self.finding.test.engagement.product
        self.finding.severity = "High"
        self.finding.false_p = False
        self.finding.out_of_scope = False
        self.finding.duplicate = False
        self.finding.risk_accepted = False
        self.finding.save(dedupe_option=False, issue_updater_option=False, product_grading_option=False)

        self.mapping = SolarWinds_Product.objects.create(
            solarwinds_instance=self.instance,
            product=self.product,
            push_all_findings=True,
        )

    # -- helpers -----------------------------------------------------------

    def client_for(self, session, instance=None):
        """Real client, fake socket. Zero retries so a 429 test does not sleep."""
        return sw_client.SolarWindsClient(instance or self.instance, session=session, max_retries=0)

    @contextlib.contextmanager
    def api(self, session):
        """Make every client the integration constructs for itself use ``session``."""
        real_cls = sw_client.SolarWindsClient

        def factory(instance, **_kwargs):
            return real_cls(instance, session=session, max_retries=0)

        with patch("dojo.solarwinds.helper.SolarWindsClient", new=factory):
            yield

    def create_session(self, *, incident=None, comment_status=200):
        """A session that answers the two calls an outbound push makes."""
        incident = incident or incident_body()
        return FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(200, json_body=incident),
            ("POST", f"/incidents/{incident['id']}/comments"): FakeResponse(
                comment_status, json_body={"id": 5} if comment_status < 300 else None, text="nope",
            ),
        })

    def poll_session(self, incidents, *, runs=1):
        """A session that answers ``runs`` identical poll requests."""
        return FakeSession(routes={
            ("GET", "/incidents"): [list_page(incidents) for _ in range(runs)],
        })

    def link_ticket(self, *, incident_id="9001", state="New", instance=None, finding=None):
        return SolarWinds_Ticket.objects.create(
            finding=finding or self.finding,
            solarwinds_instance=instance or self.instance,
            incident_id=incident_id,
            incident_number=f"INC-{incident_id}",
            last_synced_state=state,
        )

    def solarwinds_alerts(self, fragment):
        return Alerts.objects.filter(title__icontains=fragment)

    def note_count(self):
        return self.finding.notes.count()


# ---------------------------------------------------------------------------
# Outbound: routing and eligibility
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestOutboundEligibility(SolarWindsSyncTestCase):

    def test_push_all_findings_makes_a_mapped_finding_eligible(self):
        self.assertTrue(sw_helper.is_push_all_findings(self.finding))
        self.assertIsNotNone(sw_helper.resolve_push_target(self.finding))

    def test_without_push_all_an_explicit_request_is_required(self):
        self.mapping.push_all_findings = False
        self.mapping.save()
        self.assertIsNone(sw_helper.resolve_push_target(self.finding))
        self.assertIsNotNone(sw_helper.resolve_push_target(self.finding, push_requested=True))

    def test_a_disabled_instance_is_never_a_target(self):
        self.instance.push_enabled = False
        self.instance.save()
        self.assertIsNone(sw_helper.resolve_push_target(self.finding, push_requested=True))

    def test_a_product_with_no_mapping_is_never_a_target(self):
        other = Product.objects.exclude(id=self.product.id).first()
        other_finding = Finding.objects.filter(test__engagement__product=other).first()
        if other_finding is None:
            self.skipTest("fixture has no finding on a second product")
        self.assertIsNone(sw_helper.resolve_push_target(other_finding, push_requested=True))

    def test_closed_and_dismissed_findings_are_not_pushable(self):
        # Without this gate, closing a finding would itself open a ticket: the close
        # path calls Finding.save() like every other write.
        for field in ("is_mitigated", "false_p", "out_of_scope", "duplicate", "risk_accepted"):
            with self.subTest(field=field):
                finding = Finding.objects.get(pk=self.finding.pk)
                setattr(finding, field, True)
                self.assertFalse(sw_helper.is_status_pushable(finding))

    def test_an_inactive_finding_is_not_pushable(self):
        finding = Finding.objects.get(pk=self.finding.pk)
        finding.active = False
        self.assertFalse(sw_helper.is_status_pushable(finding))

    def test_an_open_mapped_finding_is_pushable(self):
        self.assertTrue(sw_helper.is_status_pushable(Finding.objects.get(pk=self.finding.pk)))


# ---------------------------------------------------------------------------
# Outbound: the push itself
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestOutboundPush(SolarWindsSyncTestCase):

    def test_push_creates_the_incident_the_link_row_and_a_private_comment(self):
        session = self.create_session()
        ticket = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))

        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.incident_id, "9001")
        self.assertEqual(ticket.incident_number, "INC-9001")
        self.assertEqual(ticket.solarwinds_instance, self.instance)
        # The cursor is seeded with the state we just asked for, so the first poll does
        # not read "New" as a change worth acting on.
        self.assertEqual(ticket.last_synced_state, "New")

        create = session.calls_to("POST", "/incidents")
        self.assertEqual(len(create), 1)
        payload = create[0]["json"]["incident"]
        self.assertEqual(payload["state"], "New")
        self.assertEqual(payload["priority"], "High")
        self.assertIn(f"finding-{self.finding.id}", payload["tag_list"])

        comments = session.calls_to("POST", "/incidents/9001/comments")
        self.assertEqual(len(comments), 1)
        comment = comments[0]["json"]["comment"]
        self.assertTrue(comment["is_private"], "correlation comments must not notify the requester")
        self.assertIn(str(self.finding.id), comment["body"])

    def test_the_request_carries_the_bearer_token_and_the_tenant_host(self):
        session = self.create_session()
        sw_helper.push_finding_internal(self.finding, client=self.client_for(session))
        call = session.calls_to("POST", "/incidents")[0]
        self.assertEqual(
            call["headers"]["X-Samanage-Authorization"], "Bearer service-account-token",
        )
        self.assertTrue(call["url"].startswith("https://acme.samanage.com/"))

    def test_an_existing_ticket_is_never_double_created(self):
        existing = self.link_ticket(incident_id="1111")
        session = self.create_session()

        result = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))

        self.assertIsNone(result)
        self.assertEqual(session.calls, [], "no API call may be made for an already-ticketed finding")
        self.assertEqual(SolarWinds_Ticket.objects.filter(finding=self.finding).count(), 1)
        self.assertEqual(
            SolarWinds_Ticket.objects.get(finding=self.finding).incident_id, existing.incident_id,
        )

    def test_pushing_twice_in_a_row_creates_only_one_incident(self):
        session = self.create_session()
        first = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))
        second = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_an_unmapped_severity_is_skipped_silently(self):
        # Info has no entry in severity_priority_map on purpose (8.5): Info findings
        # stay DefectDojo-only rather than adding ticket-queue volume.
        self.finding.severity = "Info"
        self.finding.save(dedupe_option=False, issue_updater_option=False, product_grading_option=False)
        session = self.create_session()

        result = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))

        self.assertIsNone(result)
        self.assertEqual(session.calls, [])
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())
        self.assertFalse(self.solarwinds_alerts("push failed").exists(), "a skip is not a failure")

    def test_a_disabled_instance_skips_without_calling_the_api(self):
        self.instance.push_enabled = False
        self.instance.save()
        session = self.create_session()

        self.assertIsNone(
            sw_helper.push_finding_internal(self.finding, push_requested=True, client=self.client_for(session)),
        )
        self.assertEqual(session.calls, [])

    def test_a_response_without_an_id_is_an_error_not_a_dangling_ticket(self):
        session = FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(200, json_body={"number": "INC-1"}),
        })
        with self.assertRaises(sw_client.SolarWindsError):
            sw_helper.push_finding_internal(self.finding, client=self.client_for(session))
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_a_failed_correlation_comment_still_leaves_the_ticket_linked(self):
        # The incident exists by then. Retrying the whole push would create a second
        # one, so the comment failure is alerted and swallowed.
        session = self.create_session(comment_status=500)

        ticket = sw_helper.push_finding_internal(self.finding, client=self.client_for(session))

        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.incident_id, "9001")
        self.assertTrue(self.solarwinds_alerts("correlation comment failed").exists())


# ---------------------------------------------------------------------------
# Outbound: failure handling through the Celery task
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestOutboundTaskFailureHandling(SolarWindsSyncTestCase):

    def run_task(self, session):
        with self.api(session):
            return sw_tasks.push_finding_to_solarwinds(self.finding.id)

    def test_an_auth_failure_alerts_and_does_not_raise(self):
        # R6: a regenerated service-account token has no symptom other than sync
        # stopping, so it has to become a subscribable notification.
        session = FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(401, text="unauthorized"),
        })
        self.assertIsNone(self.run_task(session))
        self.assertTrue(self.solarwinds_alerts("push failed").exists())
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_a_server_error_alerts_and_does_not_raise(self):
        session = FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(500, text="boom"),
        })
        self.assertIsNone(self.run_task(session))
        self.assertTrue(self.solarwinds_alerts("push failed").exists())

    def test_a_rate_limit_does_not_retry_when_there_is_no_retry_path(self):
        # Called directly (as in an eager run or a block_execution user), retry() cannot
        # reschedule anything, so the task must report rather than re-raise.
        session = FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(429, headers={"Retry-After": "1"}, text="slow down"),
        })
        self.assertIsNone(self.run_task(session))
        self.assertTrue(self.solarwinds_alerts("rate limited").exists())

    def test_a_configuration_error_is_alerted_and_not_retried(self):
        # An undecryptable/absent token makes _build_headers raise
        # SolarWindsConfigurationError before any request is issued. Retrying that
        # cannot help - it needs a human - so it must be alerted and dropped, which is
        # R22's residual requirement stated for the push path.
        self.instance.api_token = ""
        self.instance.save()
        session = self.create_session()

        self.assertIsNone(self.run_task(session))

        self.assertEqual(session.calls, [], "no request may be issued without credentials")
        alerts = self.solarwinds_alerts("push failed")
        self.assertTrue(alerts.exists())
        self.assertIn("configuration or credential change", alerts.first().description)

    def test_an_empty_priority_map_is_a_silent_skip_not_an_error(self):
        # R22's residual, actual behaviour: the model's clean() blocks
        # push_enabled + empty map at every normal entry point, but a direct .save()
        # (or a fixture) bypasses it. What happens then is a *skip*, not a
        # SolarWindsConfigurationError - services.should_push_finding() sees the
        # severity as unmapped and stops before the client's priority guard is ever
        # reached. Asserted so the distinction is not rediscovered later.
        self.instance.severity_priority_map = {}
        self.instance.save()
        session = self.create_session()

        self.assertIsNone(self.run_task(session))

        self.assertEqual(session.calls, [])
        self.assertFalse(self.solarwinds_alerts("push failed").exists())

    def test_a_missing_finding_is_a_no_op(self):
        missing_id = Finding.objects.order_by("-id").first().id + 1000
        self.assertIsNone(sw_tasks.push_finding_to_solarwinds(missing_id))


# ---------------------------------------------------------------------------
# Outbound: the finding-save seam
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestClientTransportRetries(SolarWindsSyncTestCase):

    """
    A failure that never produced a response must use the same retry budget as a 429.

    ``_request`` used to re-raise ``requests.RequestException`` on the spot, escaping
    the retry loop on the first attempt - so a connection reset or read timeout, the
    most ordinary transient failure there is, permanently failed a ticket push with
    zero retries while a 503 from the same tenant got four.
    """

    def setUp(self):
        super().setUp()
        # The real backoff sleeps for seconds; the schedule itself is asserted
        # separately below, so the tests do not have to wait it out.
        patcher = patch("dojo.solarwinds.client.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def retrying_client(self, session, *, max_retries=3):
        return sw_client.SolarWindsClient(self.instance, session=session, max_retries=max_retries)

    def test_a_transport_error_is_retried_and_can_succeed(self):
        session = FakeSession(routes={
            ("POST", "/incidents"): [
                requests.ConnectionError("connection reset by peer"),
                requests.ReadTimeout("timed out"),
                FakeResponse(200, json_body=incident_body()),
            ],
        })

        created = self.retrying_client(session).create_incident({"incident": {"name": "x"}})

        self.assertEqual(created["id"], 9001)
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 3)

    def test_a_transport_error_raises_only_once_the_budget_is_exhausted(self):
        session = FakeSession(routes={
            ("POST", "/incidents"): requests.ConnectionError("connection reset by peer"),
        })

        with self.assertRaises(sw_client.SolarWindsAPIError) as caught:
            self.retrying_client(session).create_incident({"incident": {"name": "x"}})

        # max_retries=3 means four attempts in total, matching the 429/5xx branch.
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 4)
        self.assertIsInstance(caught.exception.__cause__, requests.ConnectionError)

    def test_the_transport_retry_backs_off_between_attempts(self):
        # There is no Retry-After to honour when no response arrived, so this must fall
        # through to jittered exponential backoff rather than hammering immediately.
        session = FakeSession(routes={
            ("POST", "/incidents"): requests.ConnectionError("nope"),
        })

        with self.assertRaises(sw_client.SolarWindsAPIError):
            self.retrying_client(session).create_incident({"incident": {"name": "x"}})

        self.assertEqual(self.sleep.call_count, 3)
        for call in self.sleep.call_args_list:
            self.assertGreaterEqual(call.args[0], 0)

    def test_zero_retries_still_reports_the_transport_failure(self):
        session = FakeSession(routes={
            ("POST", "/incidents"): requests.ConnectionError("nope"),
        })

        with self.assertRaises(sw_client.SolarWindsAPIError):
            self.retrying_client(session, max_retries=0).create_incident({"incident": {"name": "x"}})

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_a_client_error_is_still_not_retried(self):
        # 4xx is deterministic: retrying a malformed payload burns rate-limit budget to
        # get the same answer. Only pre-response failures joined the retry path.
        session = FakeSession(routes={
            ("POST", "/incidents"): FakeResponse(422, text="Invalid state for account."),
        })

        with self.assertRaises(sw_client.SolarWindsAPIError):
            self.retrying_client(session).create_incident({"incident": {"name": "x"}})

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_a_transport_error_mid_poll_does_not_lose_the_earlier_pages(self):
        # The poll paginates through _request too, so the retry has to work there as
        # well - a blip on page 2 should not fail a whole reconciliation run.
        session = FakeSession(routes={
            ("GET", "/incidents"): [
                list_page([incident_body(incident_id=1)], total_pages=2),
                requests.ConnectionError("blip"),
                list_page([incident_body(incident_id=2)], total_pages=2),
            ],
        })

        incidents = self.retrying_client(session).list_incidents_updated_since(days=1)

        self.assertEqual([item["id"] for item in incidents], [1, 2])
        self.assertEqual(len(session.calls_to("GET", "/incidents")), 3)


@versioned_fixtures
class TestOutboundSaveHook(SolarWindsSyncTestCase):

    """
    End-to-end through ``Finding.save()``, which is the seam this pass adds.

    The push is dispatched with ``dojo_dispatch_task``, so with no current user
    ``we_want_async()`` is True and the task would go to the broker and never run
    in-process. These tests use the codebase's existing convention for that
    (``block_execution=True`` on the acting user, as ``test_finding_helper`` and the
    deduplication suites do) rather than patching the dispatcher, so the real
    dispatch path is still exercised - only the hop through the broker is removed.
    """

    def setUp(self):
        super().setUp()
        self.actor = Dojo_User.objects.get(username="admin")
        self.actor.usercontactinfo.block_execution = True
        self.actor.usercontactinfo.save()

    @contextlib.contextmanager
    def acting(self):
        with crum.impersonate(self.actor):
            yield

    def test_saving_a_finding_pushes_it_when_push_all_findings_is_set(self):
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save()

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)
        self.assertTrue(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_saving_a_finding_does_nothing_without_push_all_or_an_explicit_flag(self):
        self.mapping.push_all_findings = False
        self.mapping.save()
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save()

        self.assertEqual(session.calls, [])
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_the_explicit_push_to_solarwinds_flag_threads_through_finding_save(self):
        self.mapping.push_all_findings = False
        self.mapping.save()
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save(push_to_solarwinds=True)

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_the_explicit_flag_alone_still_reaches_post_processing(self):
        # push_to_solarwinds has to be in Finding.save()'s "is any post-processing
        # needed?" condition alongside push_to_jira, or an explicit push request would
        # be silently dropped whenever every other option happened to be off.
        self.mapping.push_all_findings = False
        self.mapping.save()
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save(
                dedupe_option=False, issue_updater_option=False, product_grading_option=False,
                push_to_solarwinds=True,
            )

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_the_named_save_helper_threads_the_flag(self):
        self.mapping.push_all_findings = False
        self.mapping.save()
        session = self.create_session()
        with self.api(session), self.acting():
            finding_helper._save_finding_with_solarwinds_sync(self.finding, push_to_solarwinds=True)

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_closing_a_finding_does_not_open_a_ticket_for_it(self):
        # The close path saves the finding like any other write, so without the status
        # gate a push_all_findings product would ticket every finding as it closed.
        session = self.create_session()
        with self.api(session), self.acting():
            finding_helper.close_finding(
                finding=self.finding,
                user=self.actor,
                is_mitigated=True,
                mitigated=timezone.now(),
                mitigated_by=self.actor,
                false_p=False,
                out_of_scope=False,
                duplicate=False,
            )

        self.assertEqual(session.calls, [])
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_an_unconfigured_deployment_dispatches_nothing_on_save(self):
        SolarWinds_Product.objects.all().delete()
        SolarWinds_Instance.objects.all().delete()
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save()
        self.assertEqual(session.calls, [])

    def test_save_no_options_still_pushes_a_config_driven_finding(self):
        # push_all_findings is configuration, not one of Finding.save()'s
        # post-processing options, so a caller that switches every option off must not
        # switch the integration off with them. save_no_options() passes them all False
        # and so used to skip post-processing (and the push) entirely, silently.
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save_no_options()

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)
        self.assertTrue(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_every_option_off_still_pushes_a_config_driven_finding(self):
        # The same gap reached through the explicit-kwargs shape that
        # jira.helper.finding_link_jira and the SonarQube status updater use.
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save(
                dedupe_option=False,
                issue_updater_option=False,
                product_grading_option=False,
                push_to_jira=False,
            )

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_save_no_options_pushes_nothing_without_a_push_all_mapping(self):
        self.mapping.push_all_findings = False
        self.mapping.save()
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save_no_options()

        self.assertEqual(session.calls, [])

    def test_a_deferred_save_leaves_the_push_to_the_batch(self):
        # The importers save each finding with every option off and then hand the whole
        # batch to post_process_findings_batch, which answers the routing question once.
        # Asking again per finding would be +1 query and a second dispatch on every
        # finding of an import - exactly the per-finding cost R27 chose to avoid.
        session = self.create_session()
        with self.api(session), self.acting():
            self.finding.save_no_options(defer_post_processing=True)

        self.assertEqual(session.calls, [])
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=self.finding).exists())

    def test_save_no_options_costs_one_query_in_an_unconfigured_deployment(self):
        # The price of closing the gap above: save paths that previously did no
        # post-processing at all now pay exactly one indexed EXISTS. Pinned here so a
        # future change that makes it a relation walk is caught (R27/R28).
        SolarWinds_Product.objects.all().delete()
        SolarWinds_Instance.objects.all().delete()
        finding = Finding.objects.get(pk=self.finding.pk)

        with self.acting():
            baseline = CaptureQueriesContext(connection)
            with baseline:
                finding.save_no_options()

        solarwinds_queries = [
            query for query in baseline.captured_queries
            if "solarwinds_product" in query["sql"].lower()
        ]
        self.assertEqual(len(solarwinds_queries), 1, solarwinds_queries)

    def test_the_gate_does_not_drag_false_positive_history_onto_optionless_saves(self):
        # The fix is deliberately NOT "add another `or` to Finding.save()'s gate":
        # post_process_finding_save_internal also runs false-positive history, which is
        # gated on system settings rather than on any option flag, so widening the gate
        # would newly run it on save paths that opt out of everything.
        system_settings = System_Settings.objects.get()
        system_settings.enable_deduplication = False
        system_settings.false_positive_history = True
        system_settings.save()

        session = self.create_session()
        with patch("dojo.finding.helper.do_false_positive_history") as fp_history, \
                self.api(session), self.acting():
            self.finding.save_no_options()

        fp_history.assert_not_called()
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_an_unconfigured_deployment_pays_one_query_not_a_relation_walk(self):
        # This hook runs on every Finding.save() in every deployment, including the
        # overwhelming majority that never configure SolarWinds. get_product() walks
        # test -> engagement -> product, which is three queries on a finding loaded
        # without select_related, so the existence guard has to come first.
        SolarWinds_Product.objects.all().delete()
        SolarWinds_Instance.objects.all().delete()
        finding = Finding.objects.get(pk=self.finding.pk)

        with self.assertNumQueries(1):
            self.assertIsNone(sw_helper.push_finding(finding))


@versioned_fixtures
class TestOutboundBatchPush(SolarWindsSyncTestCase):

    """
    The import path, where findings arrive thousands at a time.

    Every finding in a batch belongs to one product, so the routing lookup is a
    per-batch question. It used to be answered per finding on the branch that actually
    pushes - a 5,000-finding import into a push_all_findings product re-ran the same
    mapping query 5,000 times. Only the "nothing is configured" skip branch was cheap.
    """

    def setUp(self):
        super().setUp()
        self.actor = Dojo_User.objects.get(username="admin")
        self.actor.usercontactinfo.block_execution = True
        self.actor.usercontactinfo.save()

        # Build the batch with pushing switched off, so creating the fixtures does not
        # itself open tickets, then switch it on for the assertions.
        self.mapping.push_all_findings = False
        self.mapping.save()
        self.findings = [self.finding]
        for index in range(3):
            self.findings.append(Finding.objects.create(
                test=self.finding.test,
                title=f"SolarWinds batch finding {index}",
                severity="High",
                description="batched",
                reporter=self.actor,
                active=True,
                verified=False,
            ))
        self.mapping.push_all_findings = True
        self.mapping.save()

    def batch_kwargs(self):
        return {
            "dedupe_option": False,
            "issue_updater_option": False,
            "product_grading_option": False,
        }

    def run_batch(self, session):
        with patch.object(
            sw_helper, "get_product_mappings", wraps=sw_helper.get_product_mappings,
        ) as spy, self.api(session), crum.impersonate(self.actor):
            finding_helper.post_process_findings_batch(
                [finding.id for finding in self.findings], **self.batch_kwargs(),
            )
        return spy

    def run_batch_without_dispatching(self):
        """
        Run the batch with the dispatch stubbed out, to isolate the orchestration.

        ``push_finding_internal`` deliberately re-resolves the routing inside the
        Celery task, against fresh database state in another transaction - so with the
        task executing inline (``block_execution``) the resolutions from *both* layers
        are indistinguishable. The per-batch claim is about the dispatching thread, so
        that is what this measures.
        """
        with patch.object(
            sw_helper, "get_product_mappings", wraps=sw_helper.get_product_mappings,
        ) as spy, patch.object(sw_helper, "push_finding") as push:
            finding_helper.post_process_findings_batch(
                [finding.id for finding in self.findings], **self.batch_kwargs(),
            )
        return spy, push

    def test_the_mapping_is_resolved_once_per_batch_not_once_per_finding(self):
        spy, push = self.run_batch_without_dispatching()

        self.assertEqual(spy.call_count, 1)
        self.assertEqual(push.call_count, len(self.findings))

    def test_the_resolved_mapping_is_threaded_into_every_push(self):
        # Resolving once is only half of it - the resolution has to actually reach
        # each push_finding() call, or it re-queries and the saving is imaginary.
        _spy, push = self.run_batch_without_dispatching()

        threaded = [call.kwargs["mappings"] for call in push.call_args_list]
        self.assertEqual(len(threaded), len(self.findings))
        # One object, shared by identity: proof it was resolved once and passed on,
        # not re-resolved into an equal-looking list per finding.
        self.assertEqual(len({id(mappings) for mappings in threaded}), 1)
        self.assertEqual([mapping.id for mapping in threaded[0]], [self.mapping.id])

    def test_the_batch_still_pushes_every_eligible_finding(self):
        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))

        self.run_batch(session)

        self.assertEqual(len(session.calls_to("POST", "/incidents")), len(self.findings))

    def test_every_finding_in_the_batch_still_gets_its_own_ticket(self):
        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))

        self.run_batch(session)

        self.assertEqual(
            SolarWinds_Ticket.objects.filter(finding__in=self.findings).count(),
            len(self.findings),
        )

    def test_per_finding_checks_are_not_hoisted_with_the_mapping(self):
        # is_status_pushable and has_ticket differ finding by finding and must keep
        # running per finding even though the routing answer is shared.
        closed = self.findings[1]
        closed.active = False
        closed.is_mitigated = True
        closed.save_no_options()
        self.link_ticket(incident_id="7777", finding=self.findings[2])
        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))

        self.run_batch(session)

        # Two of four are ineligible: one closed, one already ticketed.
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 2)

    def test_an_unconfigured_product_still_answers_the_question_once(self):
        SolarWinds_Product.objects.all().delete()
        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))

        spy = self.run_batch(session)

        self.assertEqual(spy.call_count, 1)
        self.assertEqual(session.calls, [])


# ---------------------------------------------------------------------------
# Outbound: the auto / manual severity split
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestAutoPushSeveritySplit(SolarWindsSyncTestCase):

    """
    ``auto_push_severities`` gates the config-driven push; the manual one is unaffected.

    The concrete situation this exists for: one SCA import of ~1,500 findings across
    Critical/High/Medium/Low is ~1,500 individual ``POST /incidents`` calls, because
    nothing in this API batches or summarises and ``SolarWinds_Ticket.finding`` is a
    OneToOne. Critical/High auto-file; Medium/Low stay reachable one finding at a time.

    Both directions are asserted explicitly for the *same* finding - "Medium does not
    auto-push" is only half a feature if "Medium still pushes on request" is not also
    true, and a bug that broke the second would look identical to the first passing.
    """

    def setUp(self):
        super().setUp()
        self.actor = Dojo_User.objects.get(username="admin")
        self.actor.usercontactinfo.block_execution = True
        self.actor.usercontactinfo.save()

        # The realistic configuration: everything mapped, only the top two automatic.
        self.instance.auto_push_severities = ["Critical", "High"]
        self.instance.save()

    @contextlib.contextmanager
    def acting(self):
        with crum.impersonate(self.actor):
            yield

    def at_severity(self, severity):
        """Return the suite's finding re-read at ``severity``, without saving it."""
        finding = Finding.objects.get(pk=self.finding.pk)
        finding.severity = severity
        return finding

    # -- routing ----------------------------------------------------------

    def test_an_auto_push_severity_resolves_a_target_with_nobody_asking(self):
        for severity in ("Critical", "High"):
            with self.subTest(severity=severity):
                self.assertIsNotNone(sw_helper.resolve_push_target(self.at_severity(severity)))

    def test_a_mapped_but_not_auto_push_severity_resolves_no_automatic_target(self):
        for severity in ("Medium", "Low"):
            with self.subTest(severity=severity):
                self.assertIsNone(sw_helper.resolve_push_target(self.at_severity(severity)))

    def test_the_same_severity_resolves_a_target_when_explicitly_requested(self):
        # The other half. Medium is in severity_priority_map, so a human may file it.
        for severity in ("Medium", "Low"):
            with self.subTest(severity=severity):
                self.assertIsNotNone(
                    sw_helper.resolve_push_target(self.at_severity(severity), push_requested=True),
                )

    def test_an_unmapped_severity_has_no_manual_route_either(self):
        # auto_push_severities loosens nothing: Info has no priority label at all, so
        # it is refused on both paths. severity_priority_map remains the outer gate.
        info = self.at_severity("Info")
        self.assertIsNone(sw_helper.resolve_push_target(info))
        self.assertFalse(sw_helper.is_manual_push_available(info))

    def test_an_empty_auto_list_auto_pushes_nothing_at_all(self):
        # The shipped default. push_all_findings alone must not be enough.
        self.instance.auto_push_severities = []
        self.instance.save()
        for severity in ("Critical", "High", "Medium", "Low"):
            with self.subTest(severity=severity):
                self.assertIsNone(sw_helper.resolve_push_target(self.at_severity(severity)))

    def test_an_explicit_request_still_works_with_an_empty_auto_list(self):
        self.instance.auto_push_severities = []
        self.instance.save()
        self.assertIsNotNone(
            sw_helper.resolve_push_target(self.at_severity("High"), push_requested=True),
        )

    def test_the_severity_gate_needs_push_all_findings_as_well(self):
        # Two independent switches, ANDed. An auto-push severity on a product that does
        # not push everything is still manual-only.
        self.mapping.push_all_findings = False
        self.mapping.save()
        self.assertIsNone(sw_helper.resolve_push_target(self.at_severity("Critical")))

    # -- the two helper predicates ----------------------------------------

    def test_is_push_all_findings_stays_severity_blind(self):
        # Deliberate: it is the per-BATCH gate, evaluated against findings[0]. Making
        # it severity-aware would let one Medium finding at the head of an import
        # suppress every Critical behind it.
        self.assertTrue(sw_helper.is_push_all_findings(self.at_severity("Medium")))
        self.assertTrue(sw_helper.is_push_all_findings(self.at_severity("Critical")))

    def test_is_push_all_findings_auto_is_the_per_finding_answer(self):
        self.assertTrue(sw_helper.is_push_all_findings_auto(self.at_severity("Critical")))
        self.assertFalse(sw_helper.is_push_all_findings_auto(self.at_severity("Medium")))

    def test_is_manual_push_available_covers_every_mapped_severity(self):
        for severity in ("Critical", "High", "Medium", "Low"):
            with self.subTest(severity=severity):
                self.assertTrue(sw_helper.is_manual_push_available(self.at_severity(severity)))

    def test_is_manual_push_available_is_false_for_an_unmapped_product(self):
        self.mapping.delete()
        self.assertFalse(sw_helper.is_manual_push_available(self.at_severity("Critical")))

    # -- dispatch ---------------------------------------------------------

    def test_a_non_auto_severity_dispatches_nothing_on_save(self):
        finding = self.at_severity("Medium")
        session = self.create_session()
        with self.api(session), self.acting():
            finding.save()

        self.assertEqual(session.calls, [])
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=finding).exists())

    def test_the_same_finding_pushes_when_a_human_asks(self):
        # The pair to the test above, on the same finding at the same severity: the
        # only thing that changed is that somebody ticked the box.
        finding = self.at_severity("Medium")
        session = self.create_session()
        with self.api(session), self.acting():
            finding.save(push_to_solarwinds=True)

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)
        self.assertTrue(SolarWinds_Ticket.objects.filter(finding=finding).exists())

    def test_the_manually_pushed_incident_carries_the_mapped_priority(self):
        # Medium is not an auto-push severity but it *is* mapped, so the incident it
        # creates must still carry the tenant's Medium priority label - not a fallback
        # and not the priority of some other severity.
        finding = self.at_severity("Medium")
        session = self.create_session()
        with self.api(session), self.acting():
            finding.save(push_to_solarwinds=True)

        body = session.calls_to("POST", "/incidents")[0]["json"]["incident"]
        self.assertEqual(body["priority"], "Medium")

    def test_an_auto_severity_still_pushes_on_a_plain_save(self):
        session = self.create_session()
        with self.api(session), self.acting():
            self.at_severity("Critical").save()

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_the_task_re_checks_the_severity_gate_against_fresh_state(self):
        # push_finding_internal resolves routing again inside the task. A severity that
        # stopped being an auto-push severity between dispatch and execution must not
        # open an incident on the config-driven path.
        self.instance.auto_push_severities = ["Critical"]
        self.instance.save()
        session = self.create_session()

        ticket = sw_helper.push_finding_internal(
            self.at_severity("High"), client=self.client_for(session),
        )

        self.assertIsNone(ticket)
        self.assertEqual(session.calls, [])

    def test_the_task_honours_an_explicit_request_for_a_non_auto_severity(self):
        session = self.create_session()

        ticket = sw_helper.push_finding_internal(
            self.at_severity("Medium"), push_requested=True, client=self.client_for(session),
        )

        self.assertIsNotNone(ticket)
        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    # -- batch ------------------------------------------------------------

    def test_a_batch_headed_by_a_non_auto_finding_still_pushes_the_auto_ones(self):
        """
        The reason ``is_push_all_findings`` must not consult severity.

        ``post_process_findings_batch`` asks the gate question once, about
        ``findings[0]``. If that question were severity-aware, a single Medium finding
        sorted to the front of a scan import would silently suppress every Critical
        behind it - a data-loss-shaped bug that no per-finding test would catch.
        """
        self.mapping.push_all_findings = False
        self.mapping.save()
        medium = Finding.objects.create(
            test=self.finding.test, title="SolarWinds batch medium", severity="Medium",
            description="batched", reporter=self.actor, active=True, verified=False,
        )
        critical = Finding.objects.create(
            test=self.finding.test, title="SolarWinds batch critical", severity="Critical",
            description="batched", reporter=self.actor, active=True, verified=False,
        )
        self.mapping.push_all_findings = True
        self.mapping.save()

        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))
        with self.api(session), crum.impersonate(self.actor):
            finding_helper.post_process_findings_batch(
                [medium.id, critical.id],
                dedupe_option=False, issue_updater_option=False, product_grading_option=False,
            )

        self.assertTrue(SolarWinds_Ticket.objects.filter(finding=critical).exists())
        self.assertFalse(SolarWinds_Ticket.objects.filter(finding=medium).exists())

    # -- the explicit-dispatch helper used by the REST API ----------------

    def test_push_finding_explicitly_dispatches_for_a_non_auto_severity(self):
        finding = self.at_severity("Medium")
        session = self.create_session()
        with self.api(session), self.acting():
            sw_helper.push_finding_explicitly(finding)

        self.assertEqual(len(session.calls_to("POST", "/incidents")), 1)

    def test_push_finding_explicitly_does_not_double_dispatch_an_auto_push(self):
        # The guard that keeps the REST API from manufacturing R25: the save that just
        # happened already dispatched a task for an auto-push severity, and a second
        # one racing it would create two incidents for one finding.
        with patch.object(sw_helper, "push_finding") as push:
            result = sw_helper.push_finding_explicitly(self.at_severity("Critical"))

        self.assertIsNone(result)
        push.assert_not_called()

    def test_push_finding_explicitly_ignores_an_unsaved_finding(self):
        self.assertIsNone(sw_helper.push_finding_explicitly(Finding()))


# ---------------------------------------------------------------------------
# Inbound: state mapping
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestInboundStateMapping(SolarWindsSyncTestCase):

    def test_a_resolved_state_mitigates_the_linked_finding(self):
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")])

        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertTrue(finding.is_mitigated)
        self.assertFalse(finding.active)
        self.assertIsNotNone(finding.mitigated)
        self.assertEqual(summary.mitigated, 1)

    def test_every_configured_resolved_label_counts(self):
        for label in ("Resolved", "Closed"):
            with self.subTest(label=label):
                Finding.objects.filter(pk=self.finding.pk).update(is_mitigated=False, active=True)
                SolarWinds_Ticket.objects.all().delete()
                self.link_ticket(state="Assigned")
                session = self.poll_session([incident_body(state=label)])
                sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))
                self.assertTrue(Finding.objects.get(pk=self.finding.pk).is_mitigated)

    def test_a_non_resolved_state_advances_the_cursor_without_touching_the_finding(self):
        ticket = self.link_ticket(state="New")
        session = self.poll_session([incident_body(state="Awaiting Input")])

        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Awaiting Input")
        self.assertFalse(Finding.objects.get(pk=self.finding.pk).is_mitigated)
        self.assertEqual(summary.mitigated, 0)
        self.assertEqual(summary.cursor_advanced, 1)

    def test_a_state_label_not_in_resolved_states_is_not_a_resolution(self):
        # Tenant state labels are free text with no lookup endpoint (8.1), so a label
        # that merely sounds final must not be treated as one.
        self.link_ticket(state="New")
        session = self.poll_session([incident_body(state="On Hold")])
        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))
        self.assertFalse(Finding.objects.get(pk=self.finding.pk).is_mitigated)

    def test_the_external_timestamp_is_recorded(self):
        ticket = self.link_ticket(state="New")
        session = self.poll_session([incident_body(state="Assigned", updated_at="2026-09-18T11:22:33Z")])

        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        ticket.refresh_from_db()
        self.assertEqual(ticket.external_updated_at, datetime(2026, 9, 18, 11, 22, 33, tzinfo=UTC))

    def test_a_nested_state_object_is_tolerated(self):
        # state writes as a bare label, but category on this same API is nested, so a
        # nested read shape must not silently reconcile nothing.
        self.link_ticket(state="New")
        session = self.poll_session([{"id": 9001, "state": {"name": "Resolved"}, "updated_at": None}])

        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        self.assertTrue(Finding.objects.get(pk=self.finding.pk).is_mitigated)

    def test_the_mitigation_note_names_the_incident(self):
        self.link_ticket(incident_id="9001", state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")])

        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        notes = [note.entry for note in Finding.objects.get(pk=self.finding.pk).notes.all()]
        self.assertTrue(
            any("INC-9001" in entry and "Resolved" in entry for entry in notes),
            f"expected an audit note naming the incident and state, got {notes}",
        )

    def test_the_synthetic_actor_is_credited(self):
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")])

        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(finding.mitigated_by.username, sw_helper.SOLARWINDS_ACTOR_USERNAME)
        actor = Dojo_User.objects.get(username=sw_helper.SOLARWINDS_ACTOR_USERNAME)
        self.assertFalse(actor.has_usable_password(), "the automation actor must not be loginable")


# ---------------------------------------------------------------------------
# Inbound: reactivation
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestInboundReactivation(SolarWindsSyncTestCase):

    """
    A ticket's life is not one-way.

    Resolve -> finding mitigated, technician reopens for more work -> finding must come
    back. Without the reopen branch the cursor still advanced to record the new open
    state, so when the incident was resolved a second time ``_mitigate_from_incident``
    short-circuited on ``finding.is_mitigated`` and logged a debug line: the finding was
    stuck closed forever while its ticket carried on, invisible to any operator.

    The guard is that this integration only ever undoes **its own** closures. A finding
    a person closed is left alone even when its linked ticket reopens - the same "the
    human verdict wins" rule the mitigation side already applies to false positives,
    pointed the other way.
    """

    def lifecycle_session(self, *states):
        """One poll response per entry, so a whole ticket lifecycle runs as N runs."""
        return FakeSession(routes={
            ("GET", "/incidents"): [
                list_page([incident_body(state=state, updated_at=f"2026-09-18T1{index}:00:00Z")])
                for index, state in enumerate(states)
            ],
        })

    def poll(self, session):
        return sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

    def close_by_hand(self, user):
        finding_helper.close_finding(
            finding=self.finding,
            user=user,
            is_mitigated=True,
            mitigated=timezone.now(),
            mitigated_by=user,
            false_p=False,
            out_of_scope=False,
            duplicate=False,
        )

    def test_a_reopened_incident_reactivates_a_finding_this_integration_closed(self):
        ticket = self.link_ticket(state="Assigned")
        session = self.lifecycle_session("Resolved", "Assigned")

        self.assertEqual(self.poll(session).mitigated, 1)
        self.assertTrue(Finding.objects.get(pk=self.finding.pk).is_mitigated)

        summary = self.poll(session)

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(summary.reactivated, 1)
        self.assertTrue(finding.active)
        self.assertFalse(finding.is_mitigated)
        self.assertIsNone(finding.mitigated)
        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Assigned")

    def test_a_human_closure_is_not_reversed_by_a_reopened_incident(self):
        # The important half. A person closing a finding may have verified the issue is
        # genuinely fixed for reasons that have nothing to do with the ticket; an
        # automation has no way to evaluate that and must not overrule it.
        human = Dojo_User.objects.get(username="admin")
        self.link_ticket(state="Resolved")
        self.close_by_hand(human)
        session = self.lifecycle_session("Assigned")

        summary = self.poll(session)

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(summary.reactivated, 0)
        self.assertTrue(finding.is_mitigated)
        self.assertFalse(finding.active)
        self.assertEqual(finding.mitigated_by, human)

    def test_a_human_closure_still_advances_the_cursor(self):
        # Log and skip, don't override - but the cursor must still move, or every
        # subsequent poll re-evaluates the same incident from scratch.
        human = Dojo_User.objects.get(username="admin")
        ticket = self.link_ticket(state="Resolved")
        self.close_by_hand(human)

        summary = self.poll(self.lifecycle_session("Assigned"))

        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Assigned")
        self.assertEqual(summary.cursor_advanced, 1)
        self.assertEqual(summary.skipped, 1)

    def test_a_false_positive_verdict_survives_a_reopened_incident(self):
        # Marked false positive after the automated close: the integration's actor is
        # still on mitigated_by, so the actor guard alone would let this through.
        self.link_ticket(state="Assigned")
        self.poll(self.lifecycle_session("Resolved"))
        Finding.objects.filter(pk=self.finding.pk).update(false_p=True)

        summary = self.poll(self.lifecycle_session("Assigned"))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(summary.reactivated, 0)
        self.assertTrue(finding.is_mitigated)
        self.assertTrue(finding.false_p)

    def test_reopening_an_already_open_finding_is_a_no_op(self):
        ticket = self.link_ticket(state="Resolved")

        summary = self.poll(self.lifecycle_session("Assigned"))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(summary.reactivated, 0)
        self.assertTrue(finding.active)
        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Assigned")

    def test_reactivation_is_idempotent_across_repeated_polls(self):
        # R20 again: the same "Assigned" incident comes back every ten minutes, and
        # re-reactivating would append a note on each one.
        self.link_ticket(state="Assigned")
        self.poll(self.lifecycle_session("Resolved"))
        notes_after_close = self.finding.notes.count()

        session = self.lifecycle_session("Assigned", "Assigned")
        first = self.poll(session)
        second = self.poll(session)

        self.assertEqual(first.reactivated, 1)
        self.assertEqual(second.reactivated, 0)
        self.assertEqual(self.finding.notes.count(), notes_after_close + 1)

    def test_a_full_resolve_reopen_resolve_cycle_ends_mitigated(self):
        # The bug this closes, end to end: before the reopen branch existed the second
        # resolution was a silent no-op and the finding never came back.
        self.link_ticket(state="Assigned")
        session = self.lifecycle_session("Resolved", "Assigned", "Resolved")

        self.poll(session)
        self.poll(session)
        summary = self.poll(session)

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertEqual(summary.mitigated, 1)
        self.assertTrue(finding.is_mitigated)
        self.assertFalse(finding.active)

    def test_the_reactivation_note_names_the_incident(self):
        self.link_ticket(state="Assigned")
        self.poll(self.lifecycle_session("Resolved"))

        self.poll(self.lifecycle_session("Awaiting Input"))

        note = self.finding.notes.order_by("-id").first().entry
        self.assertIn("INC-9001", note)
        self.assertIn("Awaiting Input", note)
        self.assertIn("acme-servicedesk", note)

    def test_a_reactivated_finding_is_not_ticketed_a_second_time(self):
        # Reopening saves the finding, which re-enters the outbound hook on a
        # push_all_findings product. The existing link row is what stops a duplicate.
        self.link_ticket(state="Assigned")
        self.poll(self.lifecycle_session("Resolved"))
        session = FakeSession(default=FakeResponse(200, json_body=incident_body()))

        with self.api(session):
            self.poll(self.lifecycle_session("Assigned"))

        self.assertEqual(session.calls_to("POST", "/incidents"), [])
        self.assertEqual(SolarWinds_Ticket.objects.filter(finding=self.finding).count(), 1)


# ---------------------------------------------------------------------------
# Inbound: idempotency (R20)
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestInboundIdempotency(SolarWindsSyncTestCase):

    def test_two_poll_runs_of_the_same_resolved_incident_mitigate_exactly_once(self):
        # The normal steady state, not an edge case: the narrowest window the API
        # offers is one day (R20), so a 10-minute poll re-reads the same incident ~144
        # times before it falls out of the window.
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")], runs=2)

        first = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))
        notes_after_first = self.note_count()
        mitigated_at = Finding.objects.get(pk=self.finding.pk).mitigated

        second = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        self.assertEqual(first.mitigated, 1)
        self.assertEqual(second.mitigated, 0, "the second run must be a no-op")
        self.assertEqual(self.note_count(), notes_after_first, "no duplicate audit note")
        self.assertEqual(
            Finding.objects.get(pk=self.finding.pk).mitigated, mitigated_at,
            "the mitigation timestamp must not be rewritten on re-observation",
        )

    def test_the_second_run_writes_nothing_at_all(self):
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")], runs=2)
        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        second = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        # Cursor already holds "Resolved" and the timestamp is unchanged, so there is
        # nothing to save - otherwise this would be one UPDATE per ticket per poll,
        # forever.
        self.assertEqual(second.cursor_advanced, 0)
        self.assertEqual(second.matched, 1)

    def test_an_already_mitigated_finding_is_left_alone_even_with_a_stale_cursor(self):
        # Second, cursor-independent idempotency layer: covers a finding closed through
        # the UI, or a cursor lost to a manual edit.
        ticket = self.link_ticket(state="Assigned")
        Finding.objects.filter(pk=self.finding.pk).update(
            is_mitigated=True, active=False, mitigated=timezone.now(),
        )
        notes_before = self.note_count()
        session = self.poll_session([incident_body(state="Resolved")])

        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        self.assertEqual(summary.mitigated, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(self.note_count(), notes_before)
        # The cursor still advances, so the next run short-circuits earlier.
        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Resolved")

    def test_a_human_false_positive_verdict_is_not_overridden(self):
        ticket = self.link_ticket(state="Assigned")
        Finding.objects.filter(pk=self.finding.pk).update(false_p=True, active=False)
        session = self.poll_session([incident_body(state="Resolved")])

        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertFalse(finding.is_mitigated)
        self.assertTrue(finding.false_p, "the close path must not clear a human's false-positive flag")
        self.assertEqual(summary.skipped, 1)
        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_state, "Resolved")

    def test_a_duplicate_flag_survives_an_automated_close(self):
        # close_finding() assigns false_p/out_of_scope/duplicate unconditionally, so
        # the reconciler passes the finding's current values back in rather than False.
        self.link_ticket(state="Assigned")
        Finding.objects.filter(pk=self.finding.pk).update(duplicate=True)
        session = self.poll_session([incident_body(state="Resolved")])

        sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        finding = Finding.objects.get(pk=self.finding.pk)
        self.assertTrue(finding.is_mitigated)
        self.assertTrue(finding.duplicate)

    def test_incidents_we_never_created_are_ignored(self):
        # The day-wide window returns the tenant's whole day of activity (R20); almost
        # none of it is ours.
        self.link_ticket(incident_id="9001", state="New")
        session = self.poll_session([
            incident_body(incident_id=7, state="Resolved"),
            incident_body(incident_id=8, state="Closed"),
            incident_body(incident_id=9001, state="New"),
        ])

        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))

        self.assertEqual(summary.seen, 3)
        self.assertEqual(summary.matched, 1)
        self.assertEqual(summary.mitigated, 0)
        self.assertFalse(Finding.objects.get(pk=self.finding.pk).is_mitigated)

    def test_an_instance_with_no_tickets_makes_no_api_call(self):
        session = self.poll_session([incident_body(state="Resolved")])
        summary = sw_helper.reconcile_resolved_incidents(self.instance, client=self.client_for(session))
        self.assertEqual(session.calls, [])
        self.assertEqual(summary.seen, 0)

    def test_a_second_tenant_reusing_an_incident_id_does_not_touch_our_finding(self):
        # Incident ids are unique per *tenant*, not globally, which is why the link row
        # carries solarwinds_instance. Without that scope this poll would join on
        # incident_id alone and mitigate a finding belonging to the other tenant.
        other = SolarWinds_Instance(
            configuration_name="other-tenant",
            subdomain="other",
            open_state="New",
            resolved_states=["Resolved"],
            severity_priority_map={"High": "High"},
            poll_enabled=True,
        )
        other.set_api_token("other-token")
        other.save()

        # Our finding holds incident 9001 on the acme tenant...
        self.link_ticket(incident_id="9001", state="Assigned", instance=self.instance)
        # ...and an unrelated finding holds a different incident on the other tenant,
        # so the other tenant genuinely has tickets and really does issue the query.
        other_finding = Finding.objects.exclude(pk=self.finding.pk).filter(active=True).first()
        other_ticket = SolarWinds_Ticket.objects.create(
            finding=other_finding, solarwinds_instance=other,
            incident_id="5555", last_synced_state="New",
        )

        # The other tenant's own incident 9001 is a completely different ticket.
        session = self.poll_session([incident_body(incident_id=9001, state="Resolved")])
        summary = sw_helper.reconcile_resolved_incidents(other, client=self.client_for(session, instance=other))

        self.assertEqual(len(session.calls_to("GET", "/incidents")), 1, "the other tenant does query")
        self.assertEqual(summary.seen, 1)
        self.assertEqual(summary.matched, 0, "incident 9001 is not one of this tenant's tickets")
        self.assertFalse(Finding.objects.get(pk=self.finding.pk).is_mitigated)
        self.assertFalse(Finding.objects.get(pk=other_finding.pk).is_mitigated)
        other_ticket.refresh_from_db()
        self.assertEqual(other_ticket.last_synced_state, "New", "an unmatched incident moves no cursor")


# ---------------------------------------------------------------------------
# Inbound: the beat task
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestPollTask(SolarWindsSyncTestCase):

    def test_the_task_polls_only_poll_enabled_instances(self):
        disabled = SolarWinds_Instance(
            configuration_name="poll-off",
            subdomain="polloff",
            open_state="New",
            resolved_states=["Resolved"],
            severity_priority_map={"High": "High"},
            poll_enabled=False,
        )
        disabled.set_api_token("t")
        disabled.save()
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")])

        with self.api(session):
            summaries = sw_tasks.poll_resolved_incidents()

        self.assertEqual(list(summaries), [self.instance.id])
        self.assertTrue(Finding.objects.get(pk=self.finding.pk).is_mitigated)

    def test_the_query_is_filtered_to_a_day_bucket_not_an_arbitrary_timestamp(self):
        # R20: the API offers only fixed day buckets or a day-granular range. Asking
        # for the beat interval is not expressible, so correctness comes from the
        # cursor, and the query must still carry a filter or it would be a full scan
        # that blows the rate limit (R7).
        self.link_ticket(state="Assigned")
        session = self.poll_session([incident_body(state="Resolved")])
        with self.api(session):
            sw_tasks.poll_resolved_incidents()

        params = session.calls_to("GET", "/incidents")[0]["params"]
        self.assertEqual(params["updated"], str(sw_helper.POLL_WINDOW_DAYS))
        self.assertIn(sw_helper.POLL_WINDOW_DAYS, sw_client.UPDATED_DAYS_BUCKETS)

    def test_one_failing_instance_does_not_stop_the_others(self):
        broken = SolarWinds_Instance(
            configuration_name="broken-tenant",
            subdomain="broken",
            open_state="New",
            resolved_states=["Resolved"],
            severity_priority_map={"High": "High"},
            poll_enabled=True,
        )
        broken.set_api_token("stale-token")
        broken.save()
        other_finding = Finding.objects.exclude(pk=self.finding.pk).filter(active=True).first()
        SolarWinds_Ticket.objects.create(
            finding=other_finding, solarwinds_instance=broken,
            incident_id="4242", last_synced_state="Assigned",
        )
        self.link_ticket(state="Assigned")

        session = FakeSession(
            routes={("GET", "/incidents"): [
                # Ordering is by configuration_name, so "acme-servicedesk" is polled
                # before "broken-tenant".
                list_page([incident_body(state="Resolved")]),
                FakeResponse(401, text="unauthorized"),
            ]},
        )
        with self.api(session):
            summaries = sw_tasks.poll_resolved_incidents()

        self.assertIn(self.instance.id, summaries)
        self.assertNotIn(broken.id, summaries)
        self.assertTrue(Finding.objects.get(pk=self.finding.pk).is_mitigated)
        self.assertTrue(self.solarwinds_alerts("poll failed").exists())


# ---------------------------------------------------------------------------
# Health check (R6)
# ---------------------------------------------------------------------------


@versioned_fixtures
class TestTokenHealthCheck(SolarWindsSyncTestCase):

    def test_a_working_token_reports_healthy_and_raises_no_alert(self):
        session = FakeSession(routes={("GET", "/sites"): list_page([{"id": 1, "name": "HQ"}])})
        self.assertTrue(sw_helper.verify_instance_token(self.instance, client=self.client_for(session)))
        self.assertFalse(self.solarwinds_alerts("solarwinds").exists())

    def test_the_probe_is_a_single_cheap_request(self):
        session = FakeSession(routes={("GET", "/sites"): list_page([{"id": 1}])})
        sw_helper.verify_instance_token(self.instance, client=self.client_for(session))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["params"]["per_page"], 1)

    def test_a_rejected_token_raises_a_visible_alert(self):
        # This is the whole of R6: a regenerated service-account token produces no
        # other symptom, pushes and polls just stop.
        session = FakeSession(routes={("GET", "/sites"): FakeResponse(401, text="unauthorized")})

        self.assertFalse(sw_helper.verify_instance_token(self.instance, client=self.client_for(session)))

        alerts = self.solarwinds_alerts("token rejected")
        self.assertTrue(alerts.exists())
        self.assertIn("acme-servicedesk", alerts.first().title)

    def test_a_403_is_treated_as_a_credential_rejection_too(self):
        session = FakeSession(routes={("GET", "/sites"): FakeResponse(403, text="forbidden")})
        self.assertFalse(sw_helper.verify_instance_token(self.instance, client=self.client_for(session)))
        self.assertTrue(self.solarwinds_alerts("token rejected").exists())

    def test_an_undecryptable_token_is_reported_as_misconfiguration_not_rejection(self):
        self.instance.api_token = ""
        self.instance.save()
        self.assertFalse(sw_helper.verify_instance_token(self.instance, client=self.client_for(FakeSession())))
        self.assertTrue(self.solarwinds_alerts("misconfigured").exists())
        self.assertFalse(self.solarwinds_alerts("token rejected").exists())

    def test_an_unreachable_tenant_is_not_reported_as_healthy(self):
        session = FakeSession(routes={("GET", "/sites"): FakeResponse(500, text="boom")})
        self.assertFalse(sw_helper.verify_instance_token(self.instance, client=self.client_for(session)))
        self.assertTrue(self.solarwinds_alerts("health check failed").exists())

    def test_push_only_instances_are_health_checked(self):
        # The configuration where a rotated token stays invisible longest: nothing
        # scheduled touches a push-only tenant, so without this it would only surface
        # at the next finding save.
        self.instance.poll_enabled = False
        self.instance.save()
        self.assertIn(self.instance, list(sw_helper.instances_needing_health_check()))

    def test_fully_disabled_instances_are_not_probed(self):
        self.instance.push_enabled = False
        self.instance.poll_enabled = False
        self.instance.save()
        self.assertNotIn(self.instance, list(sw_helper.instances_needing_health_check()))

    def test_the_task_reports_per_instance_results(self):
        session = FakeSession(routes={("GET", "/sites"): FakeResponse(401, text="unauthorized")})
        with self.api(session):
            results = sw_tasks.check_instance_tokens()
        self.assertEqual(results, {self.instance.id: False})


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestSolarWindsTaskWiring(DojoTestCase):

    """Registration, not behaviour: a beat entry naming an unregistered task fails at run time."""

    def test_all_three_tasks_are_registered_with_celery(self):
        for name in (
            "dojo.solarwinds.tasks.push_finding_to_solarwinds",
            "dojo.solarwinds.tasks.poll_resolved_incidents",
            "dojo.solarwinds.tasks.check_instance_tokens",
        ):
            with self.subTest(task=name):
                self.assertIn(name, app.tasks)

    def test_the_tasks_module_is_in_celery_imports(self):
        # autodiscover_tasks only scans <installed_app>/tasks.py, and dojo.solarwinds is
        # a package inside the dojo app, so without this the beat entries below would
        # raise NotRegistered in the worker.
        self.assertIn("dojo.solarwinds.tasks", settings.CELERY_IMPORTS)

    def test_the_poll_is_on_the_beat_schedule(self):
        entry = settings.CELERY_BEAT_SCHEDULE["solarwinds-poll-resolved"]
        self.assertEqual(entry["task"], "dojo.solarwinds.tasks.poll_resolved_incidents")
        self.assertIn("expires", entry["options"])

    def test_the_health_check_is_on_the_beat_schedule(self):
        entry = settings.CELERY_BEAT_SCHEDULE["solarwinds-token-health-check"]
        self.assertEqual(entry["task"], "dojo.solarwinds.tasks.check_instance_tokens")
        self.assertIn("expires", entry["options"])

    def test_the_beat_entries_name_tasks_that_actually_exist(self):
        # Not asserted across the whole schedule: in a test process only the modules
        # this suite imports are loaded, so unrelated entries (dojo.tasks.*) legitimately
        # look unregistered here even though the worker's autodiscovery finds them.
        for name in ("solarwinds-poll-resolved", "solarwinds-token-health-check"):
            with self.subTest(entry=name):
                dotted = settings.CELERY_BEAT_SCHEDULE[name]["task"]
                module, _, attr = dotted.rpartition(".")
                self.assertEqual(module, "dojo.solarwinds.tasks")
                self.assertTrue(hasattr(sw_tasks, attr), f"{dotted} does not exist")
