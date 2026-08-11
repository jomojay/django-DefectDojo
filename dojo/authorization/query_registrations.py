"""
Authorization queryset filters.

Each filter restricts results to objects whose underlying Product /
Product_Type the user is authorized on, with ``is_superuser`` and
``is_staff`` bypasses.

Two resolvers live here, selected per call by ``DD_FEATURE_RBAC``
(INTEGRATIONS_ROADMAP.md §7.6):

  * **legacy** (``off``, and ``shadow`` — see ``_rbac_enabled_for_queries``):
    membership in ``authorized_users``, action-blind. ``_is_unrestricted`` /
    ``_authorized_product_ids`` / ``_authorized_product_type_ids`` /
    ``authorized_product_id_set`` / ``authorized_product_type_id_set``
    implement it and are deliberately left untouched.
  * **role-aware** (``on``): the union of legacy ``authorized_users``
    membership (worth exactly ``LEGACY_AUTHORIZED_USERS_ACTIONS``), direct
    ``Product_Member`` / ``Product_Type_Member`` role grants, group grants via
    ``Product_Group`` / ``Product_Type_Group`` → ``Dojo_Group_Member``, and
    ``Global_Role`` held directly or through a group. Product_Type grants
    cascade to every Product under the type, exactly as
    ``prod_type__authorized_users`` already does.

Object-level checks (``dojo.authorization.authorization``) resolve through the
same two leaf implementations, so the object answer and the list answer agree
by construction in every flag state.

The dojo/authorization/queries entry-point names (e.g. ``product.get_
authorized_products``) are preserved so the per-app queries.py modules and
the API filter classes that look them up via ``get_auth_filter()`` keep
working without code changes.
"""
from collections import defaultdict

from crum import get_current_user
from django.db.models import Q

from dojo.authorization.models import (
    Global_Role,
    Product_Group,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
)
from dojo.authorization.query_filters import register_auth_filter
from dojo.authorization.roles_permissions import (
    FEATURE_RBAC_ON,
    LEGACY_AUTHORIZED_USERS_ACTIONS,
    Roles,
    feature_rbac_state,
    get_global_roles_with_permissions,
    get_roles_with_permissions,
    permission_to_action,
)
from dojo.location.models import Location, LocationFindingReference, LocationProductReference
from dojo.models import (
    App_Analysis,
    Dojo_User,
    DojoMeta,
    Endpoint,
    Endpoint_Status,
    Engagement,
    Engagement_Presets,
    Finding,
    Finding_Group,
    JIRA_Issue,
    JIRA_Project,
    Languages,
    Product,
    Product_API_Scan_Configuration,
    Product_Type,
    Risk_Acceptance,
    Test,
    Test_Import,
    Tool_Product_Settings,
    Vulnerability_Id,
)
from dojo.request_cache import cache_for_request


def _resolve_user(user):
    return user if user is not None else get_current_user()


def _is_unrestricted(user, action):
    """
    Returns True if the user can see every object regardless of membership.
    Superuser and staff both bypass — matches pre-2020 behavior where
    is_staff was an absolute bypass for every perm_type. The ``action``
    arg is retained for callers that may want to gate StaffOnly /
    SuperuserOnly differently in the future.
    """
    if not user or getattr(user, "is_anonymous", False):
        return False
    if user.is_superuser:
        return True
    return bool(user.is_staff)


def _authorized_product_ids(user):
    """
    QuerySet of product ids the user can access via authorized_users.
    Lazy on purpose — callers that pass this into ``.filter(id__in=...)``
    let Postgres collapse it into a single subquery.
    """
    return Product.objects.filter(
        Q(authorized_users=user) | Q(prod_type__authorized_users=user),
    ).values("id")


def _authorized_product_type_ids(user):
    """
    QuerySet of product_type ids the user can access via authorized_users.
    Lazy on purpose (see ``_authorized_product_ids``).
    """
    return Product_Type.objects.filter(authorized_users=user).values("id")


