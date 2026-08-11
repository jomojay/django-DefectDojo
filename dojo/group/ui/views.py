"""
Group CRUD and group-membership management (INTEGRATIONS_ROADMAP.md §7.5).

Ported from the pre-3.0 ``dojo/group/views.py`` (commit ``db1932c9e``) and
reshaped to current conventions:

* Plain function views, matching every other reorganized module in this tree
  (``dojo/product_type/ui/views.py`` is the reference), instead of the old
  class-based views whose only purpose was Pro subclassing.
* Authorization is declared centrally in
  ``dojo.authorization.url_permissions.URL_PERMISSIONS`` and applied by
  ``dojo.authorization.middleware.AuthorizationMiddleware``, rather than by
  eleven new ``@user_is_authorized`` decorator call sites. The extra in-view
  checks below are the ones the central table structurally cannot express:
  they concern the *payload* (which role is being granted), not the URL.
* ``GlobalRoleForm`` / ``ConfigurationPermissionsForm`` and the
  add-product(-type)-group-from-the-group-page flows are deliberately not
  ported. Global roles are the privilege-escalation surface of the whole
  system and get their own superuser-only surface with the REST API (§7.7);
  product/product-type grants are created and edited from those objects' own
  detail pages (``dojo/product/ui/views.py``, ``dojo/product_type/ui/views.py``
  — §7.8). This module's detail page lists them read-only, so exactly one
  surface owns a grant.

There is no ``services.py``: everything here is form-save / form-delete plus
authorization, which AGENTS.md Phase 2 explicitly says not to wrap in a
service. ``get_auth_group_name()`` lives in ``dojo/group/signals.py`` next to
its only caller.

The sidebar's ``{% block groups_submenu_link %}`` in ``base.html`` (both
template trees) links here, but only while ``DD_FEATURE_RBAC`` is not ``off`` —
so the module still ships dark and is reachable by URL only until PR 7 flips
the flag (§7.6, §7.10).
"""
import logging

from django.contrib import messages
from django.contrib.admin.utils import NestedObjects
from django.db import DEFAULT_DB_ALIAS
from django.db.models.deletion import RestrictedError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _

from dojo.authorization.authorization import user_has_permission, user_has_permission_or_403
from dojo.authorization.models import Dojo_Group, Dojo_Group_Member
from dojo.authorization.roles_permissions import Permissions
from dojo.group.queries import (
    get_authorized_groups,
    get_group_members_for_group,
    get_product_groups_for_group,
    get_product_type_groups_for_group,
)
from dojo.group.ui.filters import GroupFilter
from dojo.group.ui.forms import (
    Add_Group_MemberForm,
    Delete_Group_MemberForm,
    DeleteGroupForm,
    DojoGroupForm,
    Edit_Group_MemberForm,
)
from dojo.utils import add_breadcrumb, get_page_items, get_setting, redirect_to_return_url_or_else

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------


def groups(request):
    """
    List every group the user is authorized to see.

    Reachable only with the ``auth.view_group`` configuration permission (the
    URL_PERMISSIONS gate, matching the pre-3.0 behavior), and the queryset is
    *additionally* narrowed by ``get_authorized_groups`` so the two layers can
    never disagree about what a given user may see.
    """
    group_list = get_authorized_groups("view")
    filtered_groups = GroupFilter(request.GET, queryset=group_list)

    add_breadcrumb(title=_("All Groups"), top_level=True, request=request)

    return render(request, "dojo/dojo_groups.html", {
        "name": _("All Groups"),
        "filtered": filtered_groups,
        "groups": get_page_items(request, filtered_groups.qs, 25),
    })


def view_group(request, group_id):
    """
    Group detail: description, members and their roles, and a read-only list of
    the Products / Product Types this group has been granted a role on. Those
    grants are created from the Product / Product Type detail pages' RBAC
    panels (§7.8), not from here, so there is deliberately no "add" action on
    this page.
    """
    group = get_object_or_404(Dojo_Group, id=group_id)

    add_breadcrumb(title=_("View Group"), top_level=False, request=request)

    return render(request, "dojo/view_dojo_group.html", {
        "group": group,
        "group_members": get_group_members_for_group(group),
        "products": get_product_groups_for_group(group),
        "product_types": get_product_type_groups_for_group(group),
    })


