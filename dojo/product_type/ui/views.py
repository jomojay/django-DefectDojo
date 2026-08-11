import logging
from functools import partial

from django.contrib import messages
from django.contrib.admin.utils import NestedObjects
from django.core.exceptions import PermissionDenied
from django.db import DEFAULT_DB_ALIAS
from django.db.models import OuterRef, Value
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _

from dojo.authorization.authorization import user_has_permission, user_has_permission_or_403
from dojo.authorization.models import Product_Type_Group, Product_Type_Member, Role
from dojo.authorization.roles_permissions import FEATURE_RBAC_OFF, Permissions, feature_rbac_state
from dojo.forms import (
    Add_Product_Type_AuthorizedUsersForm,
    Delete_Product_TypeForm,
    Product_TypeForm,
)
from dojo.labels import get_labels
from dojo.models import Dojo_User, Endpoint, Finding, Product, Product_Type
from dojo.product.queries import get_authorized_products
from dojo.product.ui.filters import ProductFilter, ProductFilterWithoutObjectLookups
from dojo.product_type.queries import (
    get_authorized_groups_for_product_type,
    get_authorized_members_for_product_type,
    get_authorized_product_types,
)
from dojo.product_type.ui.filters import ProductTypeFilter
from dojo.product_type.ui.forms import (
    Add_Product_Type_GroupForm,
    Add_Product_Type_MemberForm,
    Edit_Product_Type_GroupForm,
    Edit_Product_Type_MemberForm,
)
from dojo.query_utils import build_count_subquery
from dojo.utils import (
    add_breadcrumb,
    async_delete,
    get_page_items,
    get_setting,
    get_system_setting,
)

logger = logging.getLogger(__name__)

"""
Jay
Status: in prod
Product Type views
"""

labels = get_labels()


def product_type(request):
    prod_types = get_authorized_product_types("view")
    name_words = prod_types.values_list("name", flat=True)

    ptl = ProductTypeFilter(request.GET, queryset=prod_types)
    pts = get_page_items(request, ptl.qs, 25)

    pts.object_list = prefetch_for_product_type(pts.object_list)

    page_name = str(labels.ORG_READ_LIST_LABEL)
    add_breadcrumb(title=page_name, top_level=True, request=request)

    return render(request, "dojo/product_type.html", {
        "name": page_name,
        "pts": pts,
        "ptl": ptl,
        "name_words": name_words})


def prefetch_for_product_type(prod_types):
    # old code can arrive here with prods being a list because the query was already executed
    if not isinstance(prod_types, QuerySet):
        logger.debug("unable to prefetch because query was already executed")
        return prod_types

    prod_subquery = build_count_subquery(
        Product.objects.filter(prod_type_id=OuterRef("pk")),
        group_field="prod_type_id",
    )
    base_findings = Finding.objects.filter(test__engagement__product__prod_type_id=OuterRef("pk"))
    count_subquery = partial(build_count_subquery, group_field="test__engagement__product__prod_type_id")

    return prod_types.annotate(
        prod_count=Coalesce(prod_subquery, Value(0)),
        active_findings_count=Coalesce(count_subquery(base_findings.filter(active=True)), Value(0)),
        active_verified_findings_count=Coalesce(
            count_subquery(base_findings.filter(active=True, verified=True)), Value(0),
        ),
    )


def add_product_type(request):
    page_name = str(labels.ORG_CREATE_LABEL)
    form = Product_TypeForm()
    if request.method == "POST":
        form = Product_TypeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.add_message(request,
                                 messages.SUCCESS,
                                 str(labels.ORG_CREATE_SUCCESS_MESSAGE),
                                 extra_tags="alert-success")
            return HttpResponseRedirect(reverse("product_type"))
    add_breadcrumb(title=page_name, top_level=False, request=request)

    return render(request, "dojo/new_product_type.html", {
        "name": page_name,
        "form": form,
    })


def view_product_type(request, ptid):
    page_name = str(labels.ORG_READ_LABEL)
    pt = get_object_or_404(Product_Type, pk=ptid)
    authorized_users = pt.authorized_users.order_by("first_name", "last_name", "username")
    products = get_authorized_products("view").filter(prod_type=pt)
    filter_string_matching = get_system_setting("filter_string_matching", False)
    filter_class = ProductFilterWithoutObjectLookups if filter_string_matching else ProductFilter
    prod_filter = filter_class(request.GET, queryset=products, user=request.user)
    products = get_page_items(request, prod_filter.qs, 25)

    add_breadcrumb(title=page_name, top_level=False, request=request)
    return render(request, "dojo/view_product_type.html", {
        "name": page_name,
        "pt": pt,
        "products": products,
        "prod_filter": prod_filter,
        "authorized_users": authorized_users,
        **_rbac_panel_context(request, pt),
    })


