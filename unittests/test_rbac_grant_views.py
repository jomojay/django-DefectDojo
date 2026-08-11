"""
End-to-end tests for the Product / Product Type RBAC grant views and panels
(INTEGRATIONS_ROADMAP.md §7.8, PR 6).

Real Django test client against real URLs, so every layer the panels depend on
is exercised: URL routing (both the v3 ``/asset/`` names and the legacy
``/product/`` ones resolve to the same URL *names*), ``AuthorizationMiddleware``
reading ``URL_PERMISSIONS``, the views' payload-level owner checks, the forms,
and the template gating on ``DD_FEATURE_RBAC``.

Two conventions inherited from the sibling suites:

* ``PermissionDenied`` is routed through ``dojo.views.custom_unauthorized_view``
  (``handler403``), which renders the 403 template with **status 400** — see
  ``unittests/test_authorized_users_ui.py`` and ``unittests/test_group_ui.py``.
* ``DD_FEATURE_RBAC``'s *ambient* default is no longer ``off`` as of PR 7
  (INTEGRATIONS_ROADMAP.md §7.6 — it ships ``shadow``), so tests never rely on
  the ambient value. Tests that need a *role* to mean anything wrap themselves
  in ``override_settings(FEATURE_RBAC="on")``; tests named ``..._under_off``
  pin ``override_settings(FEATURE_RBAC="off")`` explicitly to assert the
  dark-launch behaviour regardless of what the ambient default happens to be.
"""
from django.test import override_settings
from django.urls import reverse

from dojo.authorization.models import (
    Dojo_Group,
    Dojo_Group_Member,
    Product_Group,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
    Role,
)
from dojo.models import Dojo_User, Product, Product_Type
from unittests.dojo_test_case import DojoTestCase

DENIED = 400
RBAC_ON = override_settings(FEATURE_RBAC="on")
RBAC_OFF = override_settings(FEATURE_RBAC="off")


