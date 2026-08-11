"""
Role → action matrix and the pure role helpers built on it.

This is the unit under test for INTEGRATIONS_ROADMAP.md §7.3 (Epic 0, PR 2).
Everything here is dark: nothing in ``user_has_permission()`` consults the
matrix yet, so these tests describe intended authority, not current
enforcement. Legacy resolution behavior is covered in test_authorization.py
and must remain unchanged by anything in this file.

Risk R11 in §7.11 names the failure mode these tests exist to catch: a matrix
that *looks* usable shipping as "fixed" while Reader can still ``add`` or
Writer can still ``delete``. Assertions on Reader/Writer/Owner are therefore
written as exact set equality or explicit exclusion, not as "contains".
"""
from django.contrib.auth.models import AnonymousUser

from dojo.authorization.authorization import (
    get_roles_for_permission,
    role_has_global_permission,
    role_has_permission,
    user_is_superuser_or_global_owner,
)
from dojo.authorization.roles_permissions import (
    Action,
    Permissions,
    Roles,
    get_global_roles_with_permissions,
    get_roles_with_permissions,
    permission_to_action,
)
from dojo.models import (
    Dojo_Group,
    Dojo_Group_Member,
    Dojo_User,
    Global_Role,
    Role,
)
from unittests.dojo_test_case import DojoTestCase


class TestRoleActionMatrix(DojoTestCase):

    """The corrected matrix from §7.3, asserted role by role."""

    def setUp(self):
        self.matrix = get_roles_with_permissions()

    def test_reader_is_exactly_view(self):
        # The single assertion this entire PR exists for: a "read-only
        # stakeholder" must not be able to create Engagements/Findings/etc.
        # The pre-fix matrix granted Reader {"view", "add"}.
        self.assertEqual(self.matrix[Roles.Reader], {"view"})

    def test_writer_cannot_delete(self):
        # Writer historically could not delete Findings/Tests/Engagements;
        # the flattened matrix had granted it.
        self.assertNotIn("delete", self.matrix[Roles.Writer])
        self.assertEqual(self.matrix[Roles.Writer], {"view", "add", "edit", "import"})

    def test_owner_outranks_maintainer_via_own(self):
        # Before the fix these two sets were identical, which left
        # Role.is_owner carrying no actual authority anywhere.
        self.assertIn("own", self.matrix[Roles.Owner])
        self.assertNotIn("own", self.matrix[Roles.Maintainer])
        self.assertNotEqual(self.matrix[Roles.Owner], self.matrix[Roles.Maintainer])

    def test_maintainer_adds_delete_and_manage_over_writer(self):
        self.assertEqual(
            self.matrix[Roles.Maintainer] - self.matrix[Roles.Writer],
            {"delete", "manage"},
        )

    def test_api_importer_unchanged(self):
        self.assertEqual(self.matrix[Roles.API_Importer], {"view", "add", "edit", "import"})

    def test_ladder_is_cumulative(self):
        # Reader ⊂ Writer ⊂ Maintainer ⊂ Owner. API_Importer sits outside the
        # ladder deliberately (same set as Writer) and is excluded here.
        for lower, higher in (
            (Roles.Reader, Roles.Writer),
            (Roles.Writer, Roles.Maintainer),
            (Roles.Maintainer, Roles.Owner),
        ):
            with self.subTest(lower=lower.name, higher=higher.name):
                self.assertTrue(self.matrix[lower] < self.matrix[higher])

    def test_every_role_is_covered(self):
        self.assertEqual(set(self.matrix), set(Roles))

    def test_matrix_only_uses_real_actions(self):
        known = {action.value for action in Action}
        for role, actions in self.matrix.items():
            with self.subTest(role=role.name):
                self.assertTrue(actions <= known, f"unknown action(s): {actions - known}")

    def test_no_role_grants_staff_only_or_superuser_only(self):
        # staff_only is superseded by manage/own in the matrix; superuser_only
        # is by definition never delegated to a role.
        for role, actions in self.matrix.items():
            with self.subTest(role=role.name):
                self.assertNotIn("staff_only", actions)
                self.assertNotIn("superuser_only", actions)

    def test_global_role_extras_unchanged(self):
        # Extra grants layered on top of the object-scoped sets for global
        # roles only. Deliberately untouched by this PR.
        self.assertEqual(
            get_global_roles_with_permissions(),
            {Roles.Maintainer: {"add"}, Roles.Owner: {"add"}},
        )

    def test_new_actions_exist_with_expected_values(self):
        self.assertEqual(Action.Manage.value, "manage")
        self.assertEqual(Action.Own.value, "own")