def add_group(request):
    """
    Create a group.

    The creator is added as its Owner, and the mirrored ``auth.Group`` is
    created, by ``dojo/group/signals.py`` — not here. That is deliberate: the
    same thing has to happen for a group created through the REST API (PR 5),
    the Django admin, or a fixture load.
    """
    form = DojoGroupForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        group = form.save()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Group was added successfully."),
            extra_tags="alert-success")
        return redirect_to_return_url_or_else(request, reverse("view_group", args=(group.id,)))

    if request.method == "POST":
        messages.add_message(
            request,
            messages.ERROR,
            _("Group was not added successfully."),
            extra_tags="alert-danger")

    add_breadcrumb(title=_("Add Group"), top_level=False, request=request)

    return render(request, "dojo/dojo_group_form.html", {
        "name": _("Add Group"),
        "form": form,
        "form_action": reverse("add_group"),
    })


def edit_group(request, group_id):
    group = get_object_or_404(Dojo_Group, id=group_id)
    form = DojoGroupForm(request.POST or None, instance=group)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Group saved successfully."),
            extra_tags="alert-success")
        return redirect_to_return_url_or_else(request, reverse("view_group", args=(group.id,)))

    if request.method == "POST":
        messages.add_message(
            request,
            messages.ERROR,
            _("Group was not saved successfully."),
            extra_tags="alert-danger")

    add_breadcrumb(title=_("Edit Group"), top_level=False, request=request)

    return render(request, "dojo/dojo_group_form.html", {
        "name": _("Edit Group"),
        "form": form,
        "group": group,
        "form_action": reverse("edit_group", args=(group.id,)),
    })


def delete_group(request, group_id):
    group = get_object_or_404(Dojo_Group, id=group_id)
    form = DeleteGroupForm(request.POST or None, instance=group)

    if request.method == "POST" and form.is_valid():
        try:
            group.delete()
        except RestrictedError as err:
            messages.add_message(
                request,
                messages.WARNING,
                _("Group cannot be deleted: %(error)s") % {"error": err},
                extra_tags="alert-warning")
        else:
            messages.add_message(
                request,
                messages.SUCCESS,
                _("Group and relationships successfully removed."),
                extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("groups"))

    # Previewing what cascades is expensive on large graphs, so it honours the
    # same DELETE_PREVIEW setting every other delete page in this codebase does.
    rels = [_("Previewing the relationships has been disabled."), ""]
    if get_setting("DELETE_PREVIEW"):
        collector = NestedObjects(using=DEFAULT_DB_ALIAS)
        collector.collect([group])
        rels = collector.nested()

    add_breadcrumb(title=_("Delete Group"), top_level=False, request=request)

    return render(request, "dojo/delete_dojo_group.html", {
        "form": form,
        "to_delete": group,
        "rels": rels,
    })


# ---------------------------------------------------------------------------
# Group membership
# ---------------------------------------------------------------------------