@cache_for_request
def authorized_product_id_set(user_pk):
    """
    Frozen set of product ids the user can access via authorized_users
    (direct or via prod_type). Result is cached for the lifetime of the
    current request — repeated per-object permission checks (one per
    object, often dozens per request) collapse to a single SELECT.

    Returns an empty frozenset for anonymous / missing users so callers
    can do ``pid in set`` without a None check.
    """
    if not user_pk:
        return frozenset()
    return frozenset(
        Product.objects.filter(
            Q(authorized_users=user_pk) | Q(prod_type__authorized_users=user_pk),
        ).values_list("id", flat=True),
    )


@cache_for_request
def authorized_product_type_id_set(user_pk):
    """
    Frozen set of product_type ids the user is a direct member of via
    authorized_users. Cached per request (see ``authorized_product_id_set``).
    """
    if not user_pk:
        return frozenset()
    return frozenset(
        Product_Type.objects.filter(authorized_users=user_pk).values_list("id", flat=True),
    )


# ---------------------------------------------------------------------------
# Role-aware resolution (DD_FEATURE_RBAC=on)
#
# Everything below is inert unless the flag says "on". The legacy helpers above
# are never modified by it — that is what keeps the "off" state byte-identical
# to the pre-RBAC engine.
# ---------------------------------------------------------------------------


def _rbac_enabled_for_queries() -> bool:
    """
    True only in the ``on`` state.

    ``shadow`` deliberately leaves *queryset* filtering on the legacy path. Shadow
    mode's value is the cheap single-boolean divergence log on object-level checks
    (dojo/authorization/authorization.py); diffing two full result sets on every
    list endpoint would be expensive and adds no signal the object-level log does
    not already carry. Keeping querysets legacy while shadowing also keeps the
    object answer and the list answer in agreement, which is what
    unittests/test_authorization_queryset_coverage.py polices.
    """
    return feature_rbac_state() == FEATURE_RBAC_ON


def _roles_for_action(action):
    """
    Roles whose action set grants ``action``.

    Imported lazily on purpose: ``dojo.authorization.authorization`` imports this
    module at module level, so a top-level import back would be circular. Reused
    rather than reimplemented so the queryset side and the object side can never
    drift onto two different readings of the role matrix.
    """
    from dojo.authorization.authorization import get_roles_for_permission  # noqa: PLC0415 circular import

    return sorted(get_roles_for_permission(action))


def _actions_for_role_id(role_id, *, global_role=False):
    """
    Action set granted by a ``dojo_role`` row id.

    Unknown ids (a role row outside the seeded five) grant nothing rather than
    raising — an unrecognised role must never widen access.
    """
    if role_id is None or not Roles.has_value(role_id):
        return frozenset()
    role = Roles(role_id)
    actions = set(get_roles_with_permissions().get(role, set()))
    if global_role:
        actions |= set(get_global_roles_with_permissions().get(role, set()))
    return frozenset(actions)


@cache_for_request
def rbac_global_action_set(user_pk):
    """
    Actions granted system-wide by a ``Global_Role`` — held directly by the user or
    by a ``Dojo_Group`` they belong to.

    A global role is "applied to all product types and products" per the field's own
    help text, so it is resolved here as an unrestricted-access bypass rather than
    folded into the per-object maps below: those are scoped to specific ids, and
    expressing a genuinely global grant in them would mean enumerating every Product
    row on every request.

    Global roles carry the object-scoped action set of their role plus the extra
    grants in ``get_global_roles_with_permissions()``. One query, cached per request.
    """
    if not user_pk:
        return frozenset()
    role_ids = set(
        Global_Role.objects.filter(
            Q(user=user_pk) | Q(group__users=user_pk),
        ).values_list("role_id", flat=True),
    )
    actions = set()
    for role_id in role_ids:
        actions |= _actions_for_role_id(role_id, global_role=True)
    return frozenset(actions)


