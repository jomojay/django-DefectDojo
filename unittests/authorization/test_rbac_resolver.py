"""
Coexistence matrix for the role-aware resolver behind ``DD_FEATURE_RBAC``
(INTEGRATIONS_ROADMAP.md §7.4/§7.6).

The properties this file exists to pin, in rough order of how much damage their
absence would do:

  1. **No regression on flag flip.** A plain ``authorized_users`` member holds
     exactly ``{view, add, edit, import}`` under ``on`` — the same set the legacy
     engine grants them under ``off``, no more and no less. This is what the
     ``LEGACY_AUTHORIZED_USERS_ACTIONS`` constant exists to guarantee, and it is
     the reason the flip is safe for every grant that exists today.
  2. **``shadow`` never changes an answer.** For every permutation of grants and
     every action, ``shadow`` returns exactly what ``off`` returns, regardless of
     what ``on`` would have said.
  3. **Roles actually mean something under ``on``.** Reader can only view;
     Writer cannot delete; Maintainer can manage but not own; Owner can do
     everything. Grants union across direct membership, groups, product-type
     inheritance and legacy ``authorized_users``.
  4. **Global_Role is a real global grant.** A global Owner can act on a product
     they have no direct, group or legacy membership on at all.
  5. **Bounded queries.** The per-request action maps are built from a fixed
     number of queries no matter how many objects are checked or how many
     distinct actions are asked about.
"""
from contextlib import contextmanager
from unittest.mock import patch

from crum import set_current_request
from django.db import connection
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext

from dojo.authorization.authorization import (
    user_has_global_permission,
    user_has_permission,
)
from dojo.authorization.models import (
    Dojo_Group,
    Dojo_Group_Member,
    Global_Role,
    Product_Group,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
    Role,
)
from dojo.authorization.query_registrations import (
    rbac_product_action_map,
    rbac_product_type_action_map,
)
from dojo.authorization.roles_permissions import (
    LEGACY_AUTHORIZED_USERS_ACTIONS,
    Permissions,
    Roles,
    feature_rbac_state,
)
from dojo.engagement.queries import get_authorized_engagements
from dojo.models import Dojo_User, Product, Product_Type
from dojo.product.queries import get_authorized_products
from dojo.product_type.queries import get_authorized_product_types
from dojo.request_cache.middleware import RequestCache
from unittests.dojo_test_case import DojoTestCase

_GCU = "dojo.authorization.query_registrations.get_current_user"

# Every action the role matrix can express. Deliberately spelled out rather than
# derived from Action so that adding an action to the enum forces a decision here
# instead of silently going untested.
ALL_ACTIONS = ("view", "add", "edit", "delete", "import", "manage", "own")

FLAG_STATES = ("off", "shadow", "on")

AUTH_LOGGER = "dojo.authorization.authorization"


@contextmanager
def request_cache_active():
    """
    Install a crum request carrying a ``RequestCache``, so ``@cache_for_request``
    actually caches. Without a current request the decorator is a passthrough and
    every call re-queries — which is fine for correctness tests but useless for
    the query-count ones.
    """
    request = RequestFactory().get("/")
    request.cache = RequestCache()
    set_current_request(request)
    try:
        yield request
    finally:
        set_current_request(None)