def delete_product_type(request, ptid):
    product_type = get_object_or_404(Product_Type, pk=ptid)
    form = Delete_Product_TypeForm(instance=product_type)

    if request.method == "POST":
        if "id" in request.POST and str(product_type.id) == request.POST["id"]:
            form = Delete_Product_TypeForm(request.POST, instance=product_type)
            if form.is_valid():
                if get_setting("ASYNC_OBJECT_DELETE"):
                    async_del = async_delete()
                    async_del.delete(product_type)
                    message = labels.ORG_DELETE_SUCCESS_ASYNC_MESSAGE
                else:
                    message = labels.ORG_DELETE_SUCCESS_MESSAGE
                    with Endpoint.allow_endpoint_init():  # TODO: Delete this after the move to Locations
                        product_type.delete()
                messages.add_message(request,
                                     messages.SUCCESS,
                                     message,
                                     extra_tags="alert-success")
                return HttpResponseRedirect(reverse("product_type"))

    rels = [_("Previewing the relationships has been disabled."), ""]
    display_preview = get_setting("DELETE_PREVIEW")
    if display_preview:
        with Endpoint.allow_endpoint_init():  # TODO: Delete this after the move to Locations
            collector = NestedObjects(using=DEFAULT_DB_ALIAS)
            collector.collect([product_type])
            rels = collector.nested()

    add_breadcrumb(title=str(labels.ORG_DELETE_LABEL), top_level=False, request=request)
    return render(request, "dojo/delete_product_type.html", {
        "label_delete_with_name": labels.ORG_DELETE_WITH_NAME_LABEL % {"name": product_type},
        "form": form,
        "rels": rels,
    })


def edit_product_type(request, ptid):
    page_name = str(labels.ORG_UPDATE_LABEL)
    pt = get_object_or_404(Product_Type, pk=ptid)
    pt_form = Product_TypeForm(instance=pt)
    if request.method == "POST" and request.POST.get("edit_product_type"):
        pt_form = Product_TypeForm(request.POST, instance=pt)
        if pt_form.is_valid():
            pt = pt_form.save()
            messages.add_message(
                request,
                messages.SUCCESS,
                labels.ORG_UPDATE_SUCCESS_MESSAGE,
                extra_tags="alert-success",
            )
            return HttpResponseRedirect(reverse("product_type"))

    add_breadcrumb(title=page_name, top_level=False, request=request)
    return render(request, "dojo/edit_product_type.html", {
        "name": page_name,
        "label_edit_with_name": labels.ORG_UPDATE_WITH_NAME_LABEL % {"name": pt.name},
        "pt_form": pt_form,
        "pt": pt})


def add_product_type_authorized_users(request, ptid):
    pt = get_object_or_404(Product_Type, pk=ptid)
    user_has_permission_or_403(request.user, pt, Permissions.Product_Type_Manage_Members)
    page_name = _("Add Authorized Users")
    form = Add_Product_Type_AuthorizedUsersForm(product_type=pt)
    if request.method == "POST":
        form = Add_Product_Type_AuthorizedUsersForm(request.POST, product_type=pt)
        if form.is_valid():
            users = form.cleaned_data["users"]
            pt.authorized_users.add(*users)
            messages.add_message(
                request, messages.SUCCESS,
                _("Added %(count)d user(s) to authorized users.") % {"count": len(users)},
                extra_tags="alert-success",
            )
            return HttpResponseRedirect(reverse("view_product_type", args=(ptid,)))
    add_breadcrumb(title=page_name, top_level=False, request=request)
    return render(request, "dojo/new_product_type_authorized_users.html", {
        "name": page_name,
        "pt": pt,
        "form": form,
    })


def delete_product_type_authorized_user(request, ptid, user_id):
    pt = get_object_or_404(Product_Type, pk=ptid)
    user_has_permission_or_403(request.user, pt, Permissions.Product_Type_Manage_Members)
    if request.method != "POST":
        raise PermissionDenied
    user = get_object_or_404(Dojo_User, pk=user_id)
    pt.authorized_users.remove(user)
    messages.add_message(
        request, messages.SUCCESS,
        _("Removed %(username)s from authorized users.") % {"username": user.username},
        extra_tags="alert-success",
    )
    return HttpResponseRedirect(reverse("view_product_type", args=(ptid,)))


# ---------------------------------------------------------------------------
# Role grants on a Product Type (INTEGRATIONS_ROADMAP.md §7.8)
#
# Mirrors dojo/product/ui/views.py exactly — see the section note there for why
# only `add_*` renders a page and why the owner-grant checks are re-done in the
# view rather than left to URL_PERMISSIONS.
#
# The one rule that has no Product equivalent: a Product Type must always keep
# at least one Owner. There is no DB constraint expressing it (§7.2), so it is
# enforced here and in the REST serializers, in both the edit and delete paths.
# ---------------------------------------------------------------------------


