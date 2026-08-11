"""
Coverage matrix for the object-scoping auth-filter queries.

For every product-scoped filter, a non-staff user authorized on one product
(via authorized_users) must see only objects under that product, a user with
no access must see nothing, and a superuser must see everything. This is the
breadth guard that the earlier (superuser-only) coverage lacked — it would
have caught an allow-all `_for_queryset` regression on any of these filters.

Every assertion runs under both ``DD_FEATURE_RBAC=off`` and ``=on``: a legacy
``authorized_users`` grant is worth the same under either resolver, so the
scoping answers must be identical. ``shadow`` deliberately leaves queryset
filtering on the legacy path (the divergence log lives at the object level, and
diffing full result sets per list view buys nothing), so it is covered once by
an explicit equals-``off`` test rather than a third full pass.

``TestObjectAndQuerysetAgree`` closes the loop the flag opens: an object-level
answer and a list-level answer must never disagree, which is the classic RBAC
bug class (visible-but-403, or invisible-but-fetchable).
"""

from unittest.mock import patch

from django.test import override_settings

from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Product_Member, Role
from dojo.authorization.roles_permissions import Roles
from dojo.endpoint.queries import get_authorized_endpoint_status, get_authorized_endpoints
from dojo.engagement.queries import get_authorized_engagements
from dojo.finding.queries import (
    get_authorized_findings,
    get_authorized_findings_for_queryset,
    get_authorized_vulnerability_ids,
    get_authorized_vulnerability_ids_for_queryset,
)
from dojo.finding_group.queries import get_authorized_finding_groups
from dojo.models import Dojo_User, Endpoint, Finding, Test, Vulnerability_Id
from dojo.product.queries import (
    get_authorized_app_analysis,
    get_authorized_engagement_presets,
    get_authorized_languages,
    get_authorized_product_api_scan_configurations,
    get_authorized_products,
)
from dojo.product_type.queries import get_authorized_product_types
from dojo.risk_acceptance.queries import get_authorized_risk_acceptances
from dojo.test.queries import get_authorized_test_imports, get_authorized_tests

from .dojo_test_case import DojoTestCase, skip_unless_v2, versioned_fixtures

_GCU = "dojo.authorization.query_registrations.get_current_user"

# States under which the full coverage matrix is exercised. "shadow" is covered
# separately (see TestQuerysetFilteringUnderShadow) because it is defined to
# behave exactly as "off" for queryset filtering.
_FLAG_STATES = ("off", "on")