class RbacResolverBaseTestCase(DojoTestCase):

    """
    One Product under one Product_Type, plus a second, entirely unrelated Product
    used to prove that a Global_Role reaches objects no other grant touches.

    Every permutation below is a distinct user against the *same* product, so the
    only variable between them is the shape of the grant.
    """

    @classmethod
    def setUpTestData(cls):
        cls.role = {member: Role.objects.get(pk=member.value) for member in Roles}

        cls.product_type = Product_Type.objects.create(name="rbac_res_pt")
        cls.product = Product.objects.create(
            name="rbac_res_product", description="x", prod_type=cls.product_type,
        )
        # No membership of any kind is ever created against these two.
        cls.untouched_type = Product_Type.objects.create(name="rbac_res_untouched_pt")
        cls.untouched_product = Product.objects.create(
            name="rbac_res_untouched_product", description="x", prod_type=cls.untouched_type,
        )

        cls.nobody = cls._user("rbac_nobody")
        cls.staff = cls._user("rbac_staff", is_staff=True)

        # --- legacy authorized_users -------------------------------------------
        cls.legacy_member = cls._user("rbac_legacy_member")
        cls.product.authorized_users.add(cls.legacy_member)
        cls.legacy_type_member = cls._user("rbac_legacy_type_member")
        cls.product_type.authorized_users.add(cls.legacy_type_member)

        # --- direct Product_Member role grants ---------------------------------
        cls.reader = cls._product_member("rbac_reader", Roles.Reader)
        cls.writer = cls._product_member("rbac_writer", Roles.Writer)
        cls.maintainer = cls._product_member("rbac_maintainer", Roles.Maintainer)
        cls.owner = cls._product_member("rbac_owner", Roles.Owner)
        cls.api_importer = cls._product_member("rbac_api_importer", Roles.API_Importer)

        # --- group grant on the product ----------------------------------------
        cls.group_reader = cls._product_group_member("rbac_group_reader", "rbac_grp_reader", Roles.Reader)
        cls.group_owner = cls._product_group_member("rbac_group_owner", "rbac_grp_owner", Roles.Owner)

        # --- Product_Type grants, inherited by the product ---------------------
        cls.type_reader = cls._product_type_member("rbac_type_reader", Roles.Reader)
        cls.type_maintainer = cls._product_type_member("rbac_type_maintainer", Roles.Maintainer)
        cls.type_group_writer = cls._product_type_group_member(
            "rbac_type_group_writer", "rbac_grp_type_writer", Roles.Writer,
        )

        # --- global roles -------------------------------------------------------
        cls.global_owner = cls._global_role_user("rbac_global_owner", Roles.Owner)
        cls.global_reader = cls._global_role_user("rbac_global_reader", Roles.Reader)
        cls.global_maintainer = cls._global_role_user("rbac_global_maintainer", Roles.Maintainer)
        cls.global_owner_via_group = cls._global_role_group_user(
            "rbac_global_owner_grp", "rbac_grp_global_owner", Roles.Owner,
        )

        # --- combinations -------------------------------------------------------
        cls.legacy_plus_reader = cls._product_member("rbac_legacy_plus_reader", Roles.Reader)
        cls.product.authorized_users.add(cls.legacy_plus_reader)
        cls.reader_plus_group_owner = cls._product_member("rbac_reader_plus_grp_owner", Roles.Reader)
        owner_group = Dojo_Group.objects.create(name="rbac_grp_combo_owner")
        Dojo_Group_Member.objects.create(
            group=owner_group, user=cls.reader_plus_group_owner, role=cls.role[Roles.Reader],
        )
        Product_Group.objects.create(
            product=cls.product, group=owner_group, role=cls.role[Roles.Owner],
        )
        cls.staff_reader = cls._product_member("rbac_staff_reader", Roles.Reader, is_staff=True)

    # -- construction helpers ------------------------------------------------

    @classmethod
    def _user(cls, username, **kwargs):
        return Dojo_User.objects.create(username=username, is_active=True, **kwargs)

    @classmethod
    def _product_member(cls, username, role, **kwargs):
        user = cls._user(username, **kwargs)
        Product_Member.objects.create(product=cls.product, user=user, role=cls.role[role])
        return user

    @classmethod
    def _product_type_member(cls, username, role, **kwargs):
        user = cls._user(username, **kwargs)
        Product_Type_Member.objects.create(
            product_type=cls.product_type, user=user, role=cls.role[role],
        )
        return user

    @classmethod
    def _product_group_member(cls, username, group_name, role):
        user = cls._user(username)
        group = Dojo_Group.objects.create(name=group_name)
        Dojo_Group_Member.objects.create(group=group, user=user, role=cls.role[Roles.Reader])
        Product_Group.objects.create(product=cls.product, group=group, role=cls.role[role])
        return user

    @classmethod
    def _product_type_group_member(cls, username, group_name, role):
        user = cls._user(username)
        group = Dojo_Group.objects.create(name=group_name)
        Dojo_Group_Member.objects.create(group=group, user=user, role=cls.role[Roles.Reader])
        Product_Type_Group.objects.create(
            product_type=cls.product_type, group=group, role=cls.role[role],
        )
        return user

    @classmethod
    def _global_role_user(cls, username, role):
        user = cls._user(username)
        Global_Role.objects.create(user=user, role=cls.role[role])
        return user

    @classmethod
    def _global_role_group_user(cls, username, group_name, role):
        user = cls._user(username)
        group = Dojo_Group.objects.create(name=group_name)
        Dojo_Group_Member.objects.create(group=group, user=user, role=cls.role[Roles.Reader])
        Global_Role.objects.create(group=group, role=cls.role[role])
        return user

    # -- assertion helpers ---------------------------------------------------

    def actions_for(self, user, obj, state):
        """The set of actions ``user`` resolves as allowed on ``obj`` in ``state``."""
        with override_settings(FEATURE_RBAC=state):
            return {action for action in ALL_ACTIONS if user_has_permission(user, obj, action)}

    @property
    def permutations(self):
        """(label, user) for every grant shape built above."""
        return [
            ("nobody", self.nobody),
            ("staff", self.staff),
            ("staff+reader", self.staff_reader),
            ("legacy_product", self.legacy_member),
            ("legacy_product_type", self.legacy_type_member),
            ("member_reader", self.reader),
            ("member_writer", self.writer),
            ("member_maintainer", self.maintainer),
            ("member_owner", self.owner),
            ("member_api_importer", self.api_importer),
            ("group_reader", self.group_reader),
            ("group_owner", self.group_owner),
            ("type_member_reader", self.type_reader),
            ("type_member_maintainer", self.type_maintainer),
            ("type_group_writer", self.type_group_writer),
            ("global_owner", self.global_owner),
            ("global_reader", self.global_reader),
            ("global_maintainer", self.global_maintainer),
            ("global_owner_via_group", self.global_owner_via_group),
            ("legacy+member_reader", self.legacy_plus_reader),
            ("member_reader+group_owner", self.reader_plus_group_owner),
        ]