def _rbac_panel_context(request, pt):
    """
    Context for the two RBAC panels on the product type detail page.

    Empty while ``DD_FEATURE_RBAC`` is ``off`` — the panels are dark until PR 7
    flips the flag (INTEGRATIONS_ROADMAP.md §7.6, §7.10), and the templates gate
    on the same flag independently.
    """
    if feature_rbac_state() == FEATURE_RBAC_OFF:
        return {}
    # One check for both panels: `Product_Type_Group_Add` would resolve to
    # Action.Add (a Writer holds it), which is not the bar for handing out
    # grants. Manage is.
    can_manage = user_has_permission(request.user, pt, Permissions.Product_Type_Manage_Members)
    return {
        "rbac_members": get_authorized_members_for_product_type(pt, Permissions.Product_Type_View),
        "rbac_groups": get_authorized_groups_for_product_type(pt, Permissions.Product_Type_View),
        "rbac_roles": Role.objects.all().order_by("name"),
        "rbac_can_manage_members": can_manage,
        "rbac_can_manage_groups": can_manage,
        "rbac_add_member_url": reverse("add_product_type_member", args=(pt.id,)),
        "rbac_add_group_url": reverse("add_product_type_group", args=(pt.id,)),
    }


def _rbac_grant_response(ptid):
    """Every grant mutation lands back on the product type detail page."""
    return HttpResponseRedirect(reverse("view_product_type", args=(ptid,)))


def _last_owner_blocked(request, pt, *, excluding_member_id=None):
    """
    True (and a message queued) if removing / demoting this row would leave the
    product type with no Owner at all — a state nobody short of a superuser can
    recover from.
    """
    remaining = Product_Type_Member.objects.filter(product_type=pt, role__is_owner=True)
    if excluding_member_id is not None:
        remaining = remaining.exclude(id=excluding_member_id)
    if remaining.exists():
        return False
    messages.add_message(
        request, messages.WARNING,
        labels.ORG_USERS_MINIMUM_NUMBER_WITH_NAME_MESSAGE % {"name": pt.name},
        extra_tags="alert-warning")
    return True


def add_product_type_member(request, ptid):
    pt = get_object_or_404(Product_Type, pk=ptid)
    user_has_permission_or_403(request.user, pt, Permissions.Product_Type_Manage_Members)
    page_name = str(labels.ORG_USERS_ADD_LABEL)
    memberform = Add_Product_Type_MemberForm(request.POST or None, initial={"product_type": pt.id})

    if request.method == "POST" and memberform.is_valid():
        if memberform.cleaned_data["role"].is_owner and not user_has_permission(
                request.user, pt, Permissions.Product_Type_Member_Add_Owner):
            messages.add_message(
                request, messages.WARNING,
                _("You are not permitted to add users as owners."),
                extra_tags="alert-warning")
        else:
            for user in memberform.cleaned_data.get("users", []):
                # No DB uniqueness constraint until migration 0279
                # (INTEGRATIONS_ROADMAP.md R15); a duplicate row would let the
                # higher role silently win.
                if not Product_Type_Member.objects.filter(product_type=pt, user=user).exists():
                    Product_Type_Member.objects.create(
                        product_type=pt, user=user, role=memberform.cleaned_data["role"])
            messages.add_message(
                request, messages.SUCCESS,
                labels.ORG_USERS_ADD_SUCCESS_MESSAGE,
                extra_tags="alert-success")
            return _rbac_grant_response(ptid)

    add_breadcrumb(title=page_name, top_level=False, request=request)
    return render(request, "dojo/new_rbac_grant.html", {
        "name": page_name,
        "form": memberform,
        "form_action": reverse("add_product_type_member", args=(ptid,)),
        "cancel_url": reverse("view_product_type", args=(ptid,)),
    })


def edit_product_type_member(request, memberid):
    member = get_object_or_404(Product_Type_Member, pk=memberid)
    user_has_permission_or_403(request.user, member.product_type, Permissions.Product_Type_Manage_Members)
    if request.method != "POST":
        raise PermissionDenied
    # Captured before is_valid(): a bound ModelForm writes cleaned_data back
    # onto `instance` during _post_clean(), so `member.role` is the *new* role
    # from that point on and the demotion check below would never fire.
    product_type = member.product_type
    was_owner = member.role.is_owner
    memberform = Edit_Product_Type_MemberForm(request.POST, instance=member)

    if not memberform.is_valid():
        messages.add_message(
            request, messages.WARNING,
            _("The role could not be changed: %(errors)s") % {"errors": memberform.errors.as_text()},
            extra_tags="alert-warning")
        return _rbac_grant_response(product_type.id)

    becomes_owner = memberform.cleaned_data["role"].is_owner

    if was_owner and not becomes_owner and _last_owner_blocked(
            request, product_type, excluding_member_id=member.id):
        return _rbac_grant_response(product_type.id)

    if becomes_owner and not user_has_permission(
            request.user, product_type, Permissions.Product_Type_Member_Add_Owner):
        messages.add_message(
            request, messages.WARNING,
            _("You are not permitted to make users owners."),
            extra_tags="alert-warning")
        return _rbac_grant_response(product_type.id)

    memberform.save()
    messages.add_message(
        request, messages.SUCCESS,
        labels.ORG_USERS_UPDATE_SUCCESS_MESSAGE,
        extra_tags="alert-success")
    return _rbac_grant_response(product_type.id)