@versioned_fixtures
class TestAuthorizationQuerysetCoverage(DojoTestCase):

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.product = Test.objects.get(id=3).engagement.product
        self.scoped_user = Dojo_User.objects.create(username="cov_scoped", is_active=True)
        self.product.authorized_users.add(self.scoped_user)
        self.no_access_user = Dojo_User.objects.create(username="cov_noaccess", is_active=True)
        self.superuser = Dojo_User.objects.create(username="cov_super", is_active=True, is_superuser=True)
        # The fixture must have data outside the authorized product, otherwise
        # the "no leak" assertions would be vacuous.
        self.assertTrue(Finding.objects.exclude(test__engagement__product=self.product).exists())

    # (label, callable -> queryset, "<product id field on the result model>")
    @property
    def _cases(self):
        return [
            ("engagements", lambda: get_authorized_engagements("view"), "product__id"),
            ("tests", lambda: get_authorized_tests("view"), "engagement__product__id"),
            ("test_imports", lambda: get_authorized_test_imports("view"), "test__engagement__product__id"),
            ("risk_acceptances", lambda: get_authorized_risk_acceptances("view"), "engagement__product__id"),
            ("finding_groups", lambda: get_authorized_finding_groups("view"), "test__engagement__product__id"),
            ("findings", lambda: get_authorized_findings("view"), "test__engagement__product__id"),
            ("findings_for_queryset",
             lambda: get_authorized_findings_for_queryset("view", Finding.objects.all()),
             "test__engagement__product__id"),
            ("vulnerability_ids", lambda: get_authorized_vulnerability_ids("view"),
             "finding__test__engagement__product__id"),
            ("vulnerability_ids_for_queryset",
             lambda: get_authorized_vulnerability_ids_for_queryset("view", Vulnerability_Id.objects.all()),
             "finding__test__engagement__product__id"),
            ("app_analysis", lambda: get_authorized_app_analysis("view"), "product__id"),
            ("languages", lambda: get_authorized_languages("view"), "product__id"),
            ("engagement_presets", lambda: get_authorized_engagement_presets("view"), "product__id"),
            ("product_api_scan_configurations",
             lambda: get_authorized_product_api_scan_configurations("view"), "product__id"),
            ("products", lambda: get_authorized_products("view"), "id"),
        ]

    def test_scoped_user_sees_only_authorized_product(self):
        for state in _FLAG_STATES:
            with override_settings(FEATURE_RBAC=state), patch(_GCU, return_value=self.scoped_user):
                for label, call, id_field in self._cases:
                    with self.subTest(filter=label, flag=state):
                        leaked = set(call().values_list(id_field, flat=True)) - {self.product.id}
                        self.assertEqual(
                            leaked, set(), msg=f"{label} returned objects outside the authorized product",
                        )

    def test_no_access_user_sees_nothing(self):
        for state in _FLAG_STATES:
            with override_settings(FEATURE_RBAC=state), patch(_GCU, return_value=self.no_access_user):
                for label, call, _ in self._cases:
                    with self.subTest(filter=label, flag=state):
                        self.assertEqual(
                            call().count(), 0, msg=f"{label} leaked objects to an unauthorized user",
                        )

    def test_superuser_sees_everything(self):
        for state in _FLAG_STATES:
            with override_settings(FEATURE_RBAC=state), patch(_GCU, return_value=self.superuser):
                for label, call, _ in self._cases:
                    with self.subTest(filter=label, flag=state):
                        qs = call()
                        self.assertEqual(
                            qs.count(), qs.model.objects.count(),
                            msg=f"{label} did not return all objects for a superuser",
                        )

    @skip_unless_v2
    def test_endpoints_scoping(self):
        # Endpoint is deprecated under V3 (Locations); exercise under V2 only.
        ep_in = Endpoint.objects.create(product=self.product, host="cov-in.example.com")
        other_product = Test.objects.exclude(engagement__product=self.product).first().engagement.product
        ep_out = Endpoint.objects.create(product=other_product, host="cov-out.example.com")

        for state in _FLAG_STATES:
            with override_settings(FEATURE_RBAC=state):
                with patch(_GCU, return_value=self.scoped_user):
                    for label, call in [
                        ("endpoints", lambda: get_authorized_endpoints("view")),
                        ("endpoints_for_queryset", lambda: get_authorized_endpoints("view")),
                    ]:
                        with self.subTest(filter=label, flag=state):
                            eps = call()
                            self.assertIn(ep_in, eps)
                            self.assertNotIn(ep_out, eps)

                with patch(_GCU, return_value=self.no_access_user), self.subTest(flag=state):
                    self.assertEqual(get_authorized_endpoints("view").count(), 0)
                    self.assertEqual(get_authorized_endpoint_status("view").count(), 0)

                with patch(_GCU, return_value=self.superuser), self.subTest(flag=state):
                    self.assertEqual(get_authorized_endpoints("view").count(), Endpoint.objects.count())

    def test_product_types_scoping(self):
        # get_authorized_product_types keys off product_type.authorized_users, so
        # a product-only member sees none; a product-type member sees their type.
        pt_user = Dojo_User.objects.create(username="cov_pt", is_active=True)
        self.product.prod_type.authorized_users.add(pt_user)

        for state in _FLAG_STATES:
            with override_settings(FEATURE_RBAC=state), self.subTest(flag=state):
                with patch(_GCU, return_value=self.no_access_user):
                    self.assertEqual(get_authorized_product_types("view").count(), 0)
                with patch(_GCU, return_value=self.superuser):
                    self.assertEqual(
                        get_authorized_product_types("view").count(),
                        self.product.prod_type.__class__.objects.count(),
                    )
                with patch(_GCU, return_value=pt_user):
                    pts = get_authorized_product_types("view")
                    self.assertEqual(set(pts.values_list("id", flat=True)), {self.product.prod_type_id})


@versioned_fixtures
class TestQuerysetFilteringUnderShadow(DojoTestCase):

    """
    ``shadow`` is defined to leave queryset filtering on the legacy path, so that
    object-level and list-level answers keep agreeing while divergence is only
    being *observed*. Pinned explicitly rather than folded into the matrix above,
    because it is a deliberate scope decision and not an oversight.
    """

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.product = Test.objects.get(id=3).engagement.product
        self.role_user = Dojo_User.objects.create(username="shadow_role_user", is_active=True)
        Product_Member.objects.create(
            product=self.product, user=self.role_user, role=Role.objects.get(pk=Roles.Reader.value),
        )

    def _visible_product_ids(self, state):
        with override_settings(FEATURE_RBAC=state), patch(_GCU, return_value=self.role_user):
            return set(get_authorized_products("view").values_list("id", flat=True))

    def test_shadow_returns_the_same_products_as_off(self):
        self.assertEqual(self._visible_product_ids("shadow"), self._visible_product_ids("off"))

    def test_shadow_does_not_return_what_on_would(self):
        # Guards the test above from being vacuous.
        self.assertNotEqual(self._visible_product_ids("shadow"), self._visible_product_ids("on"))
        self.assertEqual(self._visible_product_ids("on"), {self.product.id})