class TestFlagPlumbing(RbacResolverBaseTestCase):

    def test_default_state_is_shadow(self):
        # The shipped default, moved off -> shadow by PR 7 (roadmap §7.6). "shadow"
        # is still dark in the sense that matters: user_has_permission returns the
        # legacy answer in that state, so nobody's access changes on upgrade - it
        # only starts logging what the role model would have decided differently.
        # What must never happen by default is "on"; that is the assertion with
        # teeth here, and the roadmap's "dark by default" claim rests on it.
        self.assertEqual(feature_rbac_state(), "shadow")
        self.assertNotEqual(feature_rbac_state(), "on")

    def test_state_is_read_fresh_on_every_call(self):
        with override_settings(FEATURE_RBAC="on"):
            self.assertEqual(feature_rbac_state(), "on")
        self.assertEqual(feature_rbac_state(), "shadow")

    def test_unknown_state_fails_closed_onto_legacy(self):
        for bogus in ("", "ON!", "enabled", "true", None):
            with self.subTest(value=bogus), override_settings(FEATURE_RBAC=bogus):
                self.assertEqual(feature_rbac_state(), "off")
                # ...and a Reader-only member therefore still resolves as legacy.
                self.assertFalse(user_has_permission(self.reader, self.product, "view"))

    def test_state_is_case_and_whitespace_tolerant(self):
        with override_settings(FEATURE_RBAC="  ON  "):
            self.assertEqual(feature_rbac_state(), "on")


