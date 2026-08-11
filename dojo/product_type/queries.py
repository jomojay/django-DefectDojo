try:
    from dojo.authorization.query_filters import get_auth_filter
except ImportError:
    def get_auth_filter(key): return None

from crum import get_current_user

from dojo.authorization.models import Product_Type_Group, Product_Type_Member
from dojo.models import Product_Type
from dojo.request_cache import cache_for_request


# Cached: all parameters are hashable, no dynamic queryset filtering
@cache_for_request
def get_authorized_product_types(permission):
    impl = get_auth_filter("product_type.get_authorized_product_types")
    if impl:
        return impl(permission)
    return Product_Type.objects.all().order_by("name")


# ---------------------------------------------------------------------------
# Role-grant rows on a Product_Type (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from the pre-3.0 dojo/product_type/queries.py (commit db1932c9e) and
# retargeted onto this fork's Action-string vocabulary. Both build on
# get_authorized_product_types(), so they inherit whichever resolver
# DD_FEATURE_RBAC selects and can never disagree with the object-level check.
# ---------------------------------------------------------------------------


def get_authorized_product_type_members(permission):
    """``Product_Type_Member`` rows on every Product_Type the current user may act on with ``permission``."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_global_permission,
    )

    user = get_current_user()

    if user is None:
        return Product_Type_Member.objects.none()

    if user.is_superuser:
        return Product_Type_Member.objects.all().order_by("id").select_related("role")

    # A global grant reaches every product type, so it reaches every membership row.
    if user_has_global_permission(user, permission):
        return Product_Type_Member.objects.all().order_by("id").select_related("role")

    product_types = get_authorized_product_types(permission)
    return Product_Type_Member.objects.filter(product_type__in=product_types).order_by("id").select_related("role")


def get_authorized_product_type_groups(permission):
    """``Product_Type_Group`` rows on every Product_Type the current user may act on with ``permission``."""
    user = get_current_user()

    if user is None:
        return Product_Type_Group.objects.none()

    if user.is_superuser:
        return Product_Type_Group.objects.all().order_by("id").select_related("role")

    product_types = get_authorized_product_types(permission)
    return Product_Type_Group.objects.filter(product_type__in=product_types).order_by("id").select_related("role")


# ---------------------------------------------------------------------------
# Grant rows *for one Product_Type* — the reverse direction of the two
# functions above, consumed by the RBAC panels on the Product Type detail page
# (INTEGRATIONS_ROADMAP.md §7.8).
# ---------------------------------------------------------------------------


def get_authorized_members_for_product_type(product_type, permission):
    """``Product_Type_Member`` rows on ``product_type``, or nothing if the user may not see them."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_permission,
    )

    user = get_current_user()

    if user is None:
        return Product_Type_Member.objects.none()

    if user.is_superuser or user_has_permission(user, product_type, permission):
        return Product_Type_Member.objects.filter(product_type=product_type).order_by(
            "user__first_name", "user__last_name",
        ).select_related("role", "product_type", "user")
    return Product_Type_Member.objects.none()


def get_authorized_groups_for_product_type(product_type, permission):
    """``Product_Type_Group`` rows on ``product_type``, narrowed to groups the user may view."""
    from dojo.authorization.authorization import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
        user_has_permission,
    )
    from dojo.group.queries import get_authorized_groups  # noqa: PLC0415 -- lazy import, avoids circular dependency

    user = get_current_user()

    if user is None:
        return Product_Type_Group.objects.none()

    if user.is_superuser or user_has_permission(user, product_type, permission):
        return Product_Type_Group.objects.filter(
            product_type=product_type, group__in=get_authorized_groups("view"),
        ).order_by("group__name").select_related("role", "group")
    return Product_Type_Group.objects.none()