@versioned_fixtures
class TestObjectAndQuerysetAgree(DojoTestCase):

    """
    The object-level answer and the list-level answer must agree in every flag
    state — a user who can ``GET /product/5`` but does not see it in the list (or
    the reverse) is the classic RBAC bug class (INTEGRATIONS_ROADMAP.md R14).
    """

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.product = Test.objects.get(id=3).engagement.product
        self.reader = Dojo_User.objects.create(username="agree_reader", is_active=True)
        Product_Member.objects.create(
            product=self.product, user=self.reader, role=Role.objects.get(pk=Roles.Reader.value),
        )
        self.legacy = Dojo_User.objects.create(username="agree_legacy", is_active=True)
        self.product.authorized_users.add(self.legacy)

    def _assert_agrees(self, user, action, state):
        with override_settings(FEATURE_RBAC=state):
            with patch(_GCU, return_value=user):
                in_list = self.product.id in set(
                    get_authorized_products(action).values_list("id", flat=True),
                )
            on_object = user_has_permission(user, self.product, action)
        self.assertEqual(
            on_object, in_list,
            msg=(
                f"object-level ({on_object}) and queryset-level ({in_list}) disagree for "
                f"user={user.username} action={action} flag={state}"
            ),
        )

    def test_role_member_agrees_for_every_action_and_state(self):
        for state in ("off", "shadow", "on"):
            for action in ("view", "add", "edit", "delete", "import"):
                with self.subTest(flag=state, action=action):
                    self._assert_agrees(self.reader, action, state)

    def test_legacy_member_agrees_for_every_action_under_on(self):
        for action in ("view", "add", "edit", "delete", "import"):
            with self.subTest(action=action):
                self._assert_agrees(self.legacy, action, "on")

    def test_legacy_member_agrees_for_the_actions_legacy_models(self):
        # Legacy's queryset filter is action-blind by construction, so agreement
        # can only be asserted for the actions a legacy grant actually confers.
        # The destructive-action gap is pinned separately below.
        for state in ("off", "shadow"):
            for action in ("view", "add", "edit", "import"):
                with self.subTest(flag=state, action=action):
                    self._assert_agrees(self.legacy, action, state)

    def test_legacy_destructive_action_gap_is_preserved_under_off_and_closed_under_on(self):
        """
        A pre-existing legacy quirk, deliberately reproduced rather than fixed.

        The legacy engine denies ``delete`` to a non-staff ``authorized_users``
        member at the object level (the staff-only short-circuit) while its
        queryset filter — which is action-blind — still lists the product. The
        result is a product that appears in a list but 403s on the delete action:
        over-visible, never over-permitted, and exactly what the code did before
        this PR. ``off`` must reproduce it byte for byte; fixing it there would
        break the "off is untouched legacy" contract.

        Under ``on`` the two sides derive from the same action-aware grant map, so
        the gap closes on its own.
        """
        for state in ("off", "shadow"):
            with self.subTest(flag=state), override_settings(FEATURE_RBAC=state):
                self.assertFalse(user_has_permission(self.legacy, self.product, "delete"))
                with patch(_GCU, return_value=self.legacy):
                    self.assertIn(
                        self.product.id,
                        set(get_authorized_products("delete").values_list("id", flat=True)),
                    )

        with override_settings(FEATURE_RBAC="on"):
            self.assertFalse(user_has_permission(self.legacy, self.product, "delete"))
            with patch(_GCU, return_value=self.legacy):
                self.assertNotIn(
                    self.product.id,
                    set(get_authorized_products("delete").values_list("id", flat=True)),
                )

    def test_reader_is_visible_for_view_and_invisible_for_edit_under_on(self):
        # The substantive assertion behind the agreement checks: under "on" the
        # list is genuinely action-scoped, not just consistent with itself.
        with override_settings(FEATURE_RBAC="on"), patch(_GCU, return_value=self.reader):
            self.assertIn(
                self.product.id, set(get_authorized_products("view").values_list("id", flat=True)),
            )
            self.assertNotIn(
                self.product.id, set(get_authorized_products("edit").values_list("id", flat=True)),
            )