class TestLegacyAuthorizedUsersIsNotRegressed(RbacResolverBaseTestCase):

    """
    The no-regression property, stated directly: flipping the flag must not change
    what an existing ``authorized_users`` grant is worth.
    """

    def test_legacy_member_holds_the_same_actions_in_every_state(self):
        expected = set(LEGACY_AUTHORIZED_USERS_ACTIONS)
        for state in FLAG_STATES:
            with self.subTest(state=state):
                self.assertEqual(self.actions_for(self.legacy_member, self.product, state), expected)

    def test_legacy_member_is_exactly_view_add_edit_import(self):
        # Spelled out rather than referencing the constant, so that widening the
        # constant cannot silently widen this expectation too.
        self.assertEqual(
            self.actions_for(self.legacy_member, self.product, "on"),
            {"view", "add", "edit", "import"},
        )

    def test_legacy_member_cannot_delete_manage_or_own_under_on(self):
        for action in ("delete", "manage", "own"):
            with self.subTest(action=action), override_settings(FEATURE_RBAC="on"):
                self.assertFalse(user_has_permission(self.legacy_member, self.product, action))

    def test_legacy_product_type_grant_cascades_identically_in_every_state(self):
        expected = set(LEGACY_AUTHORIZED_USERS_ACTIONS)
        for state in FLAG_STATES:
            with self.subTest(state=state):
                self.assertEqual(
                    self.actions_for(self.legacy_type_member, self.product_type, state), expected,
                )
                self.assertEqual(
                    self.actions_for(self.legacy_type_member, self.product, state), expected,
                )

    def test_legacy_actions_constant_matches_what_legacy_actually_grants(self):
        # The constant is only meaningful if it equals the legacy engine's own
        # answer. Derive that answer instead of trusting the constant.
        self.assertEqual(
            self.actions_for(self.legacy_member, self.product, "off"),
            set(LEGACY_AUTHORIZED_USERS_ACTIONS),
        )


class TestRoleGrantsUnderOn(RbacResolverBaseTestCase):

    """Role semantics, only observable in the ``on`` state."""

    def test_reader_can_only_view(self):
        self.assertEqual(self.actions_for(self.reader, self.product, "on"), {"view"})

    def test_reader_cannot_add_edit_or_delete_anywhere(self):
        for obj in (self.product, self.product_type, self.untouched_product):
            for action in ("add", "edit", "delete", "import", "manage", "own"):
                with self.subTest(obj=type(obj).__name__, action=action), override_settings(FEATURE_RBAC="on"):
                    self.assertFalse(user_has_permission(self.reader, obj, action))

    def test_writer_cannot_delete(self):
        self.assertEqual(
            self.actions_for(self.writer, self.product, "on"),
            {"view", "add", "edit", "import"},
        )

    def test_api_importer_matches_writer(self):
        self.assertEqual(
            self.actions_for(self.api_importer, self.product, "on"),
            self.actions_for(self.writer, self.product, "on"),
        )

    def test_maintainer_can_delete_and_manage_but_not_own(self):
        self.assertEqual(
            self.actions_for(self.maintainer, self.product, "on"),
            {"view", "add", "edit", "import", "delete", "manage"},
        )

    def test_owner_holds_every_action(self):
        self.assertEqual(self.actions_for(self.owner, self.product, "on"), set(ALL_ACTIONS))

    def test_grant_does_not_leak_to_an_unrelated_product(self):
        for label, user in self.permutations:
            if label in {"staff", "staff+reader"} or label.startswith("global"):
                continue  # staff and global roles are unrestricted by design
            with self.subTest(grant=label), override_settings(FEATURE_RBAC="on"):
                self.assertFalse(user_has_permission(user, self.untouched_product, "view"))

    def test_group_grant_resolves_like_a_direct_grant(self):
        self.assertEqual(self.actions_for(self.group_reader, self.product, "on"), {"view"})
        self.assertEqual(self.actions_for(self.group_owner, self.product, "on"), set(ALL_ACTIONS))

    def test_product_type_grant_is_inherited_by_the_product(self):
        self.assertEqual(self.actions_for(self.type_reader, self.product, "on"), {"view"})
        self.assertEqual(
            self.actions_for(self.type_maintainer, self.product, "on"),
            {"view", "add", "edit", "import", "delete", "manage"},
        )
        self.assertEqual(
            self.actions_for(self.type_group_writer, self.product, "on"),
            {"view", "add", "edit", "import"},
        )

    def test_grants_union_rather_than_the_last_one_winning(self):
        # legacy {view,add,edit,import} unioned with Reader {view} = the legacy set.
        self.assertEqual(
            self.actions_for(self.legacy_plus_reader, self.product, "on"),
            {"view", "add", "edit", "import"},
        )
        # Reader {view} unioned with group Owner {everything} = everything.
        self.assertEqual(
            self.actions_for(self.reader_plus_group_owner, self.product, "on"),
            set(ALL_ACTIONS),
        )

    def test_user_with_no_grant_gets_nothing(self):
        for state in FLAG_STATES:
            with self.subTest(state=state):
                self.assertEqual(self.actions_for(self.nobody, self.product, state), set())

    def test_staff_still_bypasses_everything_in_every_state(self):
        for state in FLAG_STATES:
            with self.subTest(state=state):
                self.assertEqual(self.actions_for(self.staff, self.product, state), set(ALL_ACTIONS))
                self.assertEqual(
                    self.actions_for(self.staff, self.untouched_product, state), set(ALL_ACTIONS),
                )

    def test_staff_flag_is_not_narrowed_by_holding_a_reader_role(self):
        # A Reader grant must never *reduce* what is_staff already allows.
        for state in FLAG_STATES:
            with self.subTest(state=state):
                self.assertEqual(
                    self.actions_for(self.staff_reader, self.product, state), set(ALL_ACTIONS),
                )


