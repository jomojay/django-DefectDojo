"""
End-to-end tests for the ``dojo/group/`` UI (INTEGRATIONS_ROADMAP.md §7.5).

Real Django test client against real URLs, so the whole stack is exercised:
URL routing, ``AuthorizationMiddleware`` reading
``URL_PERMISSIONS``, the views, the forms, and the ``auth.Group`` mirror
signals firing underneath.

Note on status codes: ``PermissionDenied`` is routed through
``dojo.views.custom_unauthorized_view`` (``handler403``), which renders the 403
template with **status 400** — the same convention
``unittests/test_authorized_users_ui.py`` already documents.
"""
from crum import impersonate
from django.contrib.auth.models import Permission
from django.urls import reverse

from dojo.authorization.models import Dojo_Group, Dojo_Group_Member, Role
from dojo.group.queries import get_authorized_groups, get_group_member_roles
from dojo.models import Dojo_User
from unittests.dojo_test_case import DojoTestCase

DENIED = 400


class GroupUIBaseTestCase(DojoTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.superuser = Dojo_User.objects.create(username="group_ui_super", is_superuser=True)
        cls.staff = Dojo_User.objects.create(username="group_ui_staff", is_staff=True)
        cls.nobody = Dojo_User.objects.create(username="group_ui_nobody")
        cls.target = Dojo_User.objects.create(username="group_ui_target", is_active=True)

        # Non-staff, non-superuser, but explicitly granted the Django
        # configuration permissions group administration keys on.
        cls.config_admin = Dojo_User.objects.create(username="group_ui_config_admin")
        cls.config_admin.user_permissions.add(
            Permission.objects.get(codename="add_group", content_type__app_label="auth"),
            Permission.objects.get(codename="view_group", content_type__app_label="auth"),
        )

        cls.owner_role = Role.objects.get(name="Owner")
        cls.reader_role = Role.objects.get(name="Reader")
        cls.maintainer_role = Role.objects.get(name="Maintainer")


class TestGroupCrudViews(GroupUIBaseTestCase):

    def test_add_group_denied_without_config_permission(self):
        self.client.force_login(self.nobody)
        response = self.client.post(reverse("add_group"), {"name": "denied_group", "description": ""})
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Dojo_Group.objects.filter(name="denied_group").exists())

    def test_add_group_as_superuser_creates_group_and_owner(self):
        self.client.force_login(self.superuser)
        response = self.client.post(reverse("add_group"), {"name": "super_group", "description": "made by super"})
        self.assertEqual(response.status_code, 302)

        group = Dojo_Group.objects.get(name="super_group")
        # The signal chain fired through the real request path, not just in a
        # model-level test: mirror created, creator seated as Owner.
        self.assertIsNotNone(group.auth_group)
        member = Dojo_Group_Member.objects.get(group=group, user=self.superuser)
        self.assertTrue(member.role.is_owner)
        self.assertTrue(group.auth_group.user_set.filter(pk=self.superuser.pk).exists())

    def test_add_group_with_config_permission_succeeds(self):
        self.client.force_login(self.config_admin)
        response = self.client.post(reverse("add_group"), {"name": "config_group", "description": ""})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Dojo_Group.objects.filter(name="config_group").exists())

    def test_groups_list_requires_view_group_config_permission(self):
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(reverse("groups")).status_code, DENIED)

        self.client.force_login(self.config_admin)
        self.assertEqual(self.client.get(reverse("groups")).status_code, 200)

    def test_groups_list_shows_authorized_groups(self):
        Dojo_Group.objects.create(name="listed_group")
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("groups"))

        self.assertEqual(response.status_code, 200)
        listed = {group.name for group in response.context["groups"]}
        self.assertIn("listed_group", listed)

    def test_groups_list_filter_narrows_results(self):
        Dojo_Group.objects.create(name="filter_alpha")
        Dojo_Group.objects.create(name="filter_beta")
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("groups"), {"name": "alpha"})

        listed = {group.name for group in response.context["groups"]}
        self.assertIn("filter_alpha", listed)
        self.assertNotIn("filter_beta", listed)

    def test_view_edit_delete_group_round_trip(self):
        group = Dojo_Group.objects.create(name="crud_group", description="before")
        self.client.force_login(self.superuser)

        self.assertEqual(self.client.get(reverse("view_group", args=(group.id,))).status_code, 200)

        response = self.client.post(
            reverse("edit_group", args=(group.id,)),
            {"name": "crud_group_renamed", "description": "after"},
        )
        self.assertEqual(response.status_code, 302)
        group.refresh_from_db()
        self.assertEqual(group.name, "crud_group_renamed")
        self.assertEqual(group.description, "after")

        response = self.client.post(reverse("delete_group", args=(group.id,)), {"id": group.id})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Dojo_Group.objects.filter(pk=group.pk).exists())

    def test_group_routes_denied_for_non_staff(self):
        """Dojo_Group has no per-object grant today, so object-level checks reduce to staff/superuser."""
        group = Dojo_Group.objects.create(name="guarded_group")
        self.client.force_login(self.nobody)

        for url_name in ("view_group", "edit_group", "delete_group"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name, args=(group.id,)))
                self.assertEqual(response.status_code, DENIED)


