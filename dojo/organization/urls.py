from django.conf import settings
from django.urls import re_path

from dojo.product.ui import views as product_views
from dojo.product_type.ui import views
from dojo.utils import redirect_view

# TODO: remove the else: branch once v3 migration is complete
if settings.ENABLE_V3_ORGANIZATION_ASSET_RELABEL:
    urlpatterns = [
        re_path(
            r"^organization$",
            views.product_type,
            name="product_type",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)$",
            views.view_product_type,
            name="view_product_type",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/edit$",
            views.edit_product_type,
            name="edit_product_type",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/delete$",
            views.delete_product_type,
            name="delete_product_type",
        ),
        re_path(
            r"^organization/add$",
            views.add_product_type,
            name="add_product_type",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/add_asset",
            product_views.new_product,
            name="add_product_to_product_type",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/authorized_users/add$",
            views.add_product_type_authorized_users,
            name="add_product_type_authorized_users",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/authorized_users/(?P<user_id>\d+)/delete$",
            views.delete_product_type_authorized_user,
            name="delete_product_type_authorized_user",
        ),
        # RBAC role grants (INTEGRATIONS_ROADMAP.md §7.8). These sit alongside
        # the authorized_users routes above rather than replacing them (§7.4).
        # No backwards-compatibility aliases: the routes are new, and
        # redirect_view() issues a 302 GET that would drop the POST body of the
        # edit / delete endpoints.
        re_path(
            r"^organization/(?P<ptid>\d+)/members/add$",
            views.add_product_type_member,
            name="add_product_type_member",
        ),
        re_path(
            r"^organization/member/(?P<memberid>\d+)/edit$",
            views.edit_product_type_member,
            name="edit_product_type_member",
        ),
        re_path(
            r"^organization/member/(?P<memberid>\d+)/delete$",
            views.delete_product_type_member,
            name="delete_product_type_member",
        ),
        re_path(
            r"^organization/(?P<ptid>\d+)/groups/add$",
            views.add_product_type_group,
            name="add_product_type_group",
        ),
        re_path(
            r"^organization/group/(?P<groupid>\d+)/edit$",
            views.edit_product_type_group,
            name="edit_product_type_group",
        ),
        re_path(
            r"^organization/group/(?P<groupid>\d+)/delete$",
            views.delete_product_type_group,
            name="delete_product_type_group",
        ),
        # TODO: Backwards compatibility; remove after v3 migration is complete
        re_path(r"^product/type$", redirect_view("product_type")),
        re_path(r"^product/type/(?P<ptid>\d+)$", redirect_view("view_product_type")),
        re_path(r"^product/type/(?P<ptid>\d+)/edit$", redirect_view("edit_product_type")),
        re_path(r"^product/type/(?P<ptid>\d+)/delete$", redirect_view("delete_product_type")),
        re_path(r"^product/type/add$", redirect_view("add_product_type")),
        re_path(r"^product/type/(?P<ptid>\d+)/add_product", redirect_view("add_product_to_product_type")),
        re_path(r"^product/type/(?P<ptid>\d+)/authorized_users/add$", redirect_view("add_product_type_authorized_users")),
        re_path(r"^product/type/(?P<ptid>\d+)/authorized_users/(?P<user_id>\d+)/delete$", redirect_view("delete_product_type_authorized_user")),
    ]
else:
    urlpatterns = [
        #  product type
        re_path(r"^product/type$", views.product_type, name="product_type"),
        re_path(r"^product/type/(?P<ptid>\d+)$",
                views.view_product_type, name="view_product_type"),
        re_path(r"^product/type/(?P<ptid>\d+)/edit$",
                views.edit_product_type, name="edit_product_type"),
        re_path(r"^product/type/(?P<ptid>\d+)/delete$",
                views.delete_product_type, name="delete_product_type"),
        re_path(r"^product/type/add$", views.add_product_type,
                name="add_product_type"),
        re_path(r"^product/type/(?P<ptid>\d+)/add_product",
                product_views.new_product,
                name="add_product_to_product_type"),
        re_path(r"^product/type/(?P<ptid>\d+)/authorized_users/add$",
                views.add_product_type_authorized_users,
                name="add_product_type_authorized_users"),
        re_path(r"^product/type/(?P<ptid>\d+)/authorized_users/(?P<user_id>\d+)/delete$",
                views.delete_product_type_authorized_user,
                name="delete_product_type_authorized_user"),
        # RBAC role grants (INTEGRATIONS_ROADMAP.md §7.8), alongside the
        # authorized_users routes above rather than replacing them (§7.4).
        re_path(r"^product/type/(?P<ptid>\d+)/members/add$", views.add_product_type_member,
                name="add_product_type_member"),
        re_path(r"^product/type/member/(?P<memberid>\d+)/edit$", views.edit_product_type_member,
                name="edit_product_type_member"),
        re_path(r"^product/type/member/(?P<memberid>\d+)/delete$", views.delete_product_type_member,
                name="delete_product_type_member"),
        re_path(r"^product/type/(?P<ptid>\d+)/groups/add$", views.add_product_type_group,
                name="add_product_type_group"),
        re_path(r"^product/type/group/(?P<groupid>\d+)/edit$", views.edit_product_type_group,
                name="edit_product_type_group"),
        re_path(r"^product/type/group/(?P<groupid>\d+)/delete$", views.delete_product_type_group,
                name="delete_product_type_group"),
        # Forward compatibility
        re_path(r"^organization$", redirect_view("product_type")),
        re_path(r"^organization/(?P<ptid>\d+)$", redirect_view("view_product_type")),
        re_path(r"^organization/(?P<ptid>\d+)/edit$", redirect_view("edit_product_type")),
        re_path(r"^organization/(?P<ptid>\d+)/delete$", redirect_view("delete_product_type")),
        re_path(r"^organization/add$", redirect_view("add_product_type")),
        re_path(r"^organization/(?P<ptid>\d+)/add_product", redirect_view("add_product_to_product_type")),
        re_path(r"^organization/(?P<ptid>\d+)/authorized_users/add$", redirect_view("add_product_type_authorized_users")),
        re_path(r"^organization/(?P<ptid>\d+)/authorized_users/(?P<user_id>\d+)/delete$", redirect_view("delete_product_type_authorized_user")),
    ]
