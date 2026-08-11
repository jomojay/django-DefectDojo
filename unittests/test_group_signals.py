"""
``dojo/group/signals.py`` — the ``Dojo_Group`` → ``auth.Group`` mirror.

This file exists because of risk **R16** in INTEGRATIONS_ROADMAP.md §7.11: a
receiver module that is never imported from ``AppConfig.ready()`` fails
*silently*, as missing configuration permissions for group members rather than
as an error. So these tests never import ``dojo.group.signals`` themselves —
they only save models and assert on the side effects, which means they fail if
and only if the ``import dojo.group.signals`` line in ``dojo/apps.py`` is
missing or wrong.
"""
from crum import impersonate
from django.contrib.auth.models import Group

from dojo.authorization.models import Dojo_Group, Dojo_Group_Member, Role
from dojo.models import Dojo_User
from unittests.dojo_test_case import DojoTestCase


class TestGroupAuthGroupMirror(DojoTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.creator = Dojo_User.objects.create(username="grp_signal_creator")
        cls.member = Dojo_User.objects.create(username="grp_signal_member")

    def test_creating_group_creates_mirrored_auth_group(self):
        group = Dojo_Group.objects.create(name="signal_mirror_group")

        group.refresh_from_db()
        self.assertIsNotNone(group.auth_group)
        self.assertEqual(group.auth_group.name, "signal_mirror_group")
        self.assertTrue(Group.objects.filter(name="signal_mirror_group").exists())

    def test_creator_is_added_as_owner(self):
        with impersonate(self.creator):
            group = Dojo_Group.objects.create(name="signal_owner_group")

        member = Dojo_Group_Member.objects.get(group=group, user=self.creator)
        self.assertTrue(member.role.is_owner)
        # ... and mirrored into the auth group, which is the whole point of the
        # mirror: user_has_configuration_permission() falls through to
        # user.has_perm, which only sees auth.Group membership.
        group.refresh_from_db()
        self.assertTrue(group.auth_group.user_set.filter(pk=self.creator.pk).exists())

    def test_social_provider_group_skips_auto_owner(self):
        """The guard is dormant (nothing writes social_provider while Epic 1.5 is deferred) but must stay correct."""
        with impersonate(self.creator):
            group = Dojo_Group.objects.create(
                name="signal_social_group", social_provider=Dojo_Group.AZURE,
            )

        self.assertFalse(Dojo_Group_Member.objects.filter(group=group).exists())
        group.refresh_from_db()
        # The auth group itself is still mirrored - only the ownership grant is skipped.
        self.assertIsNotNone(group.auth_group)
        self.assertFalse(group.auth_group.user_set.exists())

    def test_no_current_user_skips_auto_owner(self):
        """Fixture loads / management commands have no current user and must not blow up."""
        group = Dojo_Group.objects.create(name="signal_no_user_group")

        self.assertFalse(Dojo_Group_Member.objects.filter(group=group).exists())
        self.assertIsNotNone(Dojo_Group.objects.get(pk=group.pk).auth_group)

    def test_deleting_group_deletes_mirrored_auth_group(self):
        group = Dojo_Group.objects.create(name="signal_delete_group")
        group.refresh_from_db()
        auth_group_pk = group.auth_group.pk

        group.delete()

        self.assertFalse(Group.objects.filter(pk=auth_group_pk).exists())

    def test_membership_syncs_into_auth_group_both_ways(self):
        group = Dojo_Group.objects.create(name="signal_sync_group")
        group.refresh_from_db()
        reader = Role.objects.get(name="Reader")

        membership = Dojo_Group_Member.objects.create(
            group=group, user=self.member, role=reader,
        )
        self.assertTrue(group.auth_group.user_set.filter(pk=self.member.pk).exists())

        membership.delete()
        self.assertFalse(group.auth_group.user_set.filter(pk=self.member.pk).exists())


class TestAuthGroupNameCollision(DojoTestCase):

    """
    ``Dojo_Group.name`` is unique, but ``auth.Group.name`` is a separate
    namespace shared with anything Django admin created, so the mirror has to
    resolve collisions rather than raise.
    """

    def test_collision_appends_suffix(self):
        Group.objects.create(name="collide")

        group = Dojo_Group.objects.create(name="collide")

        group.refresh_from_db()
        self.assertEqual(group.auth_group.name, "collide_1")

    def test_collision_walks_past_an_occupied_suffix(self):
        Group.objects.create(name="collide2")
        # The first Dojo_Group takes "collide2_1" ...
        first = Dojo_Group.objects.create(name="collide2")
        first.refresh_from_db()
        self.assertEqual(first.auth_group.name, "collide2_1")

        # ... so a second Dojo_Group actually *named* collide2_1 has to keep walking.
        second = Dojo_Group.objects.create(name="collide2_1")
        second.refresh_from_db()
        self.assertEqual(second.auth_group.name, "collide2_1_1")
