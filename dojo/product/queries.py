try:
    from dojo.authorization.query_filters import get_auth_filter
except ImportError:
    def get_auth_filter(key): return None

from crum import get_current_user

from dojo.authorization.models import Product_Group, Product_Member
from dojo.models import (
    App_Analysis,
    DojoMeta,
    Engagement_Presets,
    Languages,
    Product,
    Product_API_Scan_Configuration,
)
from dojo.request_cache import cache_for_request


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_products(permission, user=None):
    impl = get_auth_filter("product.get_authorized_products")
    if impl:
        return impl(permission, user=user)
    return Product.objects.all().order_by("name")


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_app_analysis(permission):
    impl = get_auth_filter("product.get_authorized_app_analysis")
    if impl:
        return impl(permission)
    return App_Analysis.objects.all().order_by("id")


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_dojo_meta(permission):
    impl = get_auth_filter("product.get_authorized_dojo_meta")
    if impl:
        return impl(permission)
    return DojoMeta.objects.all().order_by("id")


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_languages(permission):
    impl = get_auth_filter("product.get_authorized_languages")
    if impl:
        return impl(permission)
    return Languages.objects.all().order_by("id")


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_engagement_presets(permission):
    impl = get_auth_filter("product.get_authorized_engagement_presets")
    if impl:
        return impl(permission)
    return Engagement_Presets.objects.all().order_by("id")


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_product_api_scan_configurations(permission):
    impl = get_auth_filter("product.get_authorized_product_api_scan_configurations")
    if impl:
        return impl(permission)
    return Product_API_Scan_Configuration.objects.all().order_by("id")


# ---------------------------------------------------------------------------
# Role-grant rows on a Product (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from the pre-3.0 dojo/product/queries.py (commit db1932c9e) and
# retargeted onto this fork's Action-string vocabulary. Both build on
# get_authorized_products(), so they inherit whichever resolver DD_FEATURE_RBAC
# selects for free and can never disagree with the object-level check.
#
# Deliberately NOT registered in dojo.authorization.query_filters: they hold no
# authorization logic of their own beyond "rows whose Product you may act on",
# so there is nothing for a plugin to override that overriding
# get_authorized_products() would not already cover.
# ---------------------------------------------------------------------------


def get_authorized_product_members(permission):
    """``Product_Member`` rows on every Product the current user may act on with ``permission``."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_global_permission,
    )

    user = get_current_user()

    if user is None:
        return Product_Member.objects.none()

    if user.is_superuser:
        return Product_Member.objects.all().order_by("id").select_related("role")

    # A global grant reaches every product, so it reaches every membership row.
    if user_has_global_permission(user, permission):
        return Product_Member.objects.all().order_by("id").select_related("role")

    products = get_authorized_products(permission)
    return Product_Member.objects.filter(product__in=products).order_by("id").select_related("role")


def get_authorized_product_groups(permission):
    """``Product_Group`` rows on every Product the current user may act on with ``permission``."""
    user = get_current_user()

    if user is None:
        return Product_Group.objects.none()

    if user.is_superuser:
        return Product_Group.objects.all().order_by("id").select_related("role")

    products = get_authorized_products(permission)
    return Product_Group.objects.filter(product__in=products).order_by("id").select_related("role")


# ---------------------------------------------------------------------------
# Grant rows *for one Product* — the reverse direction of the two functions
# above, consumed by the RBAC panels on the Product detail page
# (INTEGRATIONS_ROADMAP.md §7.8).
#
# Ported from the pre-3.0 dojo/product/queries.py (commit db1932c9e) with the
# Permissions enum swapped for this fork's Action strings. The visibility gate
# is deliberately the *same* object-level check the page itself ran, so the
# panel can never list grants on a product the viewer may not act on.
# ---------------------------------------------------------------------------


def get_authorized_members_for_product(product, permission):
    """``Product_Member`` rows on ``product``, or nothing if the user may not see them."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_permission,
    )

    user = get_current_user()

    if user is None:
        return Product_Member.objects.none()

    if user.is_superuser or user_has_permission(user, product, permission):
        return Product_Member.objects.filter(product=product).order_by(
            "user__first_name", "user__last_name",
        ).select_related("role", "product", "user")
    return Product_Member.objects.none()


def get_authorized_groups_for_product(product, permission):
    """``Product_Group`` rows on ``product``, narrowed to groups the user may view."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_permission,
    )
    from dojo.group.queries import get_authorized_groups  # noqa: PLC0415 -- lazy import, avoids circular dependency

    user = get_current_user()

    if user is None:
        return Product_Group.objects.none()

    if user.is_superuser or user_has_permission(user, product, permission):
        return Product_Group.objects.filter(
            product=product, group__in=get_authorized_groups("view"),
        ).order_by("group__name").select_related("role", "group")
    return Product_Group.objects.none()