class RbacGrantBaseTestCase(DojoTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.reader_role = Role.objects.get(name="Reader")
        cls.writer_role = Role.objects.get(name="Writer")
        cls.maintainer_role = Role.objects.get(name="Maintainer")
        cls.owner_role = Role.objects.get(name="Owner")

        cls.pt = Product_Type.objects.create(name="rbac_grant_pt")
        cls.product = Product.objects.create(
            name="rbac_grant_product", description="x", prod_type=cls.pt,
        )
        cls.group = Dojo_Group.objects.create(name="rbac_grant_group")
        cls.other_group = Dojo_Group.objects.create(name="rbac_grant_other_group")

        cls.staff = Dojo_User.objects.create(username="rbac_grant_staff", is_staff=True)
        cls.superuser = Dojo_User.objects.create(username="rbac_grant_super", is_superuser=True)
        cls.target = Dojo_User.objects.create(username="rbac_grant_target", is_active=True)
        cls.bystander = Dojo_User.objects.create(username="rbac_grant_bystander", is_active=True)

        # Direct Product grants, one user per role, so the only variable between
        # the assertions below is which role the actor holds.
        cls.prod_reader = cls._product_member("rbac_grant_prod_reader", cls.reader_role)
        cls.prod_writer = cls._product_member("rbac_grant_prod_writer", cls.writer_role)
        cls.prod_maintainer = cls._product_member("rbac_grant_prod_maintainer", cls.maintainer_role)
        cls.prod_owner = cls._product_member("rbac_grant_prod_owner", cls.owner_role)

        # Group *visibility* is a separate axis from product permissions: the
        # add-group picker only offers groups get_authorized_groups("view")
        # returns, so an actor with no group membership at all sees an empty
        # picker no matter how much authority they hold over the product.
        Dojo_Group_Member.objects.create(
            group=cls.group, user=cls.prod_maintainer, role=cls.reader_role)

        cls.pt_reader = cls._product_type_member("rbac_grant_pt_reader", cls.reader_role)
        cls.pt_maintainer = cls._product_type_member("rbac_grant_pt_maintainer", cls.maintainer_role)
        cls.pt_owner = cls._product_type_member("rbac_grant_pt_owner", cls.owner_role)
        Dojo_Group_Member.objects.create(
            group=cls.group, user=cls.pt_owner, role=cls.reader_role)

    @classmethod
    def _product_member(cls, username, role):
        user = Dojo_User.objects.create(username=username, is_active=True)
        Product_Member.objects.create(product=cls.product, user=user, role=role)
        return user

    @classmethod
    def _product_type_member(cls, username, role):
        user = Dojo_User.objects.create(username=username, is_active=True)
        Product_Type_Member.objects.create(product_type=cls.pt, user=user, role=role)
        return user


# ---------------------------------------------------------------------------
# Product members
# ---------------------------------------------------------------------------


class TestProductMemberGrantViews(RbacGrantBaseTestCase):

    @RBAC_ON
    def test_reader_cannot_open_add_member_page(self):
        self.client.force_login(self.prod_reader)
        response = self.client.get(reverse("add_product_member", args=(self.product.id,)))
        self.assertEqual(response.status_code, DENIED)

    @RBAC_ON
    def test_reader_cannot_post_add_member(self):
        self.client.force_login(self.prod_reader)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Product_Member.objects.filter(product=self.product, user=self.target).exists())

    @RBAC_ON
    def test_writer_cannot_post_add_member(self):
        """Writer holds `add`/`edit`; handing out grants is `manage`, which it does not."""
        self.client.force_login(self.prod_writer)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Product_Member.objects.filter(product=self.product, user=self.target).exists())

    @RBAC_ON
    def test_maintainer_can_add_member(self):
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        member = Product_Member.objects.get(product=self.product, user=self.target)
        self.assertEqual(member.role, self.reader_role)

    @RBAC_ON
    def test_maintainer_cannot_grant_owner(self):
        """`Product_Member_Add_Owner` resolves to Action.Own, which only Owner holds."""
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.owner_role.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product_Member.objects.filter(product=self.product, user=self.target).exists())

    @RBAC_ON
    def test_owner_can_grant_owner(self):
        self.client.force_login(self.prod_owner)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.owner_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Product_Member.objects.get(product=self.product, user=self.target).role, self.owner_role,
        )

    @RBAC_ON
    def test_duplicate_member_row_is_not_created(self):
        """
        There is no DB uniqueness constraint until migration 0279
        (INTEGRATIONS_ROADMAP.md R15) and a duplicate row would let the higher
        role silently win, so the view guards even though the form's queryset
        already excludes current members.
        """
        self.client.force_login(self.prod_maintainer)
        self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.maintainer_role.id},
        )
        self.assertEqual(
            Product_Member.objects.filter(product=self.product, user=self.target).count(), 1,
        )

    @RBAC_ON
    def test_maintainer_can_change_role(self):
        member = Product_Member.objects.create(
            product=self.product, user=self.target, role=self.reader_role)
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("edit_product_member", args=(member.id,)), {"role": self.writer_role.id})
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, self.writer_role)

    @RBAC_ON
    def test_maintainer_cannot_promote_to_owner(self):
        member = Product_Member.objects.create(
            product=self.product, user=self.target, role=self.reader_role)
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("edit_product_member", args=(member.id,)), {"role": self.owner_role.id})
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, self.reader_role)

    @RBAC_ON
    def test_reader_cannot_self_promote(self):
        """
        ``_authorized_for`` lets a user act on a grant row referencing
        themselves, but only for Delete. A role change is Manage, checked
        against the product, so the self-row carve-out cannot be turned into a
        self-promotion.
        """
        member = Product_Member.objects.get(product=self.product, user=self.prod_reader)
        self.client.force_login(self.prod_reader)
        response = self.client.post(
            reverse("edit_product_member", args=(member.id,)), {"role": self.owner_role.id})
        self.assertEqual(response.status_code, DENIED)
        member.refresh_from_db()
        self.assertEqual(member.role, self.reader_role)

    @RBAC_ON
    def test_reader_can_remove_own_grant(self):
        member = Product_Member.objects.get(product=self.product, user=self.prod_reader)
        self.client.force_login(self.prod_reader)
        response = self.client.post(reverse("delete_product_member", args=(member.id,)))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Product_Member.objects.filter(pk=member.pk).exists())

    @RBAC_ON
    def test_reader_cannot_remove_somebody_elses_grant(self):
        member = Product_Member.objects.get(product=self.product, user=self.prod_owner)
        self.client.force_login(self.prod_reader)
        response = self.client.post(reverse("delete_product_member", args=(member.id,)))
        self.assertEqual(response.status_code, DENIED)
        self.assertTrue(Product_Member.objects.filter(pk=member.pk).exists())

    @RBAC_ON
    def test_edit_and_delete_reject_get(self):
        """
        Both are state-changing single-purpose endpoints driven from the panel
        row menu; neither has (or should have) a rendered page.
        """
        member = Product_Member.objects.create(
            product=self.product, user=self.target, role=self.reader_role)
        self.client.force_login(self.prod_owner)
        self.assertEqual(
            self.client.get(reverse("edit_product_member", args=(member.id,))).status_code, DENIED)
        self.assertEqual(
            self.client.get(reverse("delete_product_member", args=(member.id,))).status_code, DENIED)
        self.assertTrue(Product_Member.objects.filter(pk=member.pk).exists())

    def test_owner_role_grants_nothing_under_off(self):
        """
        Dark launch: with the flag ``off``, `manage` is staff-only exactly as it
        was before this epic, so a non-staff Product Owner gets no new power.
        """
        self.client.force_login(self.prod_owner)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Product_Member.objects.filter(product=self.product, user=self.target).exists())

    def test_staff_can_add_member_under_off(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("add_product_member", args=(self.product.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product_Member.objects.filter(product=self.product, user=self.target).exists())


# ---------------------------------------------------------------------------
# Product groups
# ---------------------------------------------------------------------------


class TestProductGroupGrantViews(RbacGrantBaseTestCase):

    @RBAC_ON
    def test_writer_cannot_add_group_grant(self):
        """
        Regression guard: `Permissions.Product_Group_Add` resolves to
        Action.Add through permission_to_action()'s suffix rules, which a Writer
        holds. Handing out a grant is Manage, and both the URL_PERMISSIONS entry
        and the in-view check say so.
        """
        self.client.force_login(self.prod_writer)
        response = self.client.post(
            reverse("add_product_group", args=(self.product.id,)),
            {"groups": [self.group.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Product_Group.objects.filter(product=self.product).exists())

    @RBAC_ON
    def test_maintainer_can_add_group_grant(self):
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("add_product_group", args=(self.product.id,)),
            {"groups": [self.group.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Product_Group.objects.get(product=self.product, group=self.group).role, self.reader_role,
        )

    @RBAC_ON
    def test_maintainer_cannot_grant_group_owner(self):
        self.client.force_login(self.prod_maintainer)
        response = self.client.post(
            reverse("add_product_group", args=(self.product.id,)),
            {"groups": [self.group.id], "role": self.owner_role.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product_Group.objects.filter(product=self.product, group=self.group).exists())

    @RBAC_ON
    def test_writer_cannot_change_group_role(self):
        """`Product_Group_Edit` would resolve to Action.Edit, which a Writer holds."""
        grant = Product_Group.objects.create(
            product=self.product, group=self.group, role=self.reader_role)
        self.client.force_login(self.prod_writer)
        response = self.client.post(
            reverse("edit_product_group", args=(grant.id,)), {"role": self.maintainer_role.id})
        self.assertEqual(response.status_code, DENIED)
        grant.refresh_from_db()
        self.assertEqual(grant.role, self.reader_role)

    @RBAC_ON
    def test_maintainer_can_change_and_remove_group_grant(self):
        grant = Product_Group.objects.create(
            product=self.product, group=self.group, role=self.reader_role)
        self.client.force_login(self.prod_maintainer)

        self.assertEqual(
            self.client.post(
                reverse("edit_product_group", args=(grant.id,)),
                {"role": self.maintainer_role.id}).status_code, 302)
        grant.refresh_from_db()
        self.assertEqual(grant.role, self.maintainer_role)

        self.assertEqual(
            self.client.post(reverse("delete_product_group", args=(grant.id,))).status_code, 302)
        self.assertFalse(Product_Group.objects.filter(pk=grant.pk).exists())

    @RBAC_ON
    def test_add_group_form_hides_groups_the_actor_cannot_see(self):
        """
        The picker must not leak the names of every group in the install to a
        product Maintainer who has no visibility into groups at all.
        """
        self.client.force_login(self.prod_maintainer)
        response = self.client.get(reverse("add_product_group", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        # A member of `group` and of nothing else: one offered, one invisible.
        self.assertIn(self.group.name, body)
        self.assertNotIn(self.other_group.name, body)


# ---------------------------------------------------------------------------
# Product Type members and groups
# ---------------------------------------------------------------------------


class TestProductTypeGrantViews(RbacGrantBaseTestCase):

    @RBAC_ON
    def test_reader_cannot_add_member(self):
        self.client.force_login(self.pt_reader)
        response = self.client.post(
            reverse("add_product_type_member", args=(self.pt.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt, user=self.target).exists())

    @RBAC_ON
    def test_maintainer_can_add_member(self):
        self.client.force_login(self.pt_maintainer)
        response = self.client.post(
            reverse("add_product_type_member", args=(self.pt.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt, user=self.target).exists())

    @RBAC_ON
    def test_last_owner_cannot_be_removed(self):
        owner_member = Product_Type_Member.objects.get(product_type=self.pt, user=self.pt_owner)
        self.client.force_login(self.superuser)
        response = self.client.post(reverse("delete_product_type_member", args=(owner_member.id,)))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product_Type_Member.objects.filter(pk=owner_member.pk).exists())

    @RBAC_ON
    def test_last_owner_cannot_be_demoted(self):
        """
        Regression guard for the ModelForm instance-mutation trap: a bound
        ModelForm writes cleaned_data onto `instance` during `_post_clean()`, so
        the "was this row an Owner?" question has to be answered *before*
        `is_valid()` runs or this check silently never fires.
        """
        owner_member = Product_Type_Member.objects.get(product_type=self.pt, user=self.pt_owner)
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("edit_product_type_member", args=(owner_member.id,)),
            {"role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        owner_member.refresh_from_db()
        self.assertEqual(owner_member.role, self.owner_role)

    @RBAC_ON
    def test_owner_can_be_demoted_when_another_owner_remains(self):
        owner_member = Product_Type_Member.objects.get(product_type=self.pt, user=self.pt_owner)
        Product_Type_Member.objects.create(
            product_type=self.pt, user=self.target, role=self.owner_role)
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("edit_product_type_member", args=(owner_member.id,)),
            {"role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, 302)
        owner_member.refresh_from_db()
        self.assertEqual(owner_member.role, self.reader_role)

    @RBAC_ON
    def test_maintainer_cannot_grant_owner(self):
        self.client.force_login(self.pt_maintainer)
        response = self.client.post(
            reverse("add_product_type_member", args=(self.pt.id,)),
            {"users": [self.target.id], "role": self.owner_role.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt, user=self.target).exists())

    @RBAC_ON
    def test_group_grant_add_change_remove(self):
        self.client.force_login(self.pt_owner)
        self.assertEqual(
            self.client.post(
                reverse("add_product_type_group", args=(self.pt.id,)),
                {"groups": [self.group.id], "role": self.reader_role.id}).status_code, 302)
        grant = Product_Type_Group.objects.get(product_type=self.pt, group=self.group)

        self.assertEqual(
            self.client.post(
                reverse("edit_product_type_group", args=(grant.id,)),
                {"role": self.maintainer_role.id}).status_code, 302)
        grant.refresh_from_db()
        self.assertEqual(grant.role, self.maintainer_role)

        self.assertEqual(
            self.client.post(reverse("delete_product_type_group", args=(grant.id,))).status_code, 302)
        self.assertFalse(Product_Type_Group.objects.filter(pk=grant.pk).exists())

    @RBAC_ON
    def test_reader_cannot_remove_group_grant(self):
        grant = Product_Type_Group.objects.create(
            product_type=self.pt, group=self.group, role=self.reader_role)
        self.client.force_login(self.pt_reader)
        response = self.client.post(reverse("delete_product_type_group", args=(grant.id,)))
        self.assertEqual(response.status_code, DENIED)
        self.assertTrue(Product_Type_Group.objects.filter(pk=grant.pk).exists())

    def test_owner_role_grants_nothing_under_off(self):
        self.client.force_login(self.pt_owner)
        response = self.client.post(
            reverse("add_product_type_member", args=(self.pt.id,)),
            {"users": [self.target.id], "role": self.reader_role.id},
        )
        self.assertEqual(response.status_code, DENIED)


# ---------------------------------------------------------------------------
# Panel rendering / feature-flag gating
# ---------------------------------------------------------------------------


class TestRbacPanelRendering(RbacGrantBaseTestCase):

    @RBAC_OFF
    def test_product_panels_absent_under_off(self):
        """
        PR 6 ships dark: with the flag ``off`` the detail page must be
        byte-for-byte what it is today, panels included (§7.10).
        """
        self.client.force_login(self.staff)
        response = self.client.get(reverse("view_product", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn('id="rbac-asset-members"', body)
        self.assertNotIn('id="rbac-asset-groups"', body)
        # ... while the Authorized Users panel it sits beside is untouched (§7.4).
        self.assertIn("Authorized Users", body)

    @RBAC_ON
    def test_product_panels_render_grants_when_enabled(self):
        Product_Group.objects.create(product=self.product, group=self.group, role=self.reader_role)
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("view_product", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('id="rbac-asset-members"', body)
        self.assertIn('id="rbac-asset-groups"', body)
        self.assertIn(self.prod_owner.username, body)
        self.assertIn(self.group.name, body)
        self.assertIn("Authorized Users", body)

    @RBAC_ON
    def test_product_type_panels_render_when_enabled(self):
        Product_Type_Group.objects.create(
            product_type=self.pt, group=self.group, role=self.reader_role)
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("view_product_type", args=(self.pt.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('id="rbac-org-members"', body)
        self.assertIn('id="rbac-org-groups"', body)
        self.assertIn(self.group.name, body)

    @RBAC_OFF
    def test_product_type_panels_absent_under_off(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("view_product_type", args=(self.pt.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn('id="rbac-org-members"', body)
        self.assertNotIn('id="rbac-org-groups"', body)

    @RBAC_ON
    def test_reader_sees_panels_without_action_controls(self):
        """A Reader may see who has access; the add link and row menus are Manage-only."""
        self.client.force_login(self.prod_reader)
        response = self.client.get(reverse("view_product", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('id="rbac-asset-members"', body)
        self.assertNotIn('id="addProductMember"', body)
        self.assertNotIn('id="addProductGroup"', body)

    @RBAC_ON
    def test_maintainer_sees_action_controls(self):
        self.client.force_login(self.prod_maintainer)
        response = self.client.get(reverse("view_product", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('id="addProductMember"', body)
        self.assertIn('id="addProductGroup"', body)


# ---------------------------------------------------------------------------
# Sidebar link
# ---------------------------------------------------------------------------


class TestGroupsSidebarLink(RbacGrantBaseTestCase):

    @RBAC_OFF
    def test_groups_link_hidden_under_off(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("product"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f'href="{reverse("groups")}"', response.content.decode("utf-8"))

    @RBAC_ON
    def test_groups_link_visible_when_enabled_for_config_permission_holder(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("product"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'href="{reverse("groups")}"', response.content.decode("utf-8"))

    @RBAC_ON
    def test_groups_link_hidden_without_config_permission(self):
        self.client.force_login(self.bystander)
        response = self.client.get(reverse("product"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f'href="{reverse("groups")}"', response.content.decode("utf-8"))
