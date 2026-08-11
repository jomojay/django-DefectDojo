"""
``Dojo_Group`` → ``django.contrib.auth.models.Group`` mirror.

Ported unchanged from the pre-3.0 ``dojo/group/utils.py`` (commit
``db1932c9e``). This is load-bearing, not cosmetic: every ``Dojo_Group`` is
mirrored into a real Django ``auth.Group`` and membership is kept in sync into
``auth_group.user_set``. That mirror is the *only* reason
``user_has_configuration_permission()`` — which falls through to
``user.has_perm`` — can ever grant a configuration permission to a group's
members (INTEGRATIONS_ROADMAP.md §7.5).

Registered from ``dojo/apps.py``'s ``ready()``. A receiver module that is never
imported silently does not fire, which is exactly risk **R16** in the roadmap's
register; ``unittests/test_group_signals.py`` asserts the wiring end to end
rather than trusting the import line.
"""
from crum import get_current_user
from django.contrib.auth.models import Group
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from dojo.authorization.models import Dojo_Group, Dojo_Group_Member, Role


def get_auth_group_name(group, attempt=0):
    """
    First free ``auth.Group`` name for ``group``: its own name, else
    ``<name>_1``, ``<name>_2``, ... ``Dojo_Group.name`` is unique but
    ``auth.Group.name`` is a separate namespace shared with any group Django
    admin created by hand, so a collision is possible and must not raise.
    """
    if attempt > 999:
        msg = f"Cannot find name for authorization group for Dojo_Group {group.name}, aborted after 999 attempts."
        raise Exception(msg)
    auth_group_name = group.name if attempt == 0 else group.name + "_" + str(attempt)

    try:
        # Attempt to fetch an existing group before moving forward with the real operation
        _ = Group.objects.get(name=auth_group_name)
        return get_auth_group_name(group, attempt + 1)
    except Group.DoesNotExist:
        return auth_group_name


@receiver(post_save, sender=Dojo_Group)
def group_post_save_handler(sender, **kwargs):
    created = kwargs.pop("created")
    group = kwargs.pop("instance")
    if created:
        # Create authentication group
        auth_group = Group(name=get_auth_group_name(group))
        auth_group.save()
        group.auth_group = auth_group
        group.save()
        user = get_current_user()
        if user and not group.social_provider:
            # Add the current user as the owner of the group. Skipped for
            # socially-provisioned groups: those are owned by the identity
            # provider, not by whoever happened to trigger the sync (dormant
            # today - nothing writes social_provider while Epic 1.5 is
            # deferred - but the guard is already correct, so it stays).
            member = Dojo_Group_Member()
            member.user = user
            member.group = group
            member.role = Role.objects.get(is_owner=True)
            member.save()
            # Add user to authentication group as well
            auth_group.user_set.add(user)


@receiver(post_delete, sender=Dojo_Group)
def group_post_delete_handler(sender, **kwargs):
    group = kwargs.pop("instance")
    # Authorization group doesn't get deleted automatically
    if group.auth_group:
        group.auth_group.delete()


@receiver(post_save, sender=Dojo_Group_Member)
def group_member_post_save_handler(sender, **kwargs):
    created = kwargs.pop("created")
    group_member = kwargs.pop("instance")
    if created:
        # Add user to authentication group as well
        if group_member.group.auth_group:
            group_member.group.auth_group.user_set.add(group_member.user)


@receiver(post_delete, sender=Dojo_Group_Member)
def group_member_post_delete_handler(sender, **kwargs):
    group_member = kwargs.pop("instance")
    # Remove user from the authentication group as well
    if group_member.group.auth_group:
        group_member.group.auth_group.user_set.remove(group_member.user)
