"""
Regression tests for the prefetch RBAC gate.

The ``?prefetch=`` query parameter on viewsets that inherit
``PrefetchDojoModelViewSet`` used to bypass the authorization of the
related viewset entirely (see security report sub-vectors 4a/4b/4c/4e).
These tests pin the corrected behaviour: a non-superuser making the same
request must not see related objects whose top-level viewset is
superuser-only, while a superuser still receives the same payload as
before.
"""

from django.contrib.auth.models import Permission
from django.contrib.auth.models import User as DjangoUser
from django.test import SimpleTestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from dojo.api_v2.prefetch import authorized_querysets, prefetcher
from dojo.api_v2.prefetch.utils import get_prefetchable_fields
from dojo.asset.api.serializers import AssetSerializer
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
from dojo.models import (
    Dojo_User,
    Engagement,
    Finding,
    Notes,
    Product,
    Test,
    Test_Type,
    Tool_Configuration,
    Tool_Product_Settings,
    Tool_Type,
)
from dojo.organization.api.serializers import OrganizationSerializer
from dojo.product.api.serializer import ProductSerializer
from dojo.product_type.api.serializer import ProductTypeSerializer
from unittests.dojo_test_case import DojoAPITestCase, versioned_fixtures


@versioned_fixtures
class PrefetchRBACTest(DojoAPITestCase):

    """Verify that the prefetch path enforces authorization on related objects."""

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        # A regular (non-superuser) user with Owner role on product 1 -- the
        # bypass under test would have allowed this account to enumerate
        # users, tool configurations, and notes despite the superuser-only
        # guard on those viewsets.
        self.reader = Dojo_User.objects.get(username="user2")
        self.reader.is_superuser = False
        self.reader.is_staff = False
        self.reader.save()
        self.reader_token, _ = Token.objects.get_or_create(user=self.reader)

        self.admin = Dojo_User.objects.get(username="admin")
        self.admin_token, _ = Token.objects.get_or_create(user=self.admin)

        self.product = Product.objects.get(pk=1)
        # OSS authorization keys off the legacy ``authorized_users`` M2M
        # (Pro replaces this with Product_Member through the auth-filter
        # plugin -- see dojo.authorization.query_registrations).
        self.product.authorized_users.add(self.reader)
        Product_Member.objects.get_or_create(
            product=self.product,
            user=self.reader,
            defaults={"role_id": 4},
        )

        engagement = Engagement.objects.filter(product=self.product).first()
        if engagement is None:
            engagement = Engagement.objects.create(
                product=self.product,
                name="prefetch-rbac-eng",
                target_start="2026-01-01",
                target_end="2026-01-02",
            )

        test_type, _ = Test_Type.objects.get_or_create(name="prefetch-rbac-tt")
        test = Test.objects.filter(engagement=engagement).first()
        if test is None:
            test = Test.objects.create(
                engagement=engagement,
                test_type=test_type,
                target_start="2026-01-01",
                target_end="2026-01-02",
                lead=self.admin,
            )

        self.finding = Finding.objects.filter(test=test).first()
        if self.finding is None:
            self.finding = Finding.objects.create(
                title="prefetch-rbac-finding",
                test=test,
                reporter=self.admin,
                severity="Info",
                numerical_severity="S4",
            )

        # A private note attached to the finding. The leak in sub-vector 4e
        # is most acute for these.
        self.private_note = Notes.objects.create(
            entry="INTERNAL: prefetch-rbac private note",
            author=self.admin,
            private=True,
        )
        self.finding.notes.add(self.private_note)

        # A Tool_Configuration linked to the product through Tool_Product_Settings
        # is the exact shape exploited in sub-vector 4b.
        tool_type, _ = Tool_Type.objects.get_or_create(name="prefetch-rbac-tt")
        self.tool_config = Tool_Configuration.objects.create(
            name="Internal-Tool-prefetch-rbac",
            url="https://internal.example.invalid",
            username="svc-account-prefetch-rbac",
            authentication_type="API",
            api_key="should-not-leak",
            tool_type=tool_type,
        )
        self.tool_product_settings = Tool_Product_Settings.objects.create(
            name="prefetch-rbac-tps",
            product=self.product,
            tool_configuration=self.tool_config,
            url="https://internal.example.invalid",
        )

    # ---- 4a: user enumeration via Finding.reporter -----------------------

    def _client(self, token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    def test_admin_can_prefetch_reporter(self):
        """Superuser baseline -- prefetched reporter is still returned."""
        resp = self._client(self.admin_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=reporter",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn("reporter", prefetch)
        self.assertIn(str(self.admin.pk), prefetch["reporter"])

    def test_reader_cannot_prefetch_reporter(self):
        """Sub-vector 4a -- a non-superuser must not receive user data via prefetch."""
        resp = self._client(self.reader_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=reporter",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        # Either the key is absent or it is present but empty -- in both
        # cases no user data has been disclosed.
        self.assertFalse(prefetch.get("reporter"))

    def test_user_with_view_perm_can_prefetch_reporter(self):
        """
        ``django_view_perm`` lets a non-superuser with an explicit
        ``dojo.view_dojo_user`` grant prefetch reporter -- matching what
        ``UsersViewSet`` (gated by DjangoModelPermissions) already allows
        them to do via the top-level endpoint.
        """
        view_user = Permission.objects.get(
            content_type__app_label="dojo",
            codename="view_dojo_user",
        )
        self.reader.user_permissions.add(view_user)
        # has_perm caches per instance -- reload to pick up the new perm.
        self.reader = Dojo_User.objects.get(pk=self.reader.pk)
        self.reader_token, _ = Token.objects.get_or_create(user=self.reader)

        resp = self._client(self.reader_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=reporter",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn("reporter", prefetch)
        self.assertIn(str(self.admin.pk), prefetch["reporter"])

    # ---- 4b: tool configuration disclosure -------------------------------

    def test_admin_can_prefetch_tool_configuration(self):
        resp = self._client(self.admin_token).get(
            f"/api/v2/tool_product_settings/{self.tool_product_settings.pk}/?prefetch=tool_configuration",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn("tool_configuration", prefetch)
        self.assertIn(str(self.tool_config.pk), prefetch["tool_configuration"])

    def test_reader_cannot_prefetch_tool_configuration(self):
        """
        Sub-vector 4b -- prefetching tool_configuration must not leak the
        URL, service-account username, or extras field to a non-superuser.
        """
        resp = self._client(self.reader_token).get(
            f"/api/v2/tool_product_settings/{self.tool_product_settings.pk}/?prefetch=tool_configuration",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        leaked = prefetch.get("tool_configuration", {})
        self.assertFalse(
            leaked,
            f"tool_configuration disclosed via prefetch to non-superuser: {leaked!r}",
        )

    # ---- 4e: private notes disclosure ------------------------------------

    def test_admin_can_prefetch_notes(self):
        resp = self._client(self.admin_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=notes",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn("notes", prefetch)
        self.assertIn(str(self.private_note.pk), prefetch["notes"])

    def test_reader_cannot_prefetch_private_note_from_other_author(self):
        """
        Sub-vector 4e -- a private note written by someone else must not be
        returned to a non-superuser via prefetch (matches the existing UI
        behaviour where ``notes.filter(private=False)`` hides them).
        """
        resp = self._client(self.reader_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=notes",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        leaked = prefetch.get("notes", {})
        self.assertNotIn(str(self.private_note.pk), leaked)
        for note in leaked.values():
            self.assertNotIn(
                "INTERNAL: prefetch-rbac private note",
                note.get("entry", ""),
            )

    def test_reader_can_prefetch_public_notes(self):
        """
        ``notes_policy`` lets a non-superuser see non-private notes on
        findings they have parent-product access to.
        """
        public_note = Notes.objects.create(
            entry="public note visible to readers",
            author=self.admin,
            private=False,
        )
        self.finding.notes.add(public_note)

        resp = self._client(self.reader_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=notes",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn(str(public_note.pk), prefetch.get("notes", {}))
        # The private note authored by admin must still be hidden.
        self.assertNotIn(str(self.private_note.pk), prefetch.get("notes", {}))

    def test_reader_can_prefetch_own_private_notes(self):
        """
        ``notes_policy`` lets a non-superuser see their own private notes
        even on findings where they're not the author of every note.
        """
        own_private = Notes.objects.create(
            entry="reader's own private note",
            author=self.reader,
            private=True,
        )
        self.finding.notes.add(own_private)

        resp = self._client(self.reader_token).get(
            f"/api/v2/findings/{self.finding.pk}/?prefetch=notes",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetch = resp.json().get("prefetch", {})
        self.assertIn(str(own_private.pk), prefetch.get("notes", {}))
        # admin's private note must still be hidden.
        self.assertNotIn(str(self.private_note.pk), prefetch.get("notes", {}))

    # ---- RBAC grant rows are prefetchable, and still authorized ----------

    def test_admin_can_prefetch_authorization_groups_on_product_detail(self):
        """
        End-to-end proof of the two-gate wiring: ``authorization_groups`` is a
        real relation on ``Product`` that used to be dropped from every
        ``?prefetch=`` response because ``Dojo_Group`` had neither a serializer
        visible to ``_Prefetcher`` nor a registered policy. Both now exist, so
        the field has to come back with actual group data.
        """
        product_group = Product_Group.objects.filter(product=self.product).first()
        self.assertIsNotNone(product_group, "fixture must grant a group on product 1")

        resp = self._client(self.admin_token).get(
            f"/api/v2/products/{self.product.pk}/?prefetch=authorization_groups",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        body = resp.json()
        self.assertIn(product_group.group_id, body["authorization_groups"])
        prefetched = body["prefetch"]["authorization_groups"]
        self.assertIn(str(product_group.group_id), prefetched)
        # ...and it is the group *rendered*, not just its id echoed back.
        self.assertEqual(
            product_group.group.name,
            prefetched[str(product_group.group_id)]["name"],
        )

    def test_admin_can_prefetch_authorization_groups_on_product_list(self):
        product_group = Product_Group.objects.filter(product=self.product).first()
        resp = self._client(self.admin_token).get(
            "/api/v2/products/?prefetch=authorization_groups",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetched = resp.json()["prefetch"]["authorization_groups"]
        self.assertIn(str(product_group.group_id), prefetched)

    def test_admin_can_prefetch_authorization_groups_on_product_type(self):
        product_type_group = Product_Type_Group.objects.first()
        self.assertIsNotNone(product_type_group)
        for path in ("product_types", "organizations"):
            with self.subTest(path=path):
                resp = self._client(self.admin_token).get(
                    f"/api/v2/{path}/{product_type_group.product_type_id}/"
                    f"?prefetch=authorization_groups",
                )
                self.assertEqual(200, resp.status_code, resp.content[:500])
                prefetched = resp.json()["prefetch"]["authorization_groups"]
                self.assertIn(str(product_type_group.group_id), prefetched)

    def test_admin_can_prefetch_authorization_groups_on_asset(self):
        product_group = Product_Group.objects.filter(product=self.product).first()
        resp = self._client(self.admin_token).get(
            f"/api/v2/assets/{self.product.pk}/?prefetch=authorization_groups",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetched = resp.json()["prefetch"]["authorization_groups"]
        self.assertIn(str(product_group.group_id), prefetched)

    def test_reader_cannot_prefetch_authorization_groups(self):
        """
        Gate 2 is an authorization gate, not just a plumbing one. The reader
        can see product 1 (authorized_users) but is in no ``Dojo_Group`` and
        holds neither ``auth.view_group`` nor ``auth.add_group``, so
        ``get_authorized_groups()`` returns nothing and no group identity —
        which is to say, no other users' membership — is disclosed.
        """
        product_group = Product_Group.objects.filter(product=self.product).first()
        self.assertFalse(
            Dojo_Group_Member.objects.filter(user=self.reader).exists(),
        )

        resp = self._client(self.reader_token).get(
            f"/api/v2/products/{self.product.pk}/?prefetch=authorization_groups",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetched = resp.json().get("prefetch", {}).get("authorization_groups", {})
        self.assertNotIn(str(product_group.group_id), prefetched)

    def test_reader_with_view_group_perm_can_prefetch_authorization_groups(self):
        """The positive control for the test above: grant ``auth.view_group`` and the data appears."""
        view_group = Permission.objects.get(
            content_type__app_label="auth", codename="view_group",
        )
        self.reader.user_permissions.add(view_group)
        self.reader = Dojo_User.objects.get(pk=self.reader.pk)
        self.reader_token, _ = Token.objects.get_or_create(user=self.reader)

        product_group = Product_Group.objects.filter(product=self.product).first()
        resp = self._client(self.reader_token).get(
            f"/api/v2/products/{self.product.pk}/?prefetch=authorization_groups",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetched = resp.json()["prefetch"]["authorization_groups"]
        self.assertIn(str(product_group.group_id), prefetched)

    def test_members_still_prefetches(self):
        """Regression guard: adding the grant-row serializers must not disturb the relation that already worked."""
        resp = self._client(self.admin_token).get(
            f"/api/v2/products/{self.product.pk}/?prefetch=members",
        )
        self.assertEqual(200, resp.status_code, resp.content[:500])
        prefetched = resp.json()["prefetch"]["members"]
        self.assertIn(str(self.reader.pk), prefetched)

    # ---- defense in depth: unregistered models are denied ----------------

    def test_unregistered_model_is_denied_by_default(self):
        """
        An attempt to prefetch a field whose related model has no
        registered policy must return an empty prefetch payload, not the
        unfiltered serialized object.
        """
        # Pretend Dojo_User has no registered policy. The deny-by-default
        # path must kick in and the field must not appear in the response.
        original_dojo_user = authorized_querysets._REGISTRY.pop(Dojo_User, None)
        original_user = authorized_querysets._REGISTRY.pop(DjangoUser, None)
        try:
            resp = self._client(self.admin_token).get(
                f"/api/v2/findings/{self.finding.pk}/?prefetch=reporter",
            )
            self.assertEqual(200, resp.status_code)
            prefetch = resp.json().get("prefetch", {})
            self.assertFalse(prefetch.get("reporter"))
        finally:
            if original_dojo_user is not None:
                authorized_querysets._REGISTRY[Dojo_User] = original_dojo_user
            if original_user is not None:
                authorized_querysets._REGISTRY[DjangoUser] = original_user


class PrefetchableFieldDetectionTest(SimpleTestCase):

    """
    ``get_prefetchable_fields`` must only advertise relations that
    ``_Prefetcher`` can actually serve.

    Reactivating the RBAC models gave ``Product``/``Product_Type`` real
    ``members`` and ``authorization_groups`` relations. Both far-side models now
    have a serializer registered for the prefetch path (``Dojo_User`` all along;
    ``Dojo_Group`` since the group REST API landed), so both relations must be
    advertised *and* servable.
    """

    serializers = (
        ProductSerializer,
        AssetSerializer,
        ProductTypeSerializer,
        OrganizationSerializer,
    )

    def test_members_is_prefetchable(self):
        for serializer in self.serializers:
            with self.subTest(serializer=serializer.__name__):
                fields = [name for name, _ in get_prefetchable_fields(serializer)]
                self.assertIn("members", fields)

    def test_authorization_groups_is_prefetchable(self):
        """
        Gate 1 of the two-gate prefetch wiring: a serializer for the far-side
        model has to be a *member of* ``dojo.api_v2.serializers`` for
        ``_Prefetcher`` to find it (it builds its map with ``inspect.getmembers``
        over that module). Before ``DojoGroupSerializer`` was re-exported there,
        this relation existed on the model but was filtered out of the
        advertised set entirely.
        """
        for serializer in self.serializers:
            with self.subTest(serializer=serializer.__name__):
                # Guard the premise: the relation exists on the model...
                self.assertTrue(
                    hasattr(serializer.Meta.model, "authorization_groups"),
                )
                # ...and Dojo_Group now has a serializer to render it.
                self.assertIsNotNone(
                    prefetcher._Prefetcher()._find_serializer(Dojo_Group),
                )
                fields = [name for name, _ in get_prefetchable_fields(serializer)]
                self.assertIn("authorization_groups", fields)

    def test_rbac_models_have_a_prefetch_policy(self):
        """
        Gate 2, and the one that fails silently: ``get_prefetchable_fields``
        filters on serializer existence only, so a model with a serializer but
        no policy in ``dojo/api_v2/prefetch/registrations.py`` is advertised in
        the OpenAPI prefetch enum and then dropped from every response by
        ``_Prefetcher``'s deny-by-default.
        """
        for model in (
            Dojo_Group,
            Dojo_Group_Member,
            Role,
            Global_Role,
            Product_Member,
            Product_Group,
            Product_Type_Member,
            Product_Type_Group,
        ):
            with self.subTest(model=model.__name__):
                self.assertIn(model, authorized_querysets._REGISTRY)

    def test_every_advertised_field_is_serviceable(self):
        """The general contract, independent of any single field."""
        finder = prefetcher._Prefetcher()
        for serializer in self.serializers:
            with self.subTest(serializer=serializer.__name__):
                for name, model in get_prefetchable_fields(serializer):
                    self.assertIsNotNone(
                        finder._find_serializer(model),
                        f"{serializer.__name__}.{name} is advertised as "
                        f"prefetchable but {model.__name__} has no serializer",
                    )

    def test_rbac_relations_stay_read_only_in_the_api(self):
        """
        Membership writes are out of scope until the group management
        endpoints land; DRF makes ``through`` relations read-only, and this
        pins that so the reactivated models cannot be written through the
        product/asset endpoints by accident.
        """
        for serializer in self.serializers:
            with self.subTest(serializer=serializer.__name__):
                fields = serializer().fields
                for name in ("members", "authorization_groups"):
                    self.assertTrue(fields[name].read_only)