class TestGlobalRole(RbacResolverBaseTestCase):

    """
    A Global_Role is "applied to all product types and products" — so it is
    resolved as an unrestricted bypass, not as a per-object grant.
    """

    def test_global_owner_reaches_a_product_with_no_other_membership(self):
        # The headline case: no Product_Member, no Product_Group, no
        # Product_Type grant, no authorized_users row anywhere.
        self.assertFalse(
            Product_Member.objects.filter(user=self.global_owner).exists(),
        )
        self.assertFalse(
            self.untouched_product.authorized_users.filter(pk=self.global_owner.pk).exists(),
        )
        self.assertEqual(
            self.actions_for(self.global_owner, self.untouched_product, "on"), set(ALL_ACTIONS),
        )
        self.assertEqual(
            self.actions_for(self.global_owner, self.untouched_type, "on"), set(ALL_ACTIONS),
        )

    def test_global_owner_is_dark_under_off_and_shadow(self):
        for state in ("off", "shadow"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.actions_for(self.global_owner, self.untouched_product, state), set(),
                )

    def test_global_reader_is_read_only_everywhere(self):
        self.assertEqual(
            self.actions_for(self.global_reader, self.untouched_product, "on"), {"view"},
        )

    def test_global_role_granted_through_a_group_behaves_the_same(self):
        self.assertEqual(
            self.actions_for(self.global_owner_via_group, self.untouched_product, "on"),
            set(ALL_ACTIONS),
        )

    def test_global_role_extras_apply_to_global_permission_checks(self):
        # get_global_roles_with_permissions() grants Maintainer/Owner an extra
        # "add" that is meaningless scoped to a single product.
        with override_settings(FEATURE_RBAC="on"):
            self.assertTrue(user_has_global_permission(self.global_maintainer, "add"))
            self.assertTrue(user_has_global_permission(self.global_owner, "add"))
            self.assertFalse(user_has_global_permission(self.global_reader, "add"))
            self.assertFalse(user_has_global_permission(self.nobody, "add"))

    def test_global_permission_is_staff_only_under_off(self):
        for state in ("off", "shadow"):
            with self.subTest(state=state), override_settings(FEATURE_RBAC=state):
                self.assertFalse(user_has_global_permission(self.global_owner, "add"))
                self.assertTrue(user_has_global_permission(self.staff, "add"))

    def test_global_permission_still_denies_superuser_only_actions(self):
        for state in FLAG_STATES:
            with self.subTest(state=state), override_settings(FEATURE_RBAC=state):
                self.assertFalse(user_has_global_permission(self.global_owner, "superuser_only"))
                self.assertFalse(user_has_global_permission(self.staff, "superuser_only"))