def delete_product_type_member(request, memberid):
    member = get_object_or_404(Product_Type_Member, pk=memberid)
    if request.method != "POST":
        raise PermissionDenied
    # Delete also covers self-removal (see authorization._authorized_for()).
    user_has_permission_or_403(request.user, member, Permissions.Product_Type_Member_Delete)
    if member.role.is_owner and _last_owner_blocked(
            request, member.product_type, excluding_member_id=member.id):
        return _rbac_grant_response(member.product_type_id)

    product_type_id = member.product_type_id
    removed_self = member.user_id == request.user.pk
    member.delete()
    messages.add_message(
        request, messages.SUCCESS,
        labels.ORG_USERS_DELETE_SUCCESS_MESSAGE,
        extra_tags="alert-success")
    if removed_self:
        return HttpResponseRedirect(reverse("product_type"))
    return _rbac_grant_response(product_type_id)


def add_product_type_group(request, ptid):
    pt = get_object_or_404(Product_Type, pk=ptid)
    user_has_permission_or_403(request.user, pt, Permissions.Product_Type_Manage_Members)
    page_name = str(labels.ORG_GROUPS_ADD_LABEL)
    groupform = Add_Product_Type_GroupForm(request.POST or None, initial={"product_type": pt.id})

    if request.method == "POST" and groupform.is_valid():
        if groupform.cleaned_data["role"].is_owner and not user_has_permission(
                request.user, pt, Permissions.Product_Type_Group_Add_Owner):
            messages.add_message(
                request, messages.WARNING,
                _("You are not permitted to add groups as owners."),
                extra_tags="alert-warning")
        else:
            for group in groupform.cleaned_data.get("groups", []):
                if not Product_Type_Group.objects.filter(product_type=pt, group=group).exists():
                    Product_Type_Group.objects.create(
                        product_type=pt, group=group, role=groupform.cleaned_data["role"])
            messages.add_message(
                request, messages.SUCCESS,
                labels.ORG_GROUPS_ADD_SUCCESS_MESSAGE,
                extra_tags="alert-success")
            return _rbac_grant_response(ptid)

    add_breadcrumb(title=page_name, top_level=False, request=request)
    return render(request, "dojo/new_rbac_grant.html", {
        "name": page_name,
        "form": groupform,
        "form_action": reverse("add_product_type_group", args=(ptid,)),
        "cancel_url": reverse("view_product_type", args=(ptid,)),
    })


def edit_product_type_group(request, groupid):
    product_type_group = get_object_or_404(Product_Type_Group, pk=groupid)
    user_has_permission_or_403(request.user, product_type_group, Permissions.Product_Type_Manage_Members)
    if request.method != "POST":
        raise PermissionDenied
    groupform = Edit_Product_Type_GroupForm(request.POST, instance=product_type_group)

    if not groupform.is_valid():
        messages.add_message(
            request, messages.WARNING,
            _("The role could not be changed: %(errors)s") % {"errors": groupform.errors.as_text()},
            extra_tags="alert-warning")
    elif groupform.cleaned_data["role"].is_owner and not user_has_permission(
            request.user, product_type_group.product_type, Permissions.Product_Type_Group_Add_Owner):
        messages.add_message(
            request, messages.WARNING,
            _("You are not permitted to make groups owners."),
            extra_tags="alert-warning")
    else:
        groupform.save()
        messages.add_message(
            request, messages.SUCCESS,
            labels.ORG_GROUPS_UPDATE_SUCCESS_MESSAGE,
            extra_tags="alert-success")
    return _rbac_grant_response(product_type_group.product_type_id)


def delete_product_type_group(request, groupid):
    product_type_group = get_object_or_404(Product_Type_Group, pk=groupid)
    if request.method != "POST":
        raise PermissionDenied
    user_has_permission_or_403(request.user, product_type_group, Permissions.Product_Type_Group_Delete)
    product_type_id = product_type_group.product_type_id
    product_type_group.delete()
    messages.add_message(
        request, messages.SUCCESS,
        labels.ORG_GROUPS_DELETE_SUCCESS_MESSAGE,
        extra_tags="alert-success")
    return _rbac_grant_response(product_type_id)
