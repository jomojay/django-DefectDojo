"""
Unit tests for the SolarWinds Service Desk integration (INTEGRATIONS_ROADMAP.md 8.2-8.5).

Scope is deliberately narrow: models, credential encryption, the payload builder and
the client's pure helpers. Nothing here touches the network. The mocked-API harness
for create/poll round trips belongs to the Epic 3 pass.

The assertions worth protecting from a future "cleanup" are the wire-shape ones -
``state`` and ``priority`` are bare label strings while ``category`` is a nested
object, and that asymmetry is real, confirmed against a live tenant, and contradicts
the OpenAPI spec for the first two.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from dojo.models import Dojo_User, Finding, Product, SolarWinds_Instance, SolarWinds_Product, SolarWinds_Ticket
from dojo.solarwinds import client as sw_client
from dojo.solarwinds import services as sw_services
from dojo.solarwinds.validators import validate_subdomain
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


def make_instance(**overrides):
    """Build (unsaved) an instance with a realistic trial-tenant configuration."""
    defaults = {
        "configuration_name": "acme-servicedesk",
        "subdomain": "acme",
        "open_state": "New",
        "resolved_states": ["Resolved", "Closed"],
        # Deliberately no "Info" key - the skip-on-unmapped-severity decision (8.5).
        "severity_priority_map": {
            "Critical": "Critical",
            "High": "High",
            "Medium": "Medium",
            "Low": "Low",
        },
        "push_enabled": True,
    }
    defaults.update(overrides)
    return SolarWinds_Instance(**defaults)


class TestSolarWindsSubdomainValidator(DojoTestCase):

    """The subdomain is interpolated into the API base URL, so it is a request-forgery surface."""

    def test_accepts_plain_labels(self):
        for value in ("acme", "acme-corp", "a1", "example-tenant"):
            with self.subTest(value=value):
                validate_subdomain(value)

    def test_rejects_values_that_would_retarget_the_host(self):
        hostile = [
            "evil.example.com",
            "acme.samanage.com",
            "https://evil.example.com",
            "acme/../../evil",
            "acme#",
            "acme?x=1",
            "acme@evil.example.com",
            "",
            "-acme",
            "acme-",
        ]
        for value in hostile:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_subdomain(value)


class TestSolarWindsInstanceModel(DojoTestCase):

    def test_base_url_uses_the_tenant_subdomain_not_a_regional_host(self):
        # The shared regional hosts 404 on /incidents (8.1); this must never
        # regress into api.samanage.com.
        self.assertEqual(make_instance(region="EU").base_url, "https://acme.samanage.com")

    def test_subdomain_is_normalised_on_save(self):
        instance = make_instance(subdomain="  ACME  ")
        instance.set_api_token("t")
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.subdomain, "acme")

    def test_api_token_round_trips_through_encryption(self):
        instance = make_instance()
        instance.set_api_token("super-secret-token")
        instance.save()

        stored = SolarWinds_Instance.objects.get(pk=instance.pk)
        # Stored ciphertext, not the plaintext - this is the JIRA anti-pattern (R11)
        # the module exists to avoid.
        self.assertNotIn("super-secret-token", stored.api_token)
        self.assertTrue(stored.api_token.startswith("AES."))
        self.assertEqual(stored.get_api_token(), "super-secret-token")

    def test_get_api_token_is_empty_when_unset(self):
        self.assertEqual(SolarWinds_Instance(api_token="").get_api_token(), "")

    def test_priority_allowlist_is_the_map_values(self):
        self.assertEqual(
            make_instance().priority_allowlist,
            frozenset({"Critical", "High", "Medium", "Low"}),
        )

    def test_priority_for_unmapped_severity_is_none(self):
        self.assertIsNone(make_instance().priority_for_severity("Info"))

    def test_is_resolved_state(self):
        instance = make_instance()
        self.assertTrue(instance.is_resolved_state("Closed"))
        self.assertFalse(instance.is_resolved_state("Awaiting Input"))
        self.assertFalse(instance.is_resolved_state(""))

    def test_clean_rejects_unknown_severity_keys(self):
        instance = make_instance(severity_priority_map={"Catastrophic": "Critical"})
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("severity_priority_map", ctx.exception.message_dict)

    def test_clean_rejects_blank_priority_label(self):
        instance = make_instance(severity_priority_map={"High": ""})
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("severity_priority_map", ctx.exception.message_dict)

    def test_clean_rejects_non_list_resolved_states(self):
        instance = make_instance(resolved_states="Closed")
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("resolved_states", ctx.exception.message_dict)

    def test_clean_accepts_a_partial_map(self):
        # A tenant mapping only Critical/High is valid; the rest are simply skipped.
        make_instance(severity_priority_map={"Critical": "Critical", "High": "High"}).clean()


@versioned_fixtures
class TestAutoPushSeverities(DojoTestCase):

    """
    The auto/manual split: ``auto_push_severities`` is a subset of the severity map.

    ``severity_priority_map`` says what can *ever* be pushed; this says what fires with
    nobody asking. The routing consequences are tested in ``test_solarwinds_sync.py``;
    what is pinned here is the field's own contract and its configuration-time guard.
    """

    fixtures = ["dojo_testdata.json"]

    def test_the_default_is_empty_not_the_whole_severity_map(self):
        # The load-bearing default. "Everything mapped" would mean an operator who
        # upgrades into this field gets the exact flood it exists to prevent.
        self.assertEqual(SolarWinds_Instance().auto_push_severities, [])
        self.assertEqual(make_instance().auto_push_severities, [])

    def test_is_auto_push_severity_is_membership_in_the_list(self):
        instance = make_instance(auto_push_severities=["Critical", "High"])
        self.assertTrue(instance.is_auto_push_severity("Critical"))
        self.assertTrue(instance.is_auto_push_severity("High"))
        self.assertFalse(instance.is_auto_push_severity("Medium"))
        self.assertFalse(instance.is_auto_push_severity("Low"))

    def test_a_mapped_severity_is_not_automatically_an_auto_push_severity(self):
        # The whole point of the split. Medium has a priority label, so it can be
        # pushed - but only on purpose.
        instance = make_instance(auto_push_severities=["Critical", "High"])
        self.assertEqual(instance.priority_for_severity("Medium"), "Medium")
        self.assertFalse(instance.is_auto_push_severity("Medium"))

    def test_an_empty_list_auto_pushes_nothing(self):
        instance = make_instance()
        for severity in ("Critical", "High", "Medium", "Low", "Info"):
            with self.subTest(severity=severity):
                self.assertFalse(instance.is_auto_push_severity(severity))

    def test_a_blank_severity_is_never_an_auto_push(self):
        instance = make_instance(auto_push_severities=["Critical"])
        self.assertFalse(instance.is_auto_push_severity(""))
        self.assertFalse(instance.is_auto_push_severity(None))

    def test_clean_accepts_a_subset_of_the_severity_map(self):
        make_instance(auto_push_severities=["Critical", "High"]).clean()

    def test_clean_accepts_an_empty_list(self):
        make_instance(auto_push_severities=[]).clean()

    def test_clean_rejects_an_auto_push_severity_with_no_priority_label(self):
        # Nonsensical rather than merely useless: build_incident_payload raises
        # UnmappedSeverityError for it, so the push would be dropped inside a Celery
        # task with nothing to show an operator.
        instance = make_instance(
            severity_priority_map={"Critical": "Critical"},
            auto_push_severities=["Critical", "High"],
        )
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("auto_push_severities", ctx.exception.message_dict)
        self.assertIn("High", str(ctx.exception.message_dict["auto_push_severities"]))

    def test_clean_rejects_info_even_though_it_is_a_real_severity(self):
        # Info is a valid DefectDojo severity but is deliberately never in the map
        # (8.5), so it can never be an auto-push severity either - the subset rule
        # carries the Info decision for free rather than needing its own special case.
        instance = make_instance(auto_push_severities=["Info"])
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("auto_push_severities", ctx.exception.message_dict)

    def test_clean_rejects_a_non_list(self):
        instance = make_instance(auto_push_severities={"Critical": True})
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("auto_push_severities", ctx.exception.message_dict)

    def test_clean_rejects_blank_entries(self):
        instance = make_instance(auto_push_severities=["Critical", "  "])
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("auto_push_severities", ctx.exception.message_dict)

    def test_a_broken_severity_map_is_reported_on_its_own_field_not_this_one(self):
        # Both fields are wrong; the subset check must not pile a confusing second
        # message onto a map that was already rejected for its own reasons.
        instance = make_instance(
            severity_priority_map={"Catastrophic": "Critical"},
            auto_push_severities=["Critical"],
        )
        with self.assertRaises(ValidationError) as ctx:
            instance.clean()
        self.assertIn("severity_priority_map", ctx.exception.message_dict)
        self.assertNotIn("auto_push_severities", ctx.exception.message_dict)


@versioned_fixtures
class TestSolarWindsProductModel(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def test_one_mapping_per_instance_and_product(self):
        instance = make_instance()
        instance.set_api_token("t")
        instance.save()
        product = Product.objects.first()

        SolarWinds_Product.objects.create(solarwinds_instance=instance, product=product)
        with self.assertRaises(IntegrityError):
            SolarWinds_Product.objects.create(solarwinds_instance=instance, product=product)


@versioned_fixtures
class TestSolarWindsTicketModel(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def test_ticket_links_to_a_finding(self):
        instance = make_instance()
        instance.set_api_token("t")
        instance.save()
        finding = Finding.objects.first()

        ticket = SolarWinds_Ticket.objects.create(
            finding=finding, incident_id="12345", incident_number="INC-42", last_synced_state="New",
        )
        self.assertEqual(finding.solarwinds_ticket, ticket)
        self.assertIn("INC-42", str(ticket))


@versioned_fixtures
class TestBuildIncidentPayload(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.instance = make_instance(default_category="Security", default_site_id="11", default_department_id="22")
        self.finding = Finding.objects.filter(severity="High").first()
        if self.finding is None:
            self.finding = Finding.objects.first()
            self.finding.severity = "High"

    def payload(self, **kwargs):
        return sw_services.build_incident_payload(self.finding, self.instance, **kwargs)["incident"]

    def test_payload_is_wrapped_in_an_incident_envelope(self):
        built = sw_services.build_incident_payload(self.finding, self.instance)
        self.assertEqual(list(built.keys()), ["incident"])

    def test_state_is_a_bare_label_under_the_state_key(self):
        incident = self.payload()
        # NOT "state_id", NOT a nested object, NOT numeric - the spec is wrong here
        # and numeric ids get a 422 from the real API (8.1).
        self.assertEqual(incident["state"], "New")
        self.assertNotIn("state_id", incident)

    def test_priority_is_a_bare_label(self):
        self.assertEqual(self.payload()["priority"], "High")

    def test_category_is_a_nested_name_object(self):
        # Unlike state/priority, this one does match the spec. Confirmed separately.
        self.assertEqual(self.payload()["category"], {"name": "Security"})

    def test_category_is_omitted_when_unconfigured(self):
        self.instance.default_category = ""
        self.assertNotIn("category", self.payload())

    def test_name_is_prefixed_and_truncated_to_255(self):
        self.finding.title = "x" * 400
        name = self.payload()["name"]
        self.assertTrue(name.startswith("[DefectDojo] "))
        self.assertEqual(len(name), 255)

    def test_tag_list_links_back_to_the_finding(self):
        self.assertEqual(self.payload()["tag_list"], f"defectdojo,finding-{self.finding.id}")

    def test_description_carries_severity_and_a_deep_link(self):
        description = self.payload()["description"]
        self.assertIn("Severity: High", description)
        self.assertIn(self.finding.get_absolute_url(), description)

    def test_instance_defaults_are_used_without_a_product_mapping(self):
        incident = self.payload()
        self.assertEqual(incident["site_id"], "11")
        self.assertEqual(incident["department_id"], "22")
        self.assertNotIn("group_assignee_id", incident)

    def test_product_mapping_overrides_instance_defaults(self):
        mapping = SolarWinds_Product(site_id="99", group_assignee_id="grp-7")
        incident = self.payload(solarwinds_product=mapping)
        self.assertEqual(incident["site_id"], "99")
        # department_id was left blank on the mapping, so the instance default wins.
        self.assertEqual(incident["department_id"], "22")
        self.assertEqual(incident["group_assignee_id"], "grp-7")

    def test_blank_defaults_are_omitted_entirely(self):
        self.instance.default_site_id = ""
        self.instance.default_department_id = ""
        incident = self.payload()
        self.assertNotIn("site_id", incident)
        self.assertNotIn("department_id", incident)


@versioned_fixtures
class TestIncidentAssignee(DojoTestCase):

    """
    ``Product.technical_contact`` -> ``incident.assignee``.

    The wire shape is a nested ``{"email": ...}`` object, verified against
    ``resolved_schema.json``'s ``POST /incidents`` request body rather than assumed:
    ``assignee`` is typed ``anyOf`` [object requiring ``email``, the nil enum]. Same
    *family* as ``category`` (nested, spec-accurate) and not the bare-label shape
    ``state``/``priority`` use - this API is not uniform and these tests exist to keep
    a future tidy-up from making it so.
    """

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.instance = make_instance()
        self.finding = Finding.objects.filter(severity="High").first() or Finding.objects.first()
        self.finding.severity = "High"
        self.product = self.finding.test.engagement.product
        self.contact = Dojo_User.objects.create(
            username="tech-contact", email="tech.contact@example.invalid",
        )

    def payload(self, **kwargs):
        return sw_services.build_incident_payload(self.finding, self.instance, **kwargs)["incident"]

    def set_contact(self, user):
        self.product.technical_contact = user
        self.product.save()
        # Re-read through the finding's own relation chain, which is what the payload
        # builder walks; assigning to self.product alone would not prove that.
        self.finding = Finding.objects.get(pk=self.finding.pk)
        self.finding.severity = "High"

    def test_the_technical_contact_becomes_a_nested_email_object(self):
        self.set_contact(self.contact)
        self.assertEqual(
            self.payload()["assignee"],
            {"email": "tech.contact@example.invalid"},
        )

    def test_assignee_is_omitted_when_there_is_no_technical_contact(self):
        self.set_contact(None)
        self.assertNotIn("assignee", self.payload())

    def test_assignee_is_omitted_when_the_contact_has_no_email(self):
        # A Dojo_User can exist with a blank email. Sending {"email": ""} would be a
        # malformed assignee rather than "no assignee", and the schema's nil form would
        # actively *clear* one - so the field is dropped instead.
        self.contact.email = ""
        self.contact.save()
        self.set_contact(self.contact)
        self.assertNotIn("assignee", self.payload())

    def test_a_whitespace_only_email_is_treated_as_absent(self):
        self.contact.email = "   "
        self.contact.save()
        self.set_contact(self.contact)
        self.assertNotIn("assignee", self.payload())

    def test_assignee_and_group_assignee_coexist(self):
        # A person and a support group are separate fields on a Samanage incident, not
        # alternatives. Neither may suppress the other.
        self.set_contact(self.contact)
        incident = self.payload(solarwinds_product=SolarWinds_Product(group_assignee_id="grp-7"))
        self.assertEqual(incident["assignee"], {"email": "tech.contact@example.invalid"})
        self.assertEqual(incident["group_assignee_id"], "grp-7")

    def test_assignee_does_not_use_the_bare_label_shape(self):
        self.set_contact(self.contact)
        incident = self.payload()
        self.assertNotIsInstance(incident["assignee"], str)
        self.assertNotIn("assignee_id", incident)
        # ...while state/priority in the same payload still are bare labels.
        self.assertIsInstance(incident["state"], str)
        self.assertIsInstance(incident["priority"], str)

    def test_the_helper_returns_an_empty_string_for_a_detached_finding(self):
        # build_incident_description tolerates a finding with no product chain; the
        # assignee lookup has to tolerate exactly the same thing.
        self.assertEqual(sw_services.technical_contact_email(Finding()), "")


@versioned_fixtures
class TestSkipOnUnmappedSeverity(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.instance = make_instance()
        self.finding = Finding.objects.first()

    def test_info_finding_raises_rather_than_defaulting_to_a_priority(self):
        self.finding.severity = "Info"
        with self.assertRaises(sw_services.UnmappedSeverityError):
            sw_services.build_incident_payload(self.finding, self.instance)

    def test_rule_generalises_to_any_unmapped_severity(self):
        # Not an Info special case: a tenant that only maps Critical/High skips the rest.
        self.instance.severity_priority_map = {"Critical": "Critical"}
        self.finding.severity = "Medium"
        with self.assertRaises(sw_services.UnmappedSeverityError):
            sw_services.build_incident_payload(self.finding, self.instance)

    def test_should_push_finding_is_false_for_unmapped_severity(self):
        self.finding.severity = "Info"
        self.assertFalse(sw_services.should_push_finding(self.finding, self.instance))

    def test_should_push_finding_is_false_when_push_disabled(self):
        self.instance.push_enabled = False
        self.finding.severity = "Critical"
        self.assertFalse(sw_services.should_push_finding(self.finding, self.instance))

    def test_should_push_finding_is_true_for_mapped_severity(self):
        self.finding.severity = "Critical"
        self.assertTrue(sw_services.should_push_finding(self.finding, self.instance))


class TestClientPriorityGuard(DojoTestCase):

    """
    The API returns 200 and silently discards an invalid priority (R9/R19).

    These tests are the only thing standing between a typo in the map and a ticket
    queue that is quietly mis-prioritised, so they assert the request never leaves.
    """

    def setUp(self):
        super().setUp()
        self.instance = make_instance()
        self.instance.api_token = "irrelevant"
        self.client = sw_client.SolarWindsClient(self.instance)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_unknown_priority_is_rejected_before_the_request(self):
        with patch.object(self.client, "_request") as mocked:
            with self.assertRaises(sw_client.SolarWindsInvalidPriorityError):
                self.client.create_incident({"incident": {"name": "x", "priority": "Urgent"}})
            mocked.assert_not_called()

    def test_update_is_guarded_too(self):
        with patch.object(self.client, "_request") as mocked:
            with self.assertRaises(sw_client.SolarWindsInvalidPriorityError):
                self.client.update_incident("1", {"incident": {"priority": "Sev1"}})
            mocked.assert_not_called()

    def test_empty_map_refuses_to_send_any_priority(self):
        self.instance.severity_priority_map = {}
        with self.assertRaises(sw_client.SolarWindsConfigurationError):
            self.client.create_incident({"incident": {"priority": "High"}})

    def test_payload_without_a_priority_is_not_blocked(self):
        # A comment-only or state-only update has nothing to validate.
        self.client._validate_priority({"incident": {"state": "Closed"}})

    def test_valid_priority_passes(self):
        self.client._validate_priority({"incident": {"priority": "Medium"}})

    def test_builder_output_passes_the_client_guard(self):
        # The end-to-end invariant: whatever services.py produces, the client accepts.
        for severity, expected in self.instance.severity_priority_map.items():
            with self.subTest(severity=severity):
                self.client._validate_priority({"incident": {"priority": expected}})


class TestClientPlumbing(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.instance = make_instance()
        self.instance.set_api_token("tok-123")
        self.client = sw_client.SolarWindsClient(self.instance)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_headers_carry_the_bearer_token_and_pinned_accept(self):
        headers = self.client._build_headers()
        self.assertEqual(headers["X-Samanage-Authorization"], "Bearer tok-123")
        self.assertEqual(headers["Accept"], "application/vnd.samanage.v2.1+json")

    def test_missing_token_is_a_configuration_error_not_an_anonymous_call(self):
        self.instance.api_token = ""
        with self.assertRaises(sw_client.SolarWindsConfigurationError):
            self.client._build_headers()

    def test_urls_are_anchored_to_the_tenant_host(self):
        self.assertEqual(self.client._url("/incidents"), "https://acme.samanage.com/incidents.json")
        self.assertEqual(self.client._url("incidents/5"), "https://acme.samanage.com/incidents/5.json")

    def test_updated_filter_snaps_a_timestamp_up_to_a_supported_bucket(self):
        # The API has no arbitrary-timestamp filter; only 1/7/14/30/60/90 day buckets.
        now = timezone.now()
        self.assertEqual(
            self.client._build_updated_filter(
                since=now - timedelta(hours=3), days=None, date_from=None, date_to=None,
            ),
            {"updated": "1"},
        )
        self.assertEqual(
            self.client._build_updated_filter(
                since=now - timedelta(days=9), days=None, date_from=None, date_to=None,
            ),
            {"updated": "14"},
        )

    def test_window_wider_than_ninety_days_falls_back_to_a_date_range(self):
        built = self.client._build_updated_filter(
            since=timezone.now() - timedelta(days=200), days=None, date_from=None, date_to=None,
        )
        self.assertEqual(built["updated"], "Select Date Range")
        self.assertIn("updated_custom_gte", built)
        self.assertIn("updated_custom_lte", built)

    def test_explicit_days_must_be_a_supported_bucket(self):
        with self.assertRaises(ValueError):
            self.client._build_updated_filter(since=None, days=3, date_from=None, date_to=None)

    def test_unfiltered_listing_is_refused(self):
        # An unfiltered scan would exhaust the 1000/min rate limit (R7).
        with self.assertRaises(ValueError):
            self.client._build_updated_filter(since=None, days=None, date_from=None, date_to=None)

    def test_samanage_date_format_is_short_month_unpadded_day(self):
        self.assertEqual(sw_client._format_samanage_date(datetime(2023, 3, 2, tzinfo=UTC)), "Mar 2 2023")

    def test_list_response_normalisation(self):
        self.assertEqual(sw_client.SolarWindsClient._as_incident_list([{"id": 1}]), [{"id": 1}])
        self.assertEqual(sw_client.SolarWindsClient._as_incident_list({"incidents": [{"id": 2}]}), [{"id": 2}])
        self.assertEqual(sw_client.SolarWindsClient._as_incident_list({"unexpected": 1}), [])


class _RecordingResponse:

    """Minimal ``requests.Response`` stand-in: enough for ``_handle_response`` and ``_json``."""

    def __init__(self, url):
        self.url = url
        self.status_code = 200
        self.headers = {"X-Total-Pages": "1"}
        self.text = "[]"

    @staticmethod
    def json():
        # A bare array satisfies both the list paths and the single-object callers,
        # which only ever hand the body straight back.
        return []


class _RecordingSession:

    """Captures the URL of every request without ever touching the network."""

    def __init__(self):
        self.urls = []

    def request(self, method, url, **kwargs):
        params = kwargs.get("params") or {}
        # Mirror what requests itself does, so the assertions see the real wire URL.
        query = urlencode(params, doseq=True)
        full = f"{url}?{query}" if query else url
        self.urls.append((method.upper(), full))
        return _RecordingResponse(full)

    def close(self):
        pass


class TestEveryRequestPathCarriesTheJsonExtension(DojoTestCase):

    """
    R33: the ``.json`` extension is mandatory on every request path.

    The live API does **not** honour ``Accept: application/vnd.samanage.v2.1+json``
    on its own - a path without the extension comes back 406 Not Acceptable, on
    every verb, for collections (``/sites``), members (``/incidents/{id}``) and
    sub-resources (``/incidents/{id}/comments``) alike. That was verified live
    against a real tenant on 2026-09-19, in both directions, for GET, PUT and POST.

    This class exists because the original bug was invisible to mocks: a fake
    session answers a bare path just as happily as a suffixed one, so four build
    passes of green tests said nothing about whether the client could talk to a
    real tenant. These assertions pin the wire format itself rather than the
    behaviour around it.
    """

    def setUp(self):
        super().setUp()
        self.instance = make_instance()
        self.instance.set_api_token("tok-123")
        self.session = _RecordingSession()
        self.client = sw_client.SolarWindsClient(self.instance, session=self.session)

    def _assert_all_paths_are_json(self):
        self.assertTrue(self.session.urls, "expected at least one request to have been issued")
        for method, url in self.session.urls:
            with self.subTest(method=method, url=url):
                path = urlparse(url).path
                self.assertTrue(
                    path.endswith(".json"),
                    f"{method} {url} would be answered with 406 by the live tenant: "
                    f"path {path!r} is missing the '.json' extension (R33)",
                )
                # A doubled extension is the obvious way a later 'fix' goes wrong.
                self.assertFalse(path.endswith(".json.json"), f"{method} {url} has a doubled extension")

    def test_create_incident(self):
        self.client.create_incident({"incident": {"name": "x", "priority": "High"}})
        self.assertEqual(self.session.urls, [("POST", "https://acme.samanage.com/incidents.json")])
        self._assert_all_paths_are_json()

    def test_get_incident(self):
        self.client.get_incident(9001)
        self.assertEqual(self.session.urls, [("GET", "https://acme.samanage.com/incidents/9001.json")])
        self._assert_all_paths_are_json()

    def test_update_incident(self):
        self.client.update_incident(9001, {"incident": {"state": "Resolved"}})
        self.assertEqual(self.session.urls, [("PUT", "https://acme.samanage.com/incidents/9001.json")])
        self._assert_all_paths_are_json()

    def test_add_comment_on_the_sub_resource_path(self):
        # The sub-resource was the one case that could plausibly have behaved
        # differently from a bare collection. It does not - it 406s too.
        self.client.add_comment(9001, "note")
        self.assertEqual(self.session.urls, [("POST", "https://acme.samanage.com/incidents/9001/comments.json")])
        self._assert_all_paths_are_json()

    def test_every_config_collection(self):
        for name in sw_client.COLLECTION_PATHS:
            with self.subTest(collection=name):
                self.client.list_collection(name)
        issued = {urlparse(url).path for _, url in self.session.urls}
        self.assertEqual(
            issued,
            {"/sites.json", "/departments.json", "/categories.json", "/groups.json"},
        )
        self._assert_all_paths_are_json()

    def test_listing_puts_the_extension_before_the_query_string(self):
        # requests appends ``params`` after the URL is built, so the extension has to
        # land on the path - ``/incidents.json?page=1``, never ``/incidents?page=1.json``.
        self.client.list_incidents_updated_since(days=7, per_page=100)
        method, url = self.session.urls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(
            url.startswith("https://acme.samanage.com/incidents.json?"),
            f"query string must follow the extension, got {url}",
        )
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/incidents.json")
        self.assertEqual(parse_qs(parsed.query)["page"], ["1"])
        self.assertEqual(parse_qs(parsed.query)["per_page"], ["100"])
        self.assertEqual(parse_qs(parsed.query)["updated"], ["7"])
        self._assert_all_paths_are_json()

    def test_the_suffix_is_applied_once_not_per_call(self):
        # Idempotent: a caller that already spelled out the extension must not get
        # ``/incidents.json.json``.
        self.assertEqual(self.client._url("/incidents.json"), "https://acme.samanage.com/incidents.json")
        self.assertEqual(self.client._url("/incidents/"), "https://acme.samanage.com/incidents.json")