def _rbac_is_unrestricted(user, action):
    """
    Role-aware counterpart of ``_is_unrestricted``: True if the user can act on every
    object regardless of per-object membership.

    ``is_staff`` remains an absolute bypass under RBAC, exactly as under legacy —
    this flag is about adding role granularity below staff, not about redefining what
    staff means. A Global_Role granting ``action`` is the new arm.
    """
    if not user or getattr(user, "is_anonymous", False):
        return False
    if user.is_superuser:
        return True
    if user.is_staff:
        return True
    return permission_to_action(action).value in rbac_global_action_set(getattr(user, "pk", None))


@cache_for_request
def rbac_product_action_map(user_pk):
    """
    ``{product_id: frozenset(actions)}`` — the union of every grant the user holds on
    each product, from a fixed number of queries (six at most, independent of how many
    products or how many distinct actions are checked during the request).

    Cached per request and deliberately **not** keyed on the action: keying on action
    would multiply queries by the number of distinct actions a single list view checks.
    The action-blind cache that this replaces was correct only while membership was
    binary; the value here is action-aware even though the key is not.
    """
    if not user_pk:
        return {}

    grants = defaultdict(set)

    # 1. Legacy authorized_users — directly on the product, or inherited from its
    #    product type (mirrors authorized_product_id_set).
    for product_id in Product.objects.filter(
        Q(authorized_users=user_pk) | Q(prod_type__authorized_users=user_pk),
    ).values_list("id", flat=True):
        grants[product_id] |= LEGACY_AUTHORIZED_USERS_ACTIONS

    # 2/3. Role grants held directly on the product, by the user or by a group.
    for product_id, role_id in Product_Member.objects.filter(
        user=user_pk,
    ).values_list("product_id", "role_id"):
        grants[product_id] |= _actions_for_role_id(role_id)
    for product_id, role_id in Product_Group.objects.filter(
        group__users=user_pk,
    ).values_list("product_id", "role_id"):
        grants[product_id] |= _actions_for_role_id(role_id)

    # 4/5/6. Product_Type role grants cascade to every product under the type, the
    #        same way prod_type__authorized_users cascades in step 1.
    type_grants = defaultdict(set)
    for product_type_id, role_id in Product_Type_Member.objects.filter(
        user=user_pk,
    ).values_list("product_type_id", "role_id"):
        type_grants[product_type_id] |= _actions_for_role_id(role_id)
    for product_type_id, role_id in Product_Type_Group.objects.filter(
        group__users=user_pk,
    ).values_list("product_type_id", "role_id"):
        type_grants[product_type_id] |= _actions_for_role_id(role_id)
    if type_grants:
        for product_id, product_type_id in Product.objects.filter(
            prod_type__in=list(type_grants),
        ).values_list("id", "prod_type_id"):
            grants[product_id] |= type_grants[product_type_id]

    return {product_id: frozenset(actions) for product_id, actions in grants.items() if actions}


@cache_for_request
def rbac_product_type_action_map(user_pk):
    """
    ``{product_type_id: frozenset(actions)}`` — same construction as
    ``rbac_product_action_map`` without the cascade (a product type has no parent).
    Three queries, cached per request.
    """
    if not user_pk:
        return {}

    grants = defaultdict(set)

    for product_type_id in Product_Type.objects.filter(
        authorized_users=user_pk,
    ).values_list("id", flat=True):
        grants[product_type_id] |= LEGACY_AUTHORIZED_USERS_ACTIONS

    for product_type_id, role_id in Product_Type_Member.objects.filter(
        user=user_pk,
    ).values_list("product_type_id", "role_id"):
        grants[product_type_id] |= _actions_for_role_id(role_id)
    for product_type_id, role_id in Product_Type_Group.objects.filter(
        group__users=user_pk,
    ).values_list("product_type_id", "role_id"):
        grants[product_type_id] |= _actions_for_role_id(role_id)

    return {product_type_id: frozenset(actions) for product_type_id, actions in grants.items() if actions}