def add_group_member(request, gid):
    group = get_object_or_404(Dojo_Group, id=gid)
    groupform = Add_Group_MemberForm(request.POST or None, initial={"group": group.id})

    if request.method == "POST" and groupform.is_valid():
        # Granting Owner is a privilege escalation and is checked against the
        # group, not against the member row being created — the URL-level check
        # cannot see which role the payload asks for.
        if groupform.cleaned_data["role"].is_owner and not user_has_permission(request.user, group, Permissions.Group_Add_Owner):
            messages.add_message(
                request,
                messages.WARNING,
                _("You are not permitted to add users as owners."),
                extra_tags="alert-warning")
        else:
            for user in groupform.cleaned_data.get("users", []):
                # Belt and braces: the form already excludes current members,
                # but there is no DB uniqueness constraint until migration 0279
                # (INTEGRATIONS_ROADMAP.md R15) and a duplicate row would let
                # the higher role silently win.
                if not Dojo_Group_Member.objects.filter(group=group, user=user).exists():
                    Dojo_Group_Member.objects.create(
                        group=group,
                        user=user,
                        role=groupform.cleaned_data["role"],
                    )
            messages.add_message(
                request,
                messages.SUCCESS,
                _("Group members added successfully."),
                extra_tags="alert-success")
            return HttpResponseRedirect(reverse("view_group", args=(gid,)))

    add_breadcrumb(title=_("Add Group Member"), top_level=False, request=request)

    return render(request, "dojo/new_dojo_group_member.html", {
        "group": group,
        "form": groupform,
    })


def edit_group_member(request, mid):
    member = get_object_or_404(Dojo_Group_Member, pk=mid)
    # URL_PERMISSIONS checks "manage" on the *member row*, and
    # authorization._authorized_for() lets a user act on a row that references
    # themselves (so that self-removal works). Changing your own role is not
    # self-removal, so a role edit is additionally checked against the group.
    user_has_permission_or_403(request.user, member.group, Permissions.Group_Manage_Members)
    memberform = Edit_Group_MemberForm(request.POST or None, instance=member)

    if request.method == "POST" and memberform.is_valid():
        if not member.role.is_owner:
            # A group with no owner can never be administered again by anybody
            # short of a superuser. There is no DB constraint expressing this.
            owners = Dojo_Group_Member.objects.filter(
                group=member.group, role__is_owner=True,
            ).exclude(id=member.id).count()
            if owners < 1:
                messages.add_message(
                    request,
                    messages.WARNING,
                    _("There must be at least one owner for group %(group)s.") % {"group": member.group.name},
                    extra_tags="alert-warning")
                return HttpResponseRedirect(reverse("view_group", args=(member.group.id,)))
        if member.role.is_owner and not user_has_permission(request.user, member.group, Permissions.Group_Add_Owner):
            messages.add_message(
                request,
                messages.WARNING,
                _("You are not permitted to make users owners."),
                extra_tags="alert-warning")
        else:
            memberform.save()
            messages.add_message(
                request,
                messages.SUCCESS,
                _("Group member updated successfully."),
                extra_tags="alert-success")
            return HttpResponseRedirect(reverse("view_group", args=(member.group.id,)))

    add_breadcrumb(title=_("Edit a Group Member"), top_level=False, request=request)

    return render(request, "dojo/edit_dojo_group_member.html", {
        "memberid": mid,
        "member": member,
        "form": memberform,
    })


def delete_group_member(request, mid):
    member = get_object_or_404(Dojo_Group_Member, pk=mid)
    memberform = Delete_Group_MemberForm(request.POST or None, instance=member)

    if request.method == "POST" and memberform.is_valid():
        member = memberform.instance
        if member.role.is_owner:
            owners = Dojo_Group_Member.objects.filter(group=member.group, role__is_owner=True).count()
            if owners <= 1:
                messages.add_message(
                    request,
                    messages.WARNING,
                    _("There must be at least one owner for group %(group)s.") % {"group": member.group.name},
                    extra_tags="alert-warning")
                return HttpResponseRedirect(reverse("view_group", args=(member.group.id,)))

        user = member.user
        group_id = member.group.id
        member.delete()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Group member deleted successfully."),
            extra_tags="alert-success")
        # A user who just removed themselves has no business being redirected to
        # a group detail page they may no longer be able to open.
        if user == request.user:
            return HttpResponseRedirect(reverse("groups"))
        return HttpResponseRedirect(reverse("view_group", args=(group_id,)))

    add_breadcrumb(title=_("Delete a Group Member"), top_level=False, request=request)

    return render(request, "dojo/delete_dojo_group_member.html", {
        "memberid": mid,
        "member": member,
        "form": memberform,
    })