class TestPermissionToActionManageAndOwn(DojoTestCase):

    """
    Replaces PR 2's ``TestPermissionToActionIsUnchanged`` guard rail, deliberately.

    That class pinned ``_Manage_``/``_Add_Owner`` to ``Action.StaffOnly`` because
    ``StaffOnly`` was what routed those permissions into the
    ``if action in {StaffOnly, Delete}: return user.is_staff`` short-circuit in
    ``user_has_permission()``. Repointing them *alone* would have dropped the
    Manage-Members views into the action-blind membership check, letting any
    ``authorized_users`` member manage grants — so PR 2 deferred the remap and
    said to update these tests here, together with the coverage that replaces
    them.

    PR 3 makes the remap safe by flag-gating the short-circuit rather than
    deleting it: ``authorization._legacy_authorized()`` now treats
    ``{StaffOnly, Delete, Manage, Own}`` as staff-only, so under
    ``DD_FEATURE_RBAC=off`` these permissions resolve exactly as they always did.
    The coverage that replaces the old guard rail is the behavioral pin in
    ``unittests/authorization/test_rbac_resolver.py``
    (``TestManageMembersRegression``), which asserts the actual outcome — a
    non-staff ``authorized_users`` member is still denied
    ``Product_Manage_Members`` under ``off`` — rather than the enum value that
    used to imply it.
    """

    def test_manage_members_resolves_to_manage(self):
        self.assertEqual(permission_to_action(Permissions.Product_Manage_Members), Action.Manage)
        self.assertEqual(permission_to_action(Permissions.Product_Type_Manage_Members), Action.Manage)
        self.assertEqual(permission_to_action(Permissions.Group_Manage_Members), Action.Manage)

    def test_add_owner_resolves_to_own(self):
        self.assertEqual(permission_to_action(Permissions.Product_Member_Add_Owner), Action.Own)
        self.assertEqual(permission_to_action(Permissions.Product_Type_Member_Add_Owner), Action.Own)
        self.assertEqual(permission_to_action(Permissions.Product_Group_Add_Owner), Action.Own)
        self.assertEqual(permission_to_action(Permissions.Product_Type_Group_Add_Owner), Action.Own)
        self.assertEqual(permission_to_action(Permissions.Group_Add_Owner), Action.Own)

    def test_staff_only_is_still_an_accepted_input_shape(self):
        # No permission name produces StaffOnly any more, but it remains a valid
        # thing to pass explicitly (and remains granted by no role).
        self.assertEqual(permission_to_action(Action.StaffOnly), Action.StaffOnly)
        self.assertEqual(permission_to_action("staff_only"), Action.StaffOnly)

    def test_unrelated_mappings_still_hold(self):
        self.assertEqual(permission_to_action(Permissions.Product_View), Action.View)
        self.assertEqual(permission_to_action(Permissions.Product_Edit), Action.Edit)
        self.assertEqual(permission_to_action(Permissions.Product_Delete), Action.Delete)
        self.assertEqual(permission_to_action(Permissions.Engagement_Add), Action.Add)
        self.assertEqual(permission_to_action(Permissions.Import_Scan_Result), Action.Import)
        self.assertEqual(permission_to_action(Permissions.Risk_Acceptance), Action.Edit)