def _legacy_products_q(user):
    return Q(authorized_users=user) | Q(prod_type__authorized_users=user)


def _rbac_products_q(user, action):
    """
    Lazy, action-aware ``Q`` over Product — the queryset-side twin of
    ``rbac_product_action_map``. Kept as a ``Q`` (not a materialised id set) so
    callers passing it into ``.filter(id__in=...)`` still collapse to one subquery.

    ``Q(pk__in=[])`` is the identity element: an action no role and no legacy grant
    confers (e.g. ``staff_only``) yields a Q that matches nothing, which is correct.
    """
    q = Q(pk__in=[])
    if permission_to_action(action).value in LEGACY_AUTHORIZED_USERS_ACTIONS:
        q |= _legacy_products_q(user)
    roles = _roles_for_action(action)
    if roles:
        q |= Q(product_member__user=user, product_member__role__in=roles)
        q |= Q(product_group__group__users=user, product_group__role__in=roles)
        q |= Q(prod_type__product_type_member__user=user, prod_type__product_type_member__role__in=roles)
        q |= Q(prod_type__product_type_group__group__users=user, prod_type__product_type_group__role__in=roles)
    return q


def _rbac_product_types_q(user, action):
    """``_rbac_products_q`` without the ``prod_type__`` indirection."""
    q = Q(pk__in=[])
    if permission_to_action(action).value in LEGACY_AUTHORIZED_USERS_ACTIONS:
        q |= Q(authorized_users=user)
    roles = _roles_for_action(action)
    if roles:
        q |= Q(product_type_member__user=user, product_type_member__role__in=roles)
        q |= Q(product_type_group__group__users=user, product_type_group__role__in=roles)
    return q


# ---------------------------------------------------------------------------
# Flag-aware dispatchers. Every filter below goes through these three rather
# than calling either resolver directly, so the flag is checked in exactly one
# place per concern and no registered filter needs a call-site change.
# ---------------------------------------------------------------------------


def _unrestricted(user, action):
    if _rbac_enabled_for_queries():
        return _rbac_is_unrestricted(user, action)
    return _is_unrestricted(user, action)


def _products_q(user, action):
    if _rbac_enabled_for_queries():
        return _rbac_products_q(user, action)
    return _legacy_products_q(user)


def _product_ids(user, action):
    """Lazy queryset of authorized product ids for ``action`` (see ``_authorized_product_ids``)."""
    if _rbac_enabled_for_queries():
        return Product.objects.filter(_rbac_products_q(user, action)).values("id")
    return _authorized_product_ids(user)


def _product_type_ids(user, action):
    """Lazy queryset of authorized product type ids for ``action``."""
    if _rbac_enabled_for_queries():
        return Product_Type.objects.filter(_rbac_product_types_q(user, action)).values("id")
    return _authorized_product_type_ids(user)


def _filter_by_authorized_products(queryset, product_path, permission, user=None):
    """
    Generic helper: restrict ``queryset`` to rows whose ``product_path`` FK
    points at a Product the user is authorized for. ``product_path`` is a
    Django ORM lookup like ``"product"`` or ``"engagement__product"``.
    """
    user = _resolve_user(user)
    if user is None or getattr(user, "is_anonymous", False):
        return queryset.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return queryset
    return queryset.filter(**{f"{product_path}__id__in": _product_ids(user, action)})


# ---------------------------------------------------------------------------
# Product / Product_Type
# ---------------------------------------------------------------------------


def _get_authorized_products(permission, user=None):
    user = _resolve_user(user)
    if user is None or getattr(user, "is_anonymous", False):
        return Product.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return Product.objects.all().order_by("name")
    return Product.objects.filter(_products_q(user, action)).distinct().order_by("name")


register_auth_filter("product.get_authorized_products", _get_authorized_products)


