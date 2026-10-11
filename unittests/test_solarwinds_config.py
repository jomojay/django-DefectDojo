"""
Tests for the SolarWinds Service Desk configuration surface: discovery helper,
forms, UI views and REST API (INTEGRATIONS_ROADMAP.md 8.2/8.4, R21).

The two assertions here that are worth protecting from a future "cleanup":

* **The token never round-trips.** ``test_saving_twice_without_retyping_keeps_the_ciphertext_byte_identical``
  is R21's explicit test. It is meaningful because ``dojo_crypto_encrypt`` uses a
  fresh random nonce per call, so re-encrypting the same plaintext produces
  *different* ciphertext - proven by its companion control assertion. A form that
  re-encrypted on every save would decrypt to garbage and 401 exactly like a rotated
  token (R6), with no error to trace it back.
* **The observed state/priority labels are not exhaustive.** Discovery returns what
  it saw on real incidents, never a complete list, because this API has no
  ``/states`` or ``/priorities`` endpoint (8.1). The tests assert the caveat travels
  with the data.

No network is touched: the client is a stub object, since ``discovery`` accepts an
injected client precisely so this layer is testable without one.
"""
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from dojo.finding.ui.forms import FindingForm
from dojo.models import (
    Dojo_User,
    Engagement,
    Finding,
    Product,
    Product_Type,
    SolarWinds_Instance,
    SolarWinds_Product,
    SolarWinds_Ticket,
    Test,
    Test_Type,
    UserContactInfo,
)
from dojo.solarwinds import discovery
from dojo.solarwinds.api.serializer import SolarWindsInstanceSerializer
from dojo.solarwinds.client import SolarWindsAuthenticationError, SolarWindsConfigurationError
from dojo.solarwinds.ui import forms as solarwinds_forms
from dojo.solarwinds.ui.forms import SolarWindsFindingForm, SolarWindsInstanceForm, SolarWindsProductForm
from unittests.dojo_test_case import DojoAPITestCase, DojoTestCase, versioned_fixtures

# PermissionDenied is routed through dojo.views.custom_unauthorized_view
# (handler403), which renders the 403 template with status 400 - the convention
# unittests/test_authorized_users_ui.py documents.
DENIED = 400

VALID_TOKEN = "service-account-token"
ROTATED_TOKEN = "rotated-token"


def instance_post_data(**overrides):
    """A complete, valid POST body for ``SolarWindsInstanceForm``."""
    data = {
        "configuration_name": "acme-servicedesk",
        "subdomain": "acme",
        "region": "US",
        "service_account_email": "svc@example.invalid",
        "default_site_id": "",
        "default_department_id": "",
        "default_category": "",
        "open_state": "New",
        "resolved_states": '["Resolved", "Closed"]',
        "severity_priority_map": '{"Critical": "Critical", "High": "High"}',
        "api_token": VALID_TOKEN,
    }
    data.update(overrides)
    return data


def make_saved_instance(**overrides):
    """Persist a realistic instance with an encrypted token."""
    defaults = {
        "configuration_name": "acme-servicedesk",
        "subdomain": "acme",
        "open_state": "New",
        "resolved_states": ["Resolved", "Closed"],
        "severity_priority_map": {"Critical": "Critical", "High": "High"},
    }
    defaults.update(overrides)
    instance = SolarWinds_Instance(**defaults)
    instance.set_api_token(VALID_TOKEN)
    instance.save()
    return instance


class StubClient:

    """
    Stand-in for ``SolarWindsClient``.

    Raises whatever ``failures[name]`` holds, so a partial outage (token can read
    sites but not groups) can be reproduced exactly.
    """

    def __init__(self, collections=None, incidents=None, failures=None):
        self.collections = collections or {}
        self.incidents = incidents or []
        self.failures = failures or {}
        self.calls = []

    def list_collection(self, name, **kwargs):
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]
        return self.collections.get(name, [])

    def list_incidents_updated_since(self, *args, **kwargs):
        self.calls.append("incidents")
        if "incidents" in self.failures:
            raise self.failures["incidents"]
        return self.incidents


class TestDiscoveryHelpers(DojoTestCase):

    def test_probe_instance_is_usable_before_the_row_exists(self):
        probe = discovery.build_probe_instance("  ACME  ", VALID_TOKEN)
        self.assertIsNone(probe.pk)
        self.assertEqual(probe.base_url, "https://acme.samanage.com")
        # Encrypted, not assigned: the client decrypts what it reads, so a plaintext
        # assignment here would look exactly like a bad credential.
        self.assertNotIn(VALID_TOKEN, probe.api_token)
        self.assertEqual(probe.get_api_token(), VALID_TOKEN)

    def test_options_are_normalised_and_sorted(self):
        options = discovery._as_options([
            {"id": 22, "name": "Ops"},
            {"id": 11, "name": "appsec"},
            {"id": 33, "name": "  "},  # unusable, dropped rather than shown blank
            "not-a-dict",
        ])
        self.assertEqual(options, [{"id": "11", "name": "appsec"}, {"id": "22", "name": "Ops"}])
        # Ids are stringified because every field they feed is a CharField.
        self.assertIsInstance(options[0]["id"], str)

    def test_label_extraction_tolerates_both_wire_shapes(self):
        self.assertEqual(discovery._label_of(" Resolved "), "Resolved")
        self.assertEqual(discovery._label_of({"name": "Closed"}), "Closed")
        # Anything else is dropped, not stringified - an admin copying "{'id': 3}"
        # into open_state would 422 on every push.
        self.assertEqual(discovery._label_of({"id": 3}), "")
        self.assertEqual(discovery._label_of(None), "")