class TestRoleHelperRoundTrip(DojoTestCase):

    """
    ``get_roles_for_permission`` / ``role_has_permission`` /
    ``role_has_global_permission`` must agree with the matrix for every
    (role, action) pair, across every accepted permission shape.
    """

    def test_role_has_permission_matches_matrix_for_every_pair(self):
        matrix = get_roles_with_permissions()
        for role in Roles:
            for action in Action:
                with self.subTest(role=role.name, action=action.value):
                    self.assertEqual(
                        role_has_permission(role, action),
                        action.value in matrix[role],
                    )

    def test_get_roles_for_permission_is_the_inverse_of_the_matrix(self):
        matrix = get_roles_with_permissions()
        for action in Action:
            with self.subTest(action=action.value):
                self.assertEqual(
                    get_roles_for_permission(action),
                    {role for role, actions in matrix.items() if action.value in actions},
                )

    def test_get_roles_for_permission_concrete_cases(self):
        self.assertEqual(get_roles_for_permission("view"), set(Roles))
        self.assertEqual(
            get_roles_for_permission("add"),
            {Roles.API_Importer, Roles.Writer, Roles.Maintainer, Roles.Owner},
        )
        self.assertEqual(get_roles_for_permission("delete"), {Roles.Maintainer, Roles.Owner})
        self.assertEqual(get_roles_for_permission("manage"), {Roles.Maintainer, Roles.Owner})
        self.assertEqual(get_roles_for_permission("own"), {Roles.Owner})
        self.assertEqual(get_roles_for_permission(Action.StaffOnly), set())

    def test_accepts_every_permission_shape(self):
        # Action member, action string, Permissions member, legacy enum-name
        # string — all four must land on the same answer.
        for shape in (Action.Edit, "edit", Permissions.Product_Edit, "Product_Edit"):
            with self.subTest(shape=repr(shape)):
                self.assertFalse(role_has_permission(Roles.Reader, shape))
                self.assertTrue(role_has_permission(Roles.Writer, shape))
                self.assertEqual(
                    get_roles_for_permission(shape),
                    {Roles.API_Importer, Roles.Writer, Roles.Maintainer, Roles.Owner},
                )

    def test_global_permission_includes_object_scoped_grants(self):
        self.assertTrue(role_has_global_permission(Roles.Owner, "own"))
        self.assertTrue(role_has_global_permission(Roles.Writer, "edit"))
        self.assertFalse(role_has_global_permission(Roles.Reader, "edit"))

    def test_global_permission_includes_the_extra_grants(self):
        # Reader/Writer/API_Importer have no extras, so global == object-scoped
        # for them; Maintainer/Owner pick up "add" — which they already have,
        # making this a no-op today but keeping the union semantics honest.
        for role in Roles:
            for action in Action:
                with self.subTest(role=role.name, action=action.value):
                    expected = role_has_permission(role, action) or (
                        action.value in get_global_roles_with_permissions().get(role, set())
                    )
                    self.assertEqual(role_has_global_permission(role, action), expected)

    def test_unknown_role_is_falsy_not_an_error(self):
        self.assertFalse(role_has_permission(9999, "view"))
        self.assertFalse(role_has_global_permission(9999, "view"))
        self.assertFalse(role_has_permission(None, "view"))

    def test_helpers_do_not_touch_the_database(self):
        # These are pure functions over a module-level dict; the resolver that
        # needs queries is PR 3. A stray query here would land on every
        # permission check once wired up.
        with self.assertNumQueries(0):
            get_roles_for_permission(Permissions.Product_Edit)
            role_has_permission(Roles.Owner, Permissions.Product_Delete)
            role_has_global_permission(Roles.Maintainer, "add")