def _get_authorized_product_types(permission, user=None):
    user = _resolve_user(user)
    if user is None or getattr(user, "is_anonymous", False):
        return Product_Type.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return Product_Type.objects.all().order_by("name")
    if _rbac_enabled_for_queries():
        return Product_Type.objects.filter(_rbac_product_types_q(user, action)).distinct().order_by("name")
    return Product_Type.objects.filter(authorized_users=user).order_by("name")


register_auth_filter("product_type.get_authorized_product_types", _get_authorized_product_types)


# ---------------------------------------------------------------------------
# Children of Product / Product_Type (membership inherited)
# ---------------------------------------------------------------------------


def _get_authorized_engagements(permission):
    return _filter_by_authorized_products(Engagement.objects.all(), "product", permission)


register_auth_filter("engagement.get_authorized_engagements", _get_authorized_engagements)


def _get_authorized_tests(permission, product=None):
    qs = Test.objects.all()
    if product is not None:
        qs = qs.filter(engagement__product=product)
    return _filter_by_authorized_products(qs, "engagement__product", permission)


register_auth_filter("test.get_authorized_tests", _get_authorized_tests)


def _get_authorized_test_imports(permission):
    return _filter_by_authorized_products(Test_Import.objects.all(), "test__engagement__product", permission)


register_auth_filter("test.get_authorized_test_imports", _get_authorized_test_imports)


def _get_authorized_risk_acceptances(permission):
    return _filter_by_authorized_products(Risk_Acceptance.objects.all(), "engagement__product", permission)


register_auth_filter("risk_acceptance.get_authorized_risk_acceptances", _get_authorized_risk_acceptances)


def _get_authorized_finding_groups(permission, user=None):
    return _filter_by_authorized_products(
        Finding_Group.objects.all(), "test__engagement__product", permission, user=user,
    )


register_auth_filter("finding_group.get_authorized_finding_groups", _get_authorized_finding_groups)


def _get_authorized_finding_groups_for_queryset(permission, queryset, user=None):
    return _filter_by_authorized_products(queryset, "test__engagement__product", permission, user=user)


register_auth_filter("finding_group.get_authorized_finding_groups_for_queryset", _get_authorized_finding_groups_for_queryset)


def _get_authorized_app_analysis(permission):
    return _filter_by_authorized_products(App_Analysis.objects.all(), "product", permission)


register_auth_filter("product.get_authorized_app_analysis", _get_authorized_app_analysis)


def _get_authorized_dojo_meta(permission):
    user = get_current_user()
    if user is None or getattr(user, "is_anonymous", False):
        return DojoMeta.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return DojoMeta.objects.all()
    authorized_products = _product_ids(user, action)
    authorized_product_types = _product_type_ids(user, action)
    return DojoMeta.objects.filter(
        Q(product__id__in=authorized_products)
        | Q(product_type__id__in=authorized_product_types)
        | Q(finding__test__engagement__product__id__in=authorized_products)
        | Q(endpoint__product__id__in=authorized_products),
    )


register_auth_filter("product.get_authorized_dojo_meta", _get_authorized_dojo_meta)


def _get_authorized_languages(permission):
    return _filter_by_authorized_products(Languages.objects.all(), "product", permission)


register_auth_filter("product.get_authorized_languages", _get_authorized_languages)


def _get_authorized_engagement_presets(permission):
    return _filter_by_authorized_products(Engagement_Presets.objects.all(), "product", permission)


register_auth_filter("product.get_authorized_engagement_presets", _get_authorized_engagement_presets)


def _get_authorized_product_api_scan_configurations(permission):
    return _filter_by_authorized_products(
        Product_API_Scan_Configuration.objects.all(), "product", permission,
    )


register_auth_filter("product.get_authorized_product_api_scan_configurations", _get_authorized_product_api_scan_configurations)