class TestDiscoveryProbe(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.instance = make_saved_instance()

    def stub(self, **kwargs):
        return StubClient(
            collections={
                "sites": [{"id": 1, "name": "HQ"}],
                "departments": [{"id": 2, "name": "Security"}],
                "categories": [{"id": 3, "name": "Security"}],
                "groups": [{"id": 4, "name": "AppSec"}],
            },
            incidents=[
                {"state": "New", "priority": "High"},
                {"state": "New", "priority": "Critical"},
                {"state": "Resolved", "priority": "High"},
                {"state": None, "priority": ""},
            ],
            **kwargs,
        )

    def test_collections_are_exhaustive_lookups(self):
        result = discovery.discover_tenant_configuration(self.instance, client=self.stub())
        self.assertTrue(result.ok)
        self.assertEqual(result.sites, [{"id": "1", "name": "HQ"}])
        self.assertEqual(result.departments, [{"id": "2", "name": "Security"}])
        self.assertEqual(result.categories, [{"id": "3", "name": "Security"}])
        self.assertEqual(result.groups, [{"id": "4", "name": "AppSec"}])

    def test_observed_labels_are_distinct_and_carry_the_not_exhaustive_caveat(self):
        result = discovery.discover_tenant_configuration(self.instance, client=self.stub())
        self.assertEqual(result.observed_states, ["New", "Resolved"])
        self.assertEqual(result.observed_priorities, ["Critical", "High"])
        self.assertEqual(result.observed_sample_size, 4)
        # The caveat is the contract, not decoration: these labels are a hint, and
        # a tenant with only "New" incidents cannot discover "Resolved" at all.
        self.assertIn("NOT a complete list", result.observed_caveat)

    def test_empty_tenant_yields_no_labels_rather_than_an_error(self):
        result = discovery.discover_tenant_configuration(
            self.instance, client=StubClient(incidents=[]),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.observed_states, [])
        self.assertEqual(result.observed_sample_size, 0)

    def test_authentication_failure_is_reported_not_raised(self):
        # This runs from a config form; a bad token must not become a 500.
        failing = {
            name: SolarWindsAuthenticationError("SolarWinds rejected our credentials (401)")
            for name in ("sites", "departments", "categories", "groups", "incidents")
        }
        result = discovery.discover_tenant_configuration(self.instance, client=StubClient(failures=failing))
        self.assertFalse(result.ok)
        self.assertEqual(set(result.errors), {"sites", "departments", "categories", "groups", "incidents"})
        self.assertIn("401", result.errors["incidents"])

    def test_one_failing_lookup_does_not_abort_the_others(self):
        stub = self.stub(failures={"groups": SolarWindsAuthenticationError("403 on groups")})
        result = discovery.discover_tenant_configuration(self.instance, client=stub)
        self.assertFalse(result.ok)
        self.assertEqual(list(result.errors), ["groups"])
        self.assertEqual(result.sites, [{"id": "1", "name": "HQ"}])
        self.assertEqual(result.observed_states, ["New", "Resolved"])

    def test_missing_token_is_a_clean_error_too(self):
        # No token at all takes the SolarWindsConfigurationError path out of
        # client._build_headers rather than an unauthenticated request.
        stub = StubClient(failures=dict.fromkeys(
            ("sites", "departments", "categories", "groups", "incidents"),
            SolarWindsConfigurationError("no usable API token"),
        ))
        result = discovery.discover_tenant_configuration(self.instance, client=stub)
        self.assertFalse(result.ok)
        self.assertIn("no usable API token", result.errors["sites"])


class TestSolarWindsInstanceFormToken(DojoTestCase):

    """R21: the ciphertext must never make a round trip through the form."""

    def test_token_is_required_on_create(self):
        form = SolarWindsInstanceForm(data=instance_post_data(api_token=""))
        self.assertFalse(form.is_valid())
        self.assertIn("api_token", form.errors)

    def test_token_is_stored_encrypted(self):
        form = SolarWindsInstanceForm(data=instance_post_data())
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()

        stored = SolarWinds_Instance.objects.get(pk=instance.pk)
        self.assertTrue(stored.api_token.startswith("AES."))
        self.assertNotIn(VALID_TOKEN, stored.api_token)
        self.assertEqual(stored.get_api_token(), VALID_TOKEN)

    def test_saving_twice_without_retyping_keeps_the_ciphertext_byte_identical(self):
        create = SolarWindsInstanceForm(data=instance_post_data())
        self.assertTrue(create.is_valid(), create.errors)
        instance = create.save()
        first_ciphertext = SolarWinds_Instance.objects.get(pk=instance.pk).api_token

        edit = SolarWindsInstanceForm(
            data=instance_post_data(api_token="", open_state="Assigned"),
            instance=SolarWinds_Instance.objects.get(pk=instance.pk),
        )
        self.assertTrue(edit.is_valid(), edit.errors)
        edit.save()

        reloaded = SolarWinds_Instance.objects.get(pk=instance.pk)
        self.assertEqual(reloaded.api_token, first_ciphertext)
        self.assertEqual(reloaded.get_api_token(), VALID_TOKEN)
        # The rest of the form still saved - "blank token" is not "blank form".
        self.assertEqual(reloaded.open_state, "Assigned")

    def test_reencrypting_the_same_plaintext_produces_different_ciphertext(self):
        # Control for the test above: dojo_crypto_encrypt uses a fresh nonce, so
        # "byte-identical" really does prove set_api_token() was not called again.
        first = SolarWinds_Instance()
        first.set_api_token(VALID_TOKEN)
        second = SolarWinds_Instance()
        second.set_api_token(VALID_TOKEN)
        self.assertNotEqual(first.api_token, second.api_token)

    def test_retyping_a_token_replaces_it(self):
        create = SolarWindsInstanceForm(data=instance_post_data())
        self.assertTrue(create.is_valid(), create.errors)
        instance = create.save()

        edit = SolarWindsInstanceForm(
            data=instance_post_data(api_token=ROTATED_TOKEN),
            instance=SolarWinds_Instance.objects.get(pk=instance.pk),
        )
        self.assertTrue(edit.is_valid(), edit.errors)
        edit.save()

        self.assertEqual(SolarWinds_Instance.objects.get(pk=instance.pk).get_api_token(), ROTATED_TOKEN)

    def test_stored_token_is_never_rendered(self):
        instance = make_saved_instance()
        rendered = str(SolarWindsInstanceForm(instance=instance))
        self.assertNotIn(VALID_TOKEN, rendered)
        self.assertNotIn(instance.api_token, rendered)
        self.assertIn('type="password"', rendered)

    def test_model_validation_still_runs_through_the_form(self):
        form = SolarWindsInstanceForm(data=instance_post_data(severity_priority_map='{"Catastrophic": "Critical"}'))
        self.assertFalse(form.is_valid())
        self.assertIn("severity_priority_map", form.errors)

    def test_hostile_subdomain_is_rejected(self):
        form = SolarWindsInstanceForm(data=instance_post_data(subdomain="evil.example.com"))
        self.assertFalse(form.is_valid())
        self.assertIn("subdomain", form.errors)

    def test_push_cannot_be_enabled_with_an_empty_severity_map(self):
        # R22: an empty map plus push_enabled is a configuration whose every push
        # raises SolarWindsConfigurationError in the client. Rejected at config time.
        form = SolarWindsInstanceForm(data=instance_post_data(severity_priority_map="{}", push_enabled="on"))
        self.assertFalse(form.is_valid())
        self.assertIn("severity_priority_map", form.errors)

    def test_empty_severity_map_is_fine_while_push_is_off(self):
        # Half-configured is a legitimate intermediate state; only enabling push is not.
        form = SolarWindsInstanceForm(data=instance_post_data(severity_priority_map="{}"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_blank_json_fields_become_empty_containers_not_null(self):
        # Django counts `{}`/`[]` as empty values, so these fields are optional at
        # the form layer; a blank one must land as the column's own empty default
        # rather than as None, which the non-nullable column would reject.
        form = SolarWindsInstanceForm(data=instance_post_data(resolved_states="", severity_priority_map=""))
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()

        stored = SolarWinds_Instance.objects.get(pk=instance.pk)
        self.assertEqual(stored.resolved_states, [])
        self.assertEqual(stored.severity_priority_map, {})


@versioned_fixtures
class TestSolarWindsProductForm(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def test_duplicate_mapping_is_a_field_error_not_an_integrity_error(self):
        instance = make_saved_instance()
        product = Product.objects.first()
        SolarWinds_Product.objects.create(solarwinds_instance=instance, product=product)

        form = SolarWindsProductForm(data={
            "solarwinds_instance": instance.pk,
            "product": product.pk,
            "site_id": "",
            "department_id": "",
            "group_assignee_id": "",
        })
        self.assertFalse(form.is_valid())

    def test_editing_an_existing_mapping_does_not_collide_with_itself(self):
        instance = make_saved_instance()
        product = Product.objects.first()
        mapping = SolarWinds_Product.objects.create(solarwinds_instance=instance, product=product)

        form = SolarWindsProductForm(instance=mapping, data={
            "solarwinds_instance": instance.pk,
            "product": product.pk,
            "site_id": "99",
            "department_id": "",
            "group_assignee_id": "",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().site_id, "99")


@versioned_fixtures
class TestSolarWindsUIViews(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    @classmethod
    def setUpTestData(cls):
        cls.superuser = Dojo_User.objects.create(username="sw_super", is_superuser=True)
        cls.nobody = Dojo_User.objects.create(username="sw_nobody")
        # Non-staff, non-superuser, granted exactly the configuration permissions
        # these routes key on - the same shape test_group_ui.py uses.
        cls.config_admin = Dojo_User.objects.create(username="sw_config_admin")
        cls.config_admin.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="dojo",
            codename__in=[
                "view_solarwinds_instance", "add_solarwinds_instance",
                "change_solarwinds_instance", "delete_solarwinds_instance",
                "view_solarwinds_product", "add_solarwinds_product",
                "change_solarwinds_product", "delete_solarwinds_product",
            ],
        ))

    def setUp(self):
        super().setUp()
        self.instance = make_saved_instance()

    def test_routes_are_denied_without_the_configuration_permission(self):
        self.client.force_login(self.nobody)
        for url in (
            reverse("solarwinds_instances"),
            reverse("view_solarwinds_instance", args=(self.instance.id,)),
            reverse("add_solarwinds_instance"),
            reverse("edit_solarwinds_instance", args=(self.instance.id,)),
            reverse("delete_solarwinds_instance", args=(self.instance.id,)),
            reverse("test_solarwinds_instance", args=(self.instance.id,)),
            reverse("add_solarwinds_product", args=(self.instance.id,)),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, DENIED)

    def test_list_and_detail_render_for_a_config_admin(self):
        self.client.force_login(self.config_admin)
        self.assertEqual(self.client.get(reverse("solarwinds_instances")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("view_solarwinds_instance", args=(self.instance.id,))).status_code, 200,
        )

    def test_list_page_does_not_leak_the_token(self):
        self.client.force_login(self.superuser)
        for url in (
            reverse("solarwinds_instances"),
            reverse("view_solarwinds_instance", args=(self.instance.id,)),
            reverse("edit_solarwinds_instance", args=(self.instance.id,)),
        ):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn(VALID_TOKEN, body)
                self.assertNotIn(self.instance.api_token, body)

    def test_add_instance_creates_a_configuration(self):
        self.client.force_login(self.config_admin)
        response = self.client.post(
            reverse("add_solarwinds_instance"),
            instance_post_data(configuration_name="second-tenant", subdomain="second"),
        )
        self.assertEqual(response.status_code, 302)
        created = SolarWinds_Instance.objects.get(configuration_name="second-tenant")
        self.assertEqual(created.get_api_token(), VALID_TOKEN)

    def test_edit_instance_without_a_token_keeps_the_stored_one(self):
        self.client.force_login(self.config_admin)
        before = SolarWinds_Instance.objects.get(pk=self.instance.pk).api_token

        response = self.client.post(
            reverse("edit_solarwinds_instance", args=(self.instance.id,)),
            instance_post_data(api_token="", open_state="Assigned"),
        )
        self.assertEqual(response.status_code, 302)

        after = SolarWinds_Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(after.api_token, before)
        self.assertEqual(after.open_state, "Assigned")

    def test_delete_instance(self):
        self.client.force_login(self.config_admin)
        response = self.client.post(
            reverse("delete_solarwinds_instance", args=(self.instance.id,)),
            {"id": self.instance.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SolarWinds_Instance.objects.filter(pk=self.instance.pk).exists())

    def test_product_mapping_crud(self):
        self.client.force_login(self.config_admin)
        product = Product.objects.first()

        response = self.client.post(
            reverse("add_solarwinds_product", args=(self.instance.id,)),
            {
                "solarwinds_instance": self.instance.pk,
                "product": product.pk,
                "site_id": "42",
                "department_id": "",
                "group_assignee_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        mapping = SolarWinds_Product.objects.get(solarwinds_instance=self.instance, product=product)
        self.assertEqual(mapping.site_id, "42")

        response = self.client.post(
            reverse("edit_solarwinds_product", args=(mapping.id,)),
            {
                "solarwinds_instance": self.instance.pk,
                "product": product.pk,
                "site_id": "43",
                "department_id": "",
                "group_assignee_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        mapping.refresh_from_db()
        self.assertEqual(mapping.site_id, "43")

        response = self.client.post(
            reverse("delete_solarwinds_product", args=(mapping.id,)),
            {"id": mapping.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SolarWinds_Product.objects.filter(pk=mapping.pk).exists())

    def test_test_connection_button_probes_without_saving(self):
        self.client.force_login(self.config_admin)
        probe_result = discovery.DiscoveryResult(
            collections={"sites": [{"id": "1", "name": "HQ"}]},
            observed_states=["New", "Resolved"],
            observed_priorities=["High"],
            observed_sample_size=3,
        )

        with patch.object(discovery, "discover_tenant_configuration", return_value=probe_result) as probe:
            response = self.client.post(
                reverse("add_solarwinds_instance"),
                {**instance_post_data(configuration_name="probe-only", subdomain="probe"), "test_connection": "1"},
            )

        self.assertEqual(response.status_code, 200)
        probe.assert_called_once()
        # A probe must never create the configuration as a side effect.
        self.assertFalse(SolarWinds_Instance.objects.filter(configuration_name="probe-only").exists())

        body = response.content.decode()
        self.assertIn("Resolved", body)
        self.assertIn("NOT a complete list", body)

    def test_test_connection_on_a_saved_instance_reports_errors_without_500ing(self):
        self.client.force_login(self.config_admin)
        failed = discovery.DiscoveryResult(errors={"incidents": "SolarWinds rejected our credentials (401)"})

        with patch.object(discovery, "discover_tenant_configuration", return_value=failed):
            response = self.client.get(reverse("test_solarwinds_instance", args=(self.instance.id,)))

        self.assertEqual(response.status_code, 200)
        self.assertIn("401", response.content.decode())


@versioned_fixtures
class TestSolarWindsTemplateTrees(DojoTestCase):

    """
    Both template trees ship, so both must render.

    ``dojo/templates_classic/`` (Bootstrap 3) is the default and is what every other
    view test here exercises; ``dojo/templates/`` (Tailwind) is selected per user by
    ``UIPreferenceLoader`` via ``usercontactinfo.ui_use_tailwind``. Without this a
    template error in the opt-in tree would sit unnoticed until a user flipped the
    preference.
    """

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.instance = make_saved_instance()
        self.user = Dojo_User.objects.create(username="sw_tailwind", is_superuser=True)
        UserContactInfo.objects.create(user=self.user, ui_use_tailwind=True)
        self.client.force_login(self.user)

    def test_every_page_renders_in_the_tailwind_tree(self):
        product = Product.objects.first()
        mapping = SolarWinds_Product.objects.create(solarwinds_instance=self.instance, product=product)

        for url in (
            reverse("solarwinds_instances"),
            reverse("view_solarwinds_instance", args=(self.instance.id,)),
            reverse("add_solarwinds_instance"),
            reverse("edit_solarwinds_instance", args=(self.instance.id,)),
            reverse("delete_solarwinds_instance", args=(self.instance.id,)),
            reverse("add_solarwinds_product", args=(self.instance.id,)),
            reverse("edit_solarwinds_product", args=(mapping.id,)),
            reverse("delete_solarwinds_product", args=(mapping.id,)),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                # Proves the Tailwind tree was actually selected rather than the
                # loader silently falling back to classic - a 200 alone would not.
                self.assertIn("hover:bg-white/10", response.content.decode())

    def test_discovery_panel_renders_in_the_tailwind_tree(self):
        result = discovery.DiscoveryResult(
            collections={"sites": [{"id": "1", "name": "HQ"}]},
            observed_states=["New"],
            observed_priorities=["High"],
            observed_sample_size=1,
            errors={"groups": "403 on groups"},
        )
        with patch.object(discovery, "discover_tenant_configuration", return_value=result):
            response = self.client.get(reverse("test_solarwinds_instance", args=(self.instance.id,)))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("NOT a complete list", body)
        self.assertIn("403 on groups", body)


@versioned_fixtures
class TestAutoPushSeveritiesConfigSurface(DojoTestCase):

    """
    ``auto_push_severities`` is reachable and validated from every configuration layer.

    The field is the volume control for the config-driven push, so an operator has to
    be able to see and change it without the Django admin; and the subset rule has to
    hold wherever it is set, not only in the one form that happens to be tested.
    """

    fixtures = ["dojo_testdata.json"]

    def test_the_form_exposes_the_field(self):
        form = SolarWindsInstanceForm()
        self.assertIn("auto_push_severities", form.fields)

    def test_the_form_accepts_a_subset_of_the_severity_map(self):
        form = SolarWindsInstanceForm(data=instance_post_data(
            severity_priority_map='{"Critical": "Critical", "High": "High", "Medium": "Medium"}',
            auto_push_severities='["Critical", "High"]',
        ))
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.auto_push_severities, ["Critical", "High"])

    def test_the_form_rejects_a_severity_with_no_priority_label(self):
        form = SolarWindsInstanceForm(data=instance_post_data(
            severity_priority_map='{"Critical": "Critical"}',
            auto_push_severities='["Critical", "Medium"]',
        ))
        self.assertFalse(form.is_valid())
        self.assertIn("auto_push_severities", form.errors)

    def test_omitting_the_field_stores_the_empty_default(self):
        # An admin who never touches the field must end up with "nothing automatic",
        # not with a JSONField set to None (which the column cannot hold).
        form = SolarWindsInstanceForm(data=instance_post_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().auto_push_severities, [])

    def test_the_serializer_round_trips_the_field(self):
        serializer = SolarWindsInstanceSerializer(data={
            "configuration_name": "api-tenant",
            "subdomain": "acme",
            "api_token": VALID_TOKEN,
            "open_state": "New",
            "resolved_states": ["Resolved"],
            "severity_priority_map": {"Critical": "Critical", "High": "High"},
            "auto_push_severities": ["Critical"],
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()
        self.assertEqual(instance.auto_push_severities, ["Critical"])
        self.assertEqual(serializer.data["auto_push_severities"], ["Critical"])

    def test_the_serializer_enforces_the_subset_rule(self):
        serializer = SolarWindsInstanceSerializer(data={
            "configuration_name": "api-tenant",
            "subdomain": "acme",
            "api_token": VALID_TOKEN,
            "open_state": "New",
            "resolved_states": ["Resolved"],
            "severity_priority_map": {"Critical": "Critical"},
            "auto_push_severities": ["Critical", "Low"],
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("auto_push_severities", serializer.errors)

    def test_a_patch_that_narrows_the_map_cannot_strand_an_auto_push_severity(self):
        # The mirror image of R22's push_enabled case: the stored auto-push list has to
        # be validated against the *incoming* map, or a PATCH touching only the map can
        # leave a severity firing automatically with nothing to file it under.
        instance = make_saved_instance(
            severity_priority_map={"Critical": "Critical", "High": "High"},
            auto_push_severities=["Critical", "High"],
        )
        serializer = SolarWindsInstanceSerializer(
            instance, data={"severity_priority_map": {"Critical": "Critical"}}, partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("auto_push_severities", serializer.errors)

    def test_the_detail_page_shows_the_configured_severities(self):
        instance = make_saved_instance(auto_push_severities=["Critical", "High"])
        user = Dojo_User.objects.create(username="sw_auto_admin", is_superuser=True)
        self.client.force_login(user)

        body = self.client.get(
            reverse("view_solarwinds_instance", args=(instance.id,)),
        ).content.decode()

        self.assertIn("Auto-push severities", body)
        self.assertIn("Critical", body)


@versioned_fixtures
class TestSolarWindsAPI(DojoAPITestCase):

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        token = Token.objects.get(user__username="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        self.url = reverse("solarwinds_instance-list")

    def payload(self, **overrides):
        data = {
            "configuration_name": "api-tenant",
            "subdomain": "apitenant",
            "region": "US",
            "api_token": VALID_TOKEN,
            "open_state": "New",
            "resolved_states": ["Resolved", "Closed"],
            "severity_priority_map": {"Critical": "Critical"},
        }
        data.update(overrides)
        return data

    def test_create_stores_the_token_encrypted(self):
        response = self.client.post(self.url, self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        instance = SolarWinds_Instance.objects.get(configuration_name="api-tenant")
        self.assertTrue(instance.api_token.startswith("AES."))
        self.assertNotIn(VALID_TOKEN, instance.api_token)
        self.assertEqual(instance.get_api_token(), VALID_TOKEN)

    def test_responses_never_contain_the_token_in_any_form(self):
        instance = make_saved_instance()
        created = self.client.post(self.url, self.payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)

        detail = f"{self.url}{instance.id}/"
        for response in (created, self.client.get(self.url), self.client.get(detail)):
            with self.subTest(url=response.request["PATH_INFO"]):
                body = response.content.decode()
                # Neither the plaintext...
                self.assertNotIn(VALID_TOKEN, body)
                # ...nor the ciphertext, which is what R21's double-encryption bug
                # would need in order to happen at all.
                self.assertNotIn(instance.api_token, body)
                self.assertNotIn("AES.", body)
                self.assertNotIn("api_token", body)

    def test_patch_without_a_token_keeps_the_stored_ciphertext(self):
        instance = make_saved_instance()
        before = instance.api_token

        response = self.client.patch(f"{self.url}{instance.id}/", {"open_state": "Assigned"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        instance.refresh_from_db()
        self.assertEqual(instance.api_token, before)
        self.assertEqual(instance.open_state, "Assigned")

    def test_patch_with_a_token_rotates_it(self):
        instance = make_saved_instance()
        response = self.client.patch(f"{self.url}{instance.id}/", {"api_token": "rotated"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        instance.refresh_from_db()
        self.assertEqual(instance.get_api_token(), "rotated")

    def test_model_validation_is_enforced_by_the_api_too(self):
        response = self.client.post(
            self.url, self.payload(severity_priority_map={"Catastrophic": "Critical"}), format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("severity_priority_map", response.data)

    def test_patch_cannot_enable_push_on_an_empty_severity_map(self):
        # R22 through the API: the check is conditional on push_enabled, so a PATCH
        # that flips only that flag has to be validated against the stored map.
        instance = make_saved_instance(severity_priority_map={})
        response = self.client.patch(f"{self.url}{instance.id}/", {"push_enabled": True}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("severity_priority_map", response.data)

    def test_hostile_subdomain_is_rejected_by_the_api(self):
        response = self.client.post(self.url, self.payload(subdomain="evil.example.com"), format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("subdomain", response.data)

    def test_token_field_is_declared_write_only(self):
        # Belt and braces next to the response-body assertions: a future refactor
        # that rebuilt the serializer would have to defeat both.
        self.assertTrue(SolarWindsInstanceSerializer().fields["api_token"].write_only)

    def test_duplicate_product_mapping_is_a_400(self):
        instance = make_saved_instance()
        product = Product.objects.first()
        url = reverse("solarwinds_product_configuration-list")
        body = {"solarwinds_instance": instance.id, "product": product.id}

        self.assertEqual(self.client.post(url, body, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, body, format="json").status_code, 400)

    def test_unprivileged_user_cannot_read_configurations(self):
        user = Dojo_User.objects.create(username="sw_api_nobody")
        token, _ = Token.objects.get_or_create(user=user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        self.assertEqual(client.get(self.url).status_code, 403)


class TestSolarWindsFindingFormWiring(DojoTestCase):

    """
    The manual per-finding push: the checkbox, its gating, and both template trees.

    Closes the UI half of R26. Until this existed, ``push_to_solarwinds`` was threaded
    all the way through ``Finding.save()`` with nothing anywhere setting it to True, so
    an operator's only control was all-auto-severities-or-nothing per product.

    Self-contained fixtures rather than ``dojo_testdata.json``: these tests POST a full
    finding form, and the shape of that POST is easier to keep honest against objects
    built here (the same approach ``test_edit_finding_endpoints.py`` takes).
    """

    def setUp(self):
        super().setUp()
        self.user = Dojo_User.objects.create_user(
            username="sw_finding_user", password="pass",  # noqa: S106
            is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.user)

        product_type = Product_Type.objects.create(name="SW PT")
        self.product = Product.objects.create(
            name="SW P", prod_type=product_type, description="Test product",
        )
        engagement = Engagement.objects.create(
            name="SW E", product=self.product, target_start=now(), target_end=now(),
        )
        self.test_obj = Test.objects.create(
            engagement=engagement, test_type=Test_Type.objects.create(name="SW TT"),
            target_start=now(), target_end=now(),
        )
        self.finding = Finding.objects.create(
            title="SolarWinds manual push finding",
            severity="Medium",
            test=self.test_obj,
            reporter=self.user,
            active=True,
        )

        self.instance = make_saved_instance(
            severity_priority_map={
                "Critical": "Critical", "High": "High", "Medium": "Medium", "Low": "Low",
            },
            auto_push_severities=["Critical", "High"],
            push_enabled=True,
        )
        self.mapping = SolarWinds_Product.objects.create(
            solarwinds_instance=self.instance, product=self.product, push_all_findings=True,
        )

    # -- helpers ----------------------------------------------------------

    def edit_url(self):
        return reverse("edit_finding", args=[self.finding.id])

    def add_url(self):
        return reverse("add_findings", args=[self.test_obj.id])

    def finding_post_data(self, **overrides):
        data = {
            "title": self.finding.title,
            "date": "2024-01-01",
            "severity": self.finding.severity,
            "description": "Test description",
            "active": "on",
            "verified": "",
            "false_p": "",
            "duplicate": "",
            "out_of_scope": "",
            "endpoints": [],
            "endpoints_to_add": "",
            "vulnerability_ids": "",
            "references": "",
            "mitigation": "",
            "impact": "",
            "steps_to_reproduce": "",
            "severity_justification": "",
        }
        data.update(overrides)
        return data

    # -- the form object itself -------------------------------------------

    def test_the_form_is_offered_for_a_mapped_severity(self):
        request = RequestFactory().get(self.edit_url())
        form = solarwinds_forms.build_finding_form(request, finding=self.finding)

        self.assertIsNotNone(form)
        self.assertIn("push_to_solarwinds", form.fields)
        self.assertFalse(form.fields["push_to_solarwinds"].disabled)

    def test_the_form_is_not_offered_for_an_unmapped_severity(self):
        # Info has no priority label, so the box could never do anything. Rendering a
        # dead checkbox is worse than rendering none.
        self.finding.severity = "Info"
        self.finding.save(dedupe_option=False, issue_updater_option=False, product_grading_option=False)
        request = RequestFactory().get(self.edit_url())

        self.assertIsNone(solarwinds_forms.build_finding_form(request, finding=self.finding))

    def test_the_form_is_not_offered_for_an_unmapped_product(self):
        self.mapping.delete()
        request = RequestFactory().get(self.edit_url())

        self.assertIsNone(solarwinds_forms.build_finding_form(request, finding=self.finding))

    def test_the_form_is_not_offered_when_push_is_disabled_on_the_instance(self):
        self.instance.push_enabled = False
        self.instance.save()
        request = RequestFactory().get(self.edit_url())

        self.assertIsNone(solarwinds_forms.build_finding_form(request, finding=self.finding))

    def test_an_auto_push_severity_shows_the_box_checked_and_disabled(self):
        # Same treatment JIRA gives its push-all case: the operator is told the push is
        # happening rather than offered a choice that is not theirs.
        self.finding.severity = "Critical"
        request = RequestFactory().get(self.edit_url())
        form = solarwinds_forms.build_finding_form(request, finding=self.finding)

        self.assertTrue(form.fields["push_to_solarwinds"].disabled)
        self.assertEqual(form.fields["push_to_solarwinds"].widget.attrs.get("checked"), "checked")
        self.assertIn("automatically", str(form.fields["push_to_solarwinds"].help_text))

    def test_an_already_ticketed_finding_shows_the_box_disabled_and_names_the_incident(self):
        # There is no outbound update path, so a second push would be swallowed by the
        # has_ticket guard. Say so instead of accepting a click that does nothing.
        SolarWinds_Ticket.objects.create(
            finding=self.finding, solarwinds_instance=self.instance,
            incident_id="9001", incident_number="INC-9001",
        )
        request = RequestFactory().get(self.edit_url())
        form = solarwinds_forms.build_finding_form(request, finding=self.finding)

        self.assertTrue(form.fields["push_to_solarwinds"].disabled)
        self.assertIn("INC-9001", str(form.fields["push_to_solarwinds"].help_text))

    def test_the_form_rejects_a_request_for_an_unmapped_severity_being_typed(self):
        # Validated against the severity in *this* submission, not the stored one: on
        # add-finding there is no stored severity at all, and on edit it may be changing
        # in the same POST.
        finding_form = FindingForm(
            data=self.finding_post_data(severity="Info"), instance=self.finding, req_resp=None,
        )
        finding_form.is_valid()
        form = SolarWindsFindingForm(
            data={"solarwindsform-push_to_solarwinds": "on"},
            solarwinds_instance=self.instance,
            finding_form=finding_form,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("push_to_solarwinds", form.errors)

    def test_an_unchecked_box_never_complains_about_severity(self):
        finding_form = FindingForm(
            data=self.finding_post_data(severity="Info"), instance=self.finding, req_resp=None,
        )
        finding_form.is_valid()
        form = SolarWindsFindingForm(
            data={}, solarwinds_instance=self.instance, finding_form=finding_form,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["push_to_solarwinds"])

    # -- the views --------------------------------------------------------

    def test_the_edit_finding_page_offers_the_checkbox(self):
        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["sform"])
        self.assertIn("solarwindsform-push_to_solarwinds", response.content.decode())

    def test_the_add_finding_page_offers_the_checkbox(self):
        response = self.client.get(self.add_url())

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["sform"])
        self.assertIn("solarwindsform-push_to_solarwinds", response.content.decode())

    def test_neither_page_offers_it_for_an_unconfigured_product(self):
        self.mapping.delete()

        for url in (self.edit_url(), self.add_url()):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["sform"])
                self.assertNotIn("solarwindsform-push_to_solarwinds", response.content.decode())

    def test_both_template_trees_render_the_section(self):
        # The Tailwind tree is opt-in per user, so a template error there would sit
        # unnoticed until somebody flipped the preference.
        UserContactInfo.objects.update_or_create(
            user=self.user, defaults={"ui_use_tailwind": True},
        )

        for url in (self.edit_url(), self.add_url()):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                # Proves the Tailwind tree was actually selected, not a silent fallback.
                self.assertIn("hover:bg-white/10", body)
                self.assertIn("SolarWinds Service Desk", body)
                self.assertIn("solarwindsform-push_to_solarwinds", body)

    def test_editing_a_finding_with_the_box_ticked_requests_a_push(self):
        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.post(self.edit_url(), self.finding_post_data(**{
                "solarwindsform-push_to_solarwinds": "on",
            }))

        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(push.called)
        self.assertTrue(push.call_args.kwargs["push_requested"])

    def test_editing_a_finding_without_ticking_the_box_requests_nothing(self):
        # Medium is not an auto-push severity here, so an untouched form must leave the
        # finding alone - this is the case the whole feature exists to protect.
        with patch("dojo.solarwinds.helper.push_finding") as push:
            self.client.post(self.edit_url(), self.finding_post_data())

        self.assertTrue(all(not call.kwargs.get("push_requested") for call in push.call_args_list))

    def test_adding_a_finding_with_the_box_ticked_requests_a_push(self):
        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.post(self.add_url(), self.finding_post_data(**{
                "title": "Brand new manual push finding",
                "solarwindsform-push_to_solarwinds": "on",
            }))

        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(any(call.kwargs.get("push_requested") for call in push.call_args_list))

    def test_a_disabled_checkbox_does_not_turn_into_a_request(self):
        # An auto-push severity renders the box disabled, so Django ignores whatever is
        # submitted for it. The push still happens - from the save hook, on the
        # config-driven path - but not as an explicit request, which is what keeps the
        # two triggers from both firing for one finding.
        self.finding.severity = "Critical"
        self.finding.save(dedupe_option=False, issue_updater_option=False, product_grading_option=False)

        with patch("dojo.solarwinds.helper.push_finding") as push:
            self.client.post(self.edit_url(), self.finding_post_data(**{
                "severity": "Critical",
                "solarwindsform-push_to_solarwinds": "on",
            }))

        self.assertTrue(all(not call.kwargs.get("push_requested") for call in push.call_args_list))


@versioned_fixtures
class TestFindingAPIPushToSolarWinds(DojoAPITestCase):

    """
    ``push_to_solarwinds`` on the finding REST endpoints.

    The API cannot thread the flag into ``Finding.save()`` the way the UI views do -
    ``ModelSerializer.update()`` owns that call and takes no kwargs - so it dispatches
    explicitly afterwards through ``helper.push_finding_explicitly``. The guard in that
    function is what these tests are really pinning: for a severity that auto-pushes,
    the save already dispatched, and a second dispatch would race it into creating two
    incidents for one finding (R25).
    """

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        token = Token.objects.get(user__username="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)

        self.finding = Finding.objects.filter(active=True, is_mitigated=False).first()
        self.finding.severity = "Medium"
        self.finding.save(
            dedupe_option=False, issue_updater_option=False, product_grading_option=False,
        )
        self.instance = make_saved_instance(
            severity_priority_map={"Critical": "Critical", "Medium": "Medium"},
            auto_push_severities=["Critical"],
            push_enabled=True,
        )
        SolarWinds_Product.objects.create(
            solarwinds_instance=self.instance,
            product=self.finding.test.engagement.product,
            push_all_findings=True,
        )
        self.detail_url = reverse("finding-detail", args=(self.finding.id,))

    def test_the_write_only_field_is_accepted_on_patch(self):
        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.patch(
                self.detail_url, {"push_to_solarwinds": True}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(push.called)
        self.assertTrue(push.call_args.kwargs["push_requested"])

    def test_the_field_is_never_echoed_back(self):
        # write_only: it is an instruction, not finding state, and a client reading it
        # back would reasonably assume the finding is configured to push.
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("push_to_solarwinds", response.data)

    def test_omitting_the_field_requests_nothing(self):
        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.patch(self.detail_url, {"title": "renamed"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(all(not call.kwargs.get("push_requested") for call in push.call_args_list))

    def test_an_auto_push_severity_is_not_dispatched_twice(self):
        self.finding.severity = "Critical"
        self.finding.save(
            dedupe_option=False, issue_updater_option=False, product_grading_option=False,
        )

        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.patch(
                self.detail_url,
                {"severity": "Critical", "push_to_solarwinds": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        # The save hook's own dispatch is allowed; the explicit one on top of it is not.
        self.assertTrue(all(not call.kwargs.get("push_requested") for call in push.call_args_list))

    def test_creating_a_finding_can_request_a_push(self):
        with patch("dojo.solarwinds.helper.push_finding") as push:
            response = self.client.post(reverse("finding-list"), {
                "title": "API manual push finding",
                "date": "2024-01-01",
                "severity": "Medium",
                "description": "created through the API",
                "test": self.finding.test.id,
                "active": True,
                "verified": False,
                "numerical_severity": "S2",
                "found_by": [],
                "push_to_solarwinds": True,
            }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(any(call.kwargs.get("push_requested") for call in push.call_args_list))