class TestGroupMemberViews(GroupUIBaseTestCase):

    def setUp(self):
        super().setUp()
        self.group = Dojo_Group.objects.create(name="member_group")
        self.group.refresh_from_db()

    def test_add_member_round_trip(self):
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("add_group_member", args=(self.group.id,)),
            {"group": self.group.id, "users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)

        member = Dojo_Group_Member.objects.get(group=self.group, user=self.target)
        self.assertEqual(member.role, self.reader_role)
        # Membership synced into the mirrored auth group by the signal.
        self.assertTrue(self.group.auth_group.user_set.filter(pk=self.target.pk).exists())

    def test_add_member_role_choices_exclude_api_importer_and_writer(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("add_group_member", args=(self.group.id,)))

        self.assertEqual(response.status_code, 200)
        offered = {role.name for role in response.context["form"].fields["role"].queryset}
        self.assertNotIn("API_Importer", offered)
        self.assertNotIn("Writer", offered)
        self.assertEqual(offered, {"Reader", "Maintainer", "Owner"})
        self.assertEqual(offered, {role.name for role in get_group_member_roles()})

    def test_add_member_is_idempotent_for_existing_member(self):
        Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.reader_role)
        self.client.force_login(self.superuser)

        self.client.post(
            reverse("add_group_member", args=(self.group.id,)),
            {"group": self.group.id, "users": [self.target.id], "role": self.maintainer_role.id},
        )

        self.assertEqual(Dojo_Group_Member.objects.filter(group=self.group, user=self.target).count(), 1)

    def test_edit_member_role(self):
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.reader_role)
        # Keep an owner around so the "at least one owner" guard is not the thing under test.
        Dojo_Group_Member.objects.create(group=self.group, user=self.staff, role=self.owner_role)
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("edit_group_member", args=(member.id,)),
            {"group": self.group.id, "user": self.target.id, "role": self.maintainer_role.id},
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, self.maintainer_role)

    def test_edit_member_cannot_remove_last_owner(self):
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.owner_role)
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("edit_group_member", args=(member.id,)),
            {"group": self.group.id, "user": self.target.id, "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, self.owner_role)

    def test_delete_member_round_trip(self):
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.reader_role)
        self.assertTrue(self.group.auth_group.user_set.filter(pk=self.target.pk).exists())
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("delete_group_member", args=(member.id,)),
            {"group": self.group.id, "user": self.target.id, "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Dojo_Group_Member.objects.filter(pk=member.pk).exists())
        self.assertFalse(self.group.auth_group.user_set.filter(pk=self.target.pk).exists())

    def test_delete_member_cannot_remove_last_owner(self):
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.owner_role)
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("delete_group_member", args=(member.id,)),
            {"group": self.group.id, "user": self.target.id, "role": self.owner_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Dojo_Group_Member.objects.filter(pk=member.pk).exists())

    def test_member_routes_denied_for_non_staff(self):
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.reader_role)
        self.client.force_login(self.nobody)

        self.assertEqual(
            self.client.get(reverse("add_group_member", args=(self.group.id,))).status_code, DENIED,
        )
        self.assertEqual(
            self.client.get(reverse("edit_group_member", args=(member.id,))).status_code, DENIED,
        )

    def test_member_cannot_escalate_own_role(self):
        """
        ``_authorized_for`` lets a user act on a membership row referencing
        themselves so that self-removal works. Under the default
        ``DD_FEATURE_RBAC=off`` the URL-level "manage" check already refuses
        (manage is staff-only there); the edit view additionally re-checks
        ``Group_Manage_Members`` against the *group*, so the self-row carve-out
        cannot be turned into a self-promotion under ``on`` either.
        """
        member = Dojo_Group_Member.objects.create(group=self.group, user=self.target, role=self.reader_role)
        Dojo_Group_Member.objects.create(group=self.group, user=self.staff, role=self.owner_role)
        self.client.force_login(self.target)

        response = self.client.post(
            reverse("edit_group_member", args=(member.id,)),
            {"group": self.group.id, "user": self.target.id, "role": self.maintainer_role.id},
        )

        self.assertEqual(response.status_code, DENIED)
        member.refresh_from_db()
        self.assertEqual(member.role, self.reader_role)


class TestGetAuthorizedGroups(GroupUIBaseTestCase):

    """
    ``get_authorized_groups`` is what narrows the list page's queryset. Its
    role-membership branch is only reachable for a user who holds neither the
    ``auth.*`` configuration permissions nor staff/superuser, so it is asserted
    directly rather than through the (config-gated) list view.
    """

    def test_superuser_sees_every_group(self):
        Dojo_Group.objects.create(name="auth_q_group_a")
        Dojo_Group.objects.create(name="auth_q_group_b")

        with impersonate(self.superuser):
            self.assertEqual(get_authorized_groups("view").count(), Dojo_Group.objects.count())

    def test_config_permission_holder_sees_every_group(self):
        Dojo_Group.objects.create(name="auth_q_group_c")

        with impersonate(self.config_admin):
            self.assertEqual(get_authorized_groups("view").count(), Dojo_Group.objects.count())

    def test_plain_user_sees_only_groups_they_are_a_member_of(self):
        mine = Dojo_Group.objects.create(name="auth_q_mine")
        Dojo_Group.objects.create(name="auth_q_theirs")
        Dojo_Group_Member.objects.create(group=mine, user=self.nobody, role=self.reader_role)

        with impersonate(self.nobody):
            self.assertEqual([group.name for group in get_authorized_groups("view")], ["auth_q_mine"])

    def test_reader_membership_does_not_grant_edit(self):
        mine = Dojo_Group.objects.create(name="auth_q_reader_only")
        Dojo_Group_Member.objects.create(group=mine, user=self.nobody, role=self.reader_role)

        with impersonate(self.nobody):
            self.assertEqual(list(get_authorized_groups("edit")), [])