def _get_authorized_jira_projects(permission, user=None):
    user = _resolve_user(user)
    if user is None or getattr(user, "is_anonymous", False):
        return JIRA_Project.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return JIRA_Project.objects.all()
    authorized_products = _product_ids(user, action)
    authorized_product_types = _product_type_ids(user, action)
    return JIRA_Project.objects.filter(
        Q(product__id__in=authorized_products)
        | Q(product__prod_type__id__in=authorized_product_types)
        | Q(engagement__product__id__in=authorized_products),
    ).distinct()


register_auth_filter("jira_link.get_authorized_jira_projects", _get_authorized_jira_projects)


def _get_authorized_jira_issues(permission):
    user = get_current_user()
    if user is None or getattr(user, "is_anonymous", False):
        return JIRA_Issue.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return JIRA_Issue.objects.all()
    authorized_products = _product_ids(user, action)
    return JIRA_Issue.objects.filter(
        Q(engagement__product__id__in=authorized_products)
        | Q(finding__test__engagement__product__id__in=authorized_products)
        | Q(finding_group__test__engagement__product__id__in=authorized_products),
    )


register_auth_filter("jira_link.get_authorized_jira_issues", _get_authorized_jira_issues)


def _get_authorized_tool_product_settings(permission):
    return _filter_by_authorized_products(Tool_Product_Settings.objects.all(), "product", permission)


register_auth_filter("tool_product.get_authorized_tool_product_settings", _get_authorized_tool_product_settings)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def _get_authorized_locations(permission, queryset=None, user=None):
    user = _resolve_user(user)
    qs = queryset if queryset is not None else Location.objects.all()
    if user is None or getattr(user, "is_anonymous", False):
        return qs.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return qs
    authorized_products = _product_ids(user, action)
    return qs.filter(products__product__id__in=authorized_products).distinct()


register_auth_filter("location.get_authorized_locations", _get_authorized_locations)


def _get_authorized_location_finding_reference(permission, queryset=None, user=None):
    user = _resolve_user(user)
    qs = queryset if queryset is not None else LocationFindingReference.objects.all()
    if user is None or getattr(user, "is_anonymous", False):
        return qs.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return qs
    authorized_products = _product_ids(user, action)
    return qs.filter(finding__test__engagement__product__id__in=authorized_products)


register_auth_filter("location.get_authorized_location_finding_reference", _get_authorized_location_finding_reference)


def _get_authorized_location_product_reference(permission, queryset=None, user=None):
    user = _resolve_user(user)
    qs = queryset if queryset is not None else LocationProductReference.objects.all()
    if user is None or getattr(user, "is_anonymous", False):
        return qs.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return qs
    authorized_products = _product_ids(user, action)
    return qs.filter(product__id__in=authorized_products)


register_auth_filter("location.get_authorized_location_product_reference", _get_authorized_location_product_reference)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _get_authorized_endpoints(permission, user=None):
    return _filter_by_authorized_products(Endpoint.objects.all(), "product", permission, user=user)


register_auth_filter("endpoint.get_authorized_endpoints", _get_authorized_endpoints)


def _get_authorized_endpoints_for_queryset(permission, queryset, user=None):
    return _filter_by_authorized_products(queryset, "product", permission, user=user)


register_auth_filter("endpoint.get_authorized_endpoints_for_queryset", _get_authorized_endpoints_for_queryset)


def _get_authorized_endpoint_status(permission, user=None):
    return _filter_by_authorized_products(
        Endpoint_Status.objects.all(), "endpoint__product", permission, user=user,
    )


register_auth_filter("endpoint.get_authorized_endpoint_status", _get_authorized_endpoint_status)


def _get_authorized_endpoint_status_for_queryset(permission, queryset, user=None):
    return _filter_by_authorized_products(queryset, "endpoint__product", permission, user=user)


register_auth_filter("endpoint.get_authorized_endpoint_status_for_queryset", _get_authorized_endpoint_status_for_queryset)


# ---------------------------------------------------------------------------
# Findings / Vulnerability_Ids
# ---------------------------------------------------------------------------