class TestShadowNeverChangesAnAnswer(RbacResolverBaseTestCase):

    """
    ``shadow`` returns the legacy answer, full stop. This is the property that
    makes turning shadow on in production a no-op for users.
    """

    def test_shadow_equals_off_for_every_permutation_and_action(self):
        for label, user in self.permutations:
            for obj_label, obj in (
                ("product", self.product),
                ("product_type", self.product_type),
                ("untouched_product", self.untouched_product),
            ):
                off = self.actions_for(user, obj, "off")
                shadow = self.actions_for(user, obj, "shadow")
                with self.subTest(grant=label, obj=obj_label):
                    self.assertEqual(shadow, off)

    def test_shadow_differs_from_on_where_roles_actually_add_something(self):
        # Guards the test above from being vacuous: if shadow == off == on for
        # everything, the equality assertion proves nothing.
        self.assertNotEqual(
            self.actions_for(self.reader, self.product, "shadow"),
            self.actions_for(self.reader, self.product, "on"),
        )

    def test_shadow_equals_off_for_global_permissions(self):
        for label, user in self.permutations:
            for action in ALL_ACTIONS:
                with override_settings(FEATURE_RBAC="off"):
                    off = user_has_global_permission(user, action)
                with override_settings(FEATURE_RBAC="shadow"):
                    shadow = user_has_global_permission(user, action)
                with self.subTest(grant=label, action=action):
                    self.assertEqual(shadow, off)


class TestShadowDivergenceLogging(RbacResolverBaseTestCase):

    def test_warning_is_logged_when_the_resolvers_disagree(self):
        # Product_Member(Reader) with no authorized_users row: legacy denies
        # (not in authorized_users), rbac allows (Reader grants view).
        with override_settings(FEATURE_RBAC="shadow"), self.assertLogs(AUTH_LOGGER, level="WARNING") as captured:
            self.assertFalse(user_has_permission(self.reader, self.product, "view"))
        joined = "\n".join(captured.output)
        self.assertIn("RBAC shadow divergence", joined)
        self.assertIn(self.reader.username, joined)
        self.assertIn("Product", joined)
        self.assertIn("legacy=False", joined)
        self.assertIn("rbac=True", joined)

    def test_no_warning_is_logged_when_the_resolvers_agree(self):
        with override_settings(FEATURE_RBAC="shadow"), self.assertNoLogs(AUTH_LOGGER, level="WARNING"):
            # legacy: authorized_users member → allowed. rbac: same, via the
            # LEGACY_AUTHORIZED_USERS_ACTIONS grant.
            self.assertTrue(user_has_permission(self.legacy_member, self.product, "view"))
            # Both deny an outsider.
            self.assertFalse(user_has_permission(self.nobody, self.product, "view"))
            # Both allow staff.
            self.assertTrue(user_has_permission(self.staff, self.product, "delete"))

    def test_no_warning_is_logged_in_the_off_state(self):
        with override_settings(FEATURE_RBAC="off"), self.assertNoLogs(AUTH_LOGGER, level="WARNING"):
            self.assertFalse(user_has_permission(self.reader, self.product, "view"))

    def test_no_warning_is_logged_in_the_on_state(self):
        with override_settings(FEATURE_RBAC="on"), self.assertNoLogs(AUTH_LOGGER, level="WARNING"):
            self.assertTrue(user_has_permission(self.reader, self.product, "view"))

    def test_global_divergence_is_logged(self):
        with override_settings(FEATURE_RBAC="shadow"), self.assertLogs(AUTH_LOGGER, level="WARNING") as captured:
            self.assertFalse(user_has_global_permission(self.global_owner, "delete"))
        self.assertIn("RBAC shadow divergence (global)", "\n".join(captured.output))