class TestUserIsSuperuserOrGlobalOwner(DojoTestCase):

    """
    Regression guard first, new behavior second.

    ``dojo_global_role`` has no rows in this deployment and nothing can create
    one until the group/role UI and API land, so the first two tests pin
    today's exact answers — they must keep passing for this PR to be dark.
    The rest exercise the Global_Role arms that only become reachable later.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner_role = Role.objects.get_or_create(name="gr_test_owner", defaults={"is_owner": True})[0]
        cls.reader_role = Role.objects.get_or_create(name="gr_test_reader", defaults={"is_owner": False})[0]

    # ---- no-regression: today's behavior, with no Global_Role rows ---------

    def test_superuser_is_true_with_no_global_role(self):
        superuser = Dojo_User.objects.create(username="gr_superuser", is_superuser=True)
        self.assertTrue(user_is_superuser_or_global_owner(superuser))

    def test_plain_user_is_false_with_no_global_role(self):
        user = Dojo_User.objects.create(username="gr_plain")
        self.assertFalse(user_is_superuser_or_global_owner(user))

    def test_staff_alone_is_not_a_global_owner(self):
        staff = Dojo_User.objects.create(username="gr_staff", is_staff=True)
        self.assertFalse(user_is_superuser_or_global_owner(staff))

    def test_none_and_anonymous_are_false(self):
        self.assertFalse(user_is_superuser_or_global_owner(None))
        self.assertFalse(user_is_superuser_or_global_owner(AnonymousUser()))

    def test_unsaved_user_is_false(self):
        self.assertFalse(user_is_superuser_or_global_owner(Dojo_User(username="gr_unsaved")))

    def test_superuser_is_true_regardless_of_global_role_state(self):
        superuser = Dojo_User.objects.create(username="gr_superuser_with_role", is_superuser=True)
        Global_Role.objects.create(user=superuser, role=self.reader_role)
        self.assertTrue(user_is_superuser_or_global_owner(superuser))

    # ---- the arms that go live later --------------------------------------

    def test_direct_global_owner_role(self):
        user = Dojo_User.objects.create(username="gr_direct_owner")
        Global_Role.objects.create(user=user, role=self.owner_role)
        self.assertTrue(user_is_superuser_or_global_owner(user))

    def test_direct_global_non_owner_role(self):
        user = Dojo_User.objects.create(username="gr_direct_reader")
        Global_Role.objects.create(user=user, role=self.reader_role)
        self.assertFalse(user_is_superuser_or_global_owner(user))

    def test_global_role_row_with_null_role(self):
        # Global_Role.role is nullable; a row without one grants nothing.
        user = Dojo_User.objects.create(username="gr_null_role")
        Global_Role.objects.create(user=user, role=None)
        self.assertFalse(user_is_superuser_or_global_owner(user))

    def test_global_owner_via_group_membership(self):
        user = Dojo_User.objects.create(username="gr_group_owner")
        group = Dojo_Group.objects.create(name="gr_test_owner_group")
        Global_Role.objects.create(group=group, role=self.owner_role)
        Dojo_Group_Member.objects.create(group=group, user=user, role=self.reader_role)
        self.assertTrue(user_is_superuser_or_global_owner(user))

    def test_group_with_non_owner_global_role(self):
        user = Dojo_User.objects.create(username="gr_group_reader")
        group = Dojo_Group.objects.create(name="gr_test_reader_group")
        Global_Role.objects.create(group=group, role=self.reader_role)
        Dojo_Group_Member.objects.create(group=group, user=user, role=self.reader_role)
        self.assertFalse(user_is_superuser_or_global_owner(user))

    def test_non_member_of_owner_group_is_not_a_global_owner(self):
        outsider = Dojo_User.objects.create(username="gr_outsider")
        group = Dojo_Group.objects.create(name="gr_test_other_owner_group")
        Global_Role.objects.create(group=group, role=self.owner_role)
        self.assertFalse(user_is_superuser_or_global_owner(outsider))