def _get_authorized_findings(permission, queryset=None, user=None):
    user = _resolve_user(user)
    qs = queryset if queryset is not None else Finding.objects.all()
    if user is None or getattr(user, "is_anonymous", False):
        return qs.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return qs
    return qs.filter(test__engagement__product__id__in=_product_ids(user, action))


register_auth_filter("finding.get_authorized_findings", _get_authorized_findings)
register_auth_filter("finding.get_authorized_findings_for_queryset", _get_authorized_findings)


def _get_authorized_vulnerability_ids(permission, queryset=None, user=None):
    user = _resolve_user(user)
    qs = queryset if queryset is not None else Vulnerability_Id.objects.all()
    if user is None or getattr(user, "is_anonymous", False):
        return qs.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action):
        return qs
    return qs.filter(finding__test__engagement__product__id__in=_product_ids(user, action))


register_auth_filter("finding.get_authorized_vulnerability_ids", _get_authorized_vulnerability_ids)
register_auth_filter("finding.get_authorized_vulnerability_ids_for_queryset", _get_authorized_vulnerability_ids)


# ---------------------------------------------------------------------------
# User queries
# ---------------------------------------------------------------------------


def _get_authorized_users(permission, user=None):
    user = _resolve_user(user)
    if user is None or getattr(user, "is_anonymous", False):
        return Dojo_User.objects.none()
    action = permission_to_action(permission)
    if _unrestricted(user, action) or user.is_staff:
        return Dojo_User.objects.all().order_by("first_name", "last_name")
    # Collaborators — users sharing the caller's authorized products /
    # product types (via authorized_users), plus superusers. Mirrors 2.58.4,
    # which returned co-members of the caller's authorized products/types.
    authorized_products = _product_ids(user, action)
    authorized_product_types = _product_type_ids(user, action)
    collaborators = (
        Q(authorized_products__id__in=authorized_products)
        | Q(authorized_product_types__id__in=authorized_product_types)
        | Q(is_superuser=True)
    )
    if _rbac_enabled_for_queries():
        # Under RBAC a co-worker may hold a role grant instead of (or as well as) an
        # authorized_users row, so role membership counts as sharing a scope too.
        collaborators |= (
            Q(product_members__id__in=authorized_products)
            | Q(prod_type_members__id__in=authorized_product_types)
        )
    return Dojo_User.objects.filter(collaborators).distinct().order_by("first_name", "last_name")


register_auth_filter("user.get_authorized_users", _get_authorized_users)


def _get_authorized_users_for_product_type(users, product_type, permission):
    if users is None:
        users = Dojo_User.objects.all()
    user = get_current_user()
    if user is None or getattr(user, "is_anonymous", False):
        return users.none()
    if _unrestricted(user, permission_to_action(permission)) or user.is_staff:
        return users
    if product_type is None:
        return users.none()
    # OS: users authorized on this product type via authorized_users, plus
    # superusers (2.58.4 always surfaced is_superuser users as candidates).
    return users.filter(
        Q(id__in=product_type.authorized_users.values("id"))
        | Q(is_superuser=True),
    )


register_auth_filter("user.get_authorized_users_for_product_type", _get_authorized_users_for_product_type)


def _get_authorized_users_for_product_and_product_type(users, product, permission):
    if users is None:
        users = Dojo_User.objects.all()
    user = get_current_user()
    if user is None or getattr(user, "is_anonymous", False):
        return users.none()
    if _unrestricted(user, permission_to_action(permission)) or user.is_staff:
        return users
    if product is None:
        return users.none()
    # OS: users authorized on this product via authorized_users (directly on
    # the product or via its product type), plus superusers (2.58.4 always
    # surfaced is_superuser users as candidates).
    return users.filter(
        Q(id__in=product.authorized_users.values("id"))
        | Q(id__in=product.prod_type.authorized_users.values("id"))
        | Q(is_superuser=True),
    )


register_auth_filter("user.get_authorized_users_for_product_and_product_type", _get_authorized_users_for_product_and_product_type)