class TestManageMembersRegression(RbacResolverBaseTestCase):

    """
    The coupled change: ``permission_to_action()`` now maps ``_Manage_`` →
    ``Action.Manage`` and ``_Add_Owner`` → ``Action.Own``, which is only safe
    because ``_legacy_authorized()`` still treats both as staff-only.

    Replaces PR 2's ``TestPermissionToActionIsUnchanged`` guard with the outcome
    it was really protecting.
    """

    def test_non_staff_legacy_member_is_denied_manage_members_under_off(self):
        for permission in (Permissions.Product_Manage_Members, Permissions.Product_Type_Manage_Members):
            with self.subTest(permission=permission.name), override_settings(FEATURE_RBAC="off"):
                self.assertFalse(user_has_permission(self.legacy_member, self.product, permission))
                self.assertFalse(user_has_permission(self.legacy_type_member, self.product_type, permission))

    def test_non_staff_legacy_member_is_denied_manage_members_under_shadow_and_on(self):
        for state in ("shadow", "on"):
            with self.subTest(state=state), override_settings(FEATURE_RBAC=state):
                self.assertFalse(
                    user_has_permission(self.legacy_member, self.product, Permissions.Product_Manage_Members),
                )

    def test_staff_keeps_manage_members_in_every_state(self):
        for state in FLAG_STATES:
            with self.subTest(state=state), override_settings(FEATURE_RBAC=state):
                self.assertTrue(
                    user_has_permission(self.staff, self.product, Permissions.Product_Manage_Members),
                )

    def test_maintainer_gains_manage_members_under_on_without_being_staff(self):
        self.assertFalse(self.maintainer.is_staff)
        with override_settings(FEATURE_RBAC="off"):
            self.assertFalse(
                user_has_permission(self.maintainer, self.product, Permissions.Product_Manage_Members),
            )
        with override_settings(FEATURE_RBAC="on"):
            self.assertTrue(
                user_has_permission(self.maintainer, self.product, Permissions.Product_Manage_Members),
            )

    def test_add_owner_needs_the_owner_role_under_on(self):
        with override_settings(FEATURE_RBAC="on"):
            self.assertFalse(
                user_has_permission(self.maintainer, self.product, Permissions.Product_Member_Add_Owner),
            )
            self.assertTrue(
                user_has_permission(self.owner, self.product, Permissions.Product_Member_Add_Owner),
            )

    def test_add_owner_is_staff_only_under_off(self):
        with override_settings(FEATURE_RBAC="off"):
            self.assertFalse(
                user_has_permission(self.owner, self.product, Permissions.Product_Member_Add_Owner),
            )
            self.assertTrue(
                user_has_permission(self.staff, self.product, Permissions.Product_Member_Add_Owner),
            )


