"""
Authorized-object queries for ``Dojo_Group`` / ``Dojo_Group_Member``.

Ported from the pre-3.0 ``dojo/group/queries.py`` (commit ``db1932c9e``) and
retargeted from the dropped ``Permissions`` enum onto this fork's ``Action``
string vocabulary (INTEGRATIONS_ROADMAP.md §7.1). The shape of every query is
unchanged; only the permission argument type and the model import paths differ.

Two access routes reach a group, and both are preserved verbatim from the
original:

  * the Django *configuration* permissions ``auth.view_group`` /
    ``auth.add_group`` — a global "can administer groups" grant that shows
    every group, and
  * membership in the group itself with a role that grants the action, i.e.
    ``Dojo_Group_Member.role in get_roles_for_permission(action)``.
"""
from crum import get_current_user
from django.db.models import Subquery

from dojo.authorization.authorization import get_roles_for_permission, user_has_configuration_permission
from dojo.authorization.models import Dojo_Group, Dojo_Group_Member, Product_Group, Product_Type_Group, Role
from dojo.request_cache import cache_for_request


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_groups(permission):
    """Groups the current user may act on with ``permission`` (an Action or action string)."""
    user = get_current_user()

    if user is None:
        return Dojo_Group.objects.none()

    if user.is_superuser:
        return Dojo_Group.objects.all().order_by("name")

    # Check for the case of the view_group config permission
    if user_has_configuration_permission(user, "auth.view_group") or user_has_configuration_permission(user, "auth.add_group"):
        return Dojo_Group.objects.all().order_by("name")

    roles = get_roles_for_permission(permission)

    # Get authorized group IDs via subquery
    authorized_roles = Dojo_Group_Member.objects.filter(
        user=user, role__in=roles,
    ).values("group_id")

    # Filter using IN with Subquery - no annotations needed
    return Dojo_Group.objects.filter(
        pk__in=Subquery(authorized_roles),
    ).order_by("name")


def get_authorized_group_members(permission):
    """Membership rows of every group the current user may act on with ``permission``."""
    user = get_current_user()

    if user is None:
        return Dojo_Group_Member.objects.none()

    if user.is_superuser:
        return Dojo_Group_Member.objects.all().order_by("id").select_related("role")

    groups = get_authorized_groups(permission)
    return Dojo_Group_Member.objects.filter(group__in=groups).order_by("id").select_related("role")


def get_authorized_group_members_for_user(user):
    """
    ``user``'s memberships, restricted to groups the *current* user may view.

    Consumed by the user detail page, which is out of scope for this module's
    own UI (INTEGRATIONS_ROADMAP.md §7.10, PR 6) — kept here because it is a
    group query, not a user query, and belongs with its siblings.
    """
    groups = get_authorized_groups("view")
    return Dojo_Group_Member.objects.filter(user=user, group__in=groups).order_by("group__name").select_related("role", "group")


def get_group_members_for_group(group):
    return Dojo_Group_Member.objects.filter(group=group).select_related("role")


def get_product_groups_for_group(group):
    return Product_Group.objects.filter(group=group).select_related("role")


def get_product_type_groups_for_group(group):
    return Product_Type_Group.objects.filter(group=group).select_related("role")


def get_group_member_roles():
    """
    Roles offerable when adding somebody to a *group*.

    ``API_Importer`` and ``Writer`` are excluded — a historical UX decision kept
    deliberately: group membership roles govern who may administer the group,
    and neither of those two adds anything over ``Reader`` in that context. It
    does not apply to direct ``Product_Member`` / ``Product_Type_Member`` role
    choices, which are a different surface entirely.
    """
    return Role.objects.exclude(name="API_Importer").exclude(name="Writer")