class TestActionMapCaching(DojoTestCase):

    """
    The action maps must cost a fixed number of queries per request, regardless of
    how many objects are checked or how many distinct actions are asked about.

    An action-blind cache would pass the object-count assertions but fail the
    two-actions one; a per-(user, action) cache would pass the two-actions one
    only by rebuilding, which the query counts catch.
    """

    PRODUCT_COUNT = 8

    @classmethod
    def setUpTestData(cls):
        cls.role = {member: Role.objects.get(pk=member.value) for member in Roles}
        cls.product_type = Product_Type.objects.create(name="rbac_cache_pt")
        cls.products = [
            Product.objects.create(
                name=f"rbac_cache_product_{index}", description="x", prod_type=cls.product_type,
            )
            for index in range(cls.PRODUCT_COUNT)
        ]
        cls.user = Dojo_User.objects.create(username="rbac_cache_user", is_active=True)
        for product in cls.products:
            Product_Member.objects.create(
                product=product, user=cls.user, role=cls.role[Roles.Maintainer],
            )

    def _sweep(self, products, actions):
        with override_settings(FEATURE_RBAC="on"), CaptureQueriesContext(connection) as captured:
            for product in products:
                for action in actions:
                    user_has_permission(self.user, product, action)
        return len(captured)

    def test_query_count_does_not_grow_with_the_number_of_objects_checked(self):
        with request_cache_active():
            few = self._sweep(self.products[:2], ("view",))
        with request_cache_active():
            many = self._sweep(self.products, ("view",))
        self.assertEqual(
            many, few,
            msg=f"query count scaled with object count ({few} for 2 products, {many} for {self.PRODUCT_COUNT})",
        )

    def test_query_count_does_not_grow_with_the_number_of_distinct_actions(self):
        with request_cache_active():
            one_action = self._sweep(self.products, ("view",))
        with request_cache_active():
            all_actions = self._sweep(self.products, ("view", "edit", "delete", "manage", "own"))
        self.assertEqual(
            all_actions, one_action,
            msg="checking a second action rebuilt the map — the cache has become action-keyed",
        )

    def test_a_second_action_in_the_same_request_issues_no_queries_at_all(self):
        with request_cache_active(), override_settings(FEATURE_RBAC="on"):
            user_has_permission(self.user, self.products[0], "view")
            with CaptureQueriesContext(connection) as captured:
                user_has_permission(self.user, self.products[0], "delete")
                user_has_permission(self.user, self.products[1], "manage")
            self.assertEqual(
                len(captured), 0,
                msg=f"expected the cached maps to answer without queries, got {[q['sql'] for q in captured]}",
            )

    def test_product_action_map_is_built_from_a_bounded_number_of_queries(self):
        with request_cache_active(), CaptureQueriesContext(connection) as captured:
            rbac_product_action_map(self.user.pk)
        self.assertLessEqual(
            len(captured), 6,
            msg=f"rbac_product_action_map issued {len(captured)} queries: {[q['sql'] for q in captured]}",
        )

    def test_product_type_action_map_is_built_from_a_bounded_number_of_queries(self):
        with request_cache_active(), CaptureQueriesContext(connection) as captured:
            rbac_product_type_action_map(self.user.pk)
        self.assertLessEqual(
            len(captured), 3,
            msg=f"rbac_product_type_action_map issued {len(captured)} queries: {[q['sql'] for q in captured]}",
        )

    def test_maps_are_action_aware_even_though_the_cache_key_is_not(self):
        with request_cache_active():
            action_map = rbac_product_action_map(self.user.pk)
            self.assertEqual(
                action_map[self.products[0].pk],
                frozenset({"view", "add", "edit", "import", "delete", "manage"}),
            )
            # Same cached object, still carrying the full per-object action set.
            self.assertIs(action_map, rbac_product_action_map(self.user.pk))

    def test_map_is_empty_for_a_missing_user(self):
        self.assertEqual(rbac_product_action_map(None), {})
        self.assertEqual(rbac_product_type_action_map(None), {})


class TestUngrantableActionQueries(RbacResolverBaseTestCase):

    """
    ``staff_only`` is granted by no role and is not in
    ``LEGACY_AUTHORIZED_USERS_ACTIONS``, so the role-aware ``Q`` collapses to its
    "matches nothing" identity element. Django turns a ``__in=[]`` branch into an
    EmptyResultSet during compilation, including when it is the subquery of an
    outer ``id__in`` — exercised here rather than assumed, since a compile-time
    error would only surface on a real request.
    """

    def test_ungrantable_action_yields_an_empty_product_list_without_raising(self):
        with override_settings(FEATURE_RBAC="on"), patch(_GCU, return_value=self.owner):
            self.assertEqual(get_authorized_products("staff_only").count(), 0)
            self.assertEqual(get_authorized_product_types("staff_only").count(), 0)

    def test_ungrantable_action_propagates_through_a_child_queryset(self):
        # The outer-query path: Engagement filtered by an empty product subquery.
        with override_settings(FEATURE_RBAC="on"), patch(_GCU, return_value=self.owner):
            self.assertEqual(get_authorized_engagements("staff_only").count(), 0)

    def test_staff_still_bypasses_an_ungrantable_action(self):
        with override_settings(FEATURE_RBAC="on"), patch(_GCU, return_value=self.staff):
            self.assertEqual(
                get_authorized_products("staff_only").count(), Product.objects.count(),
            )
