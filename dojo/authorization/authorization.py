"""
Object-level authorization, with two resolvers selected by ``DD_FEATURE_RBAC``
(INTEGRATIONS_ROADMAP.md §7.6). The flag is read fresh on every call — never
cached, never bound to a module-level constant.

**legacy** (``_legacy_authorized``) is the pre-RBAC engine, unchanged: an action
on an object is authorized iff

  * the user is a superuser, or
  * the action is destructive/administrative (Delete / Manage / Own /
    StaffOnly) and the user is staff, or
  * the user is staff (an absolute bypass, matching the pre-2020 model), or
  * the user is in the relevant ``authorized_users`` ManyToMany — an
    action-blind check, climbing the Product_Type → Product → Engagement →
    Test → Finding hierarchy until an authorization-bearing parent is found.

**role-aware** (``_rbac_authorized``) keeps that same climb but replaces the
Product / Product_Type leaf with a real per-action lookup: the union of legacy
``authorized_users`` membership (worth exactly
``LEGACY_AUTHORIZED_USERS_ACTIONS``), direct ``Product_Member`` /
``Product_Type_Member`` grants, group grants, and ``Global_Role``. Both modes
share one recursion (``_authorized_for``) and differ only in the two callbacks
they pass it, so they cannot drift apart.

Flag states:

  * ``off`` (default) — legacy answer, byte-identical to the pre-RBAC engine.
  * ``shadow`` — both resolvers run, the legacy answer is returned, every
    divergence is logged at WARNING.
  * ``on`` — the role-aware answer is authoritative.
"""
import logging

from django.core.exceptions import PermissionDenied
from django.db.models import Model

from dojo.authorization.models import (
    Dojo_Group_Member,
    Product_Group,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
)
from dojo.authorization.query_registrations import (
    _is_unrestricted,
    _rbac_is_unrestricted,
    authorized_product_id_set,
    authorized_product_type_id_set,
    rbac_product_action_map,
    rbac_product_type_action_map,
)
from dojo.authorization.roles_permissions import (
    FEATURE_RBAC_OFF,
    FEATURE_RBAC_SHADOW,
    Action,
    feature_rbac_state,
    get_global_roles_with_permissions,
    get_roles_with_permissions,
    permission_to_action,
)
from dojo.location.models import AbstractLocation, Location
from dojo.models import (
    App_Analysis,
    Dojo_Group,
    Dojo_User,
    Endpoint,
    Engagement,
    Finding,
    Finding_Group,
    Languages,
    Product,
    Product_API_Scan_Configuration,
    Product_Type,
    Risk_Acceptance,
    Test,
)

logger = logging.getLogger(__name__)


def user_has_configuration_permission(user: Dojo_User, permission: str):
    """
    Legacy: configuration permissions reduce to is_superuser / is_staff,
    matching the rest of the legacy auth model. ``user.has_perm`` is
    still consulted as a fallback so explicit Django permission grants
    (e.g. ``auth.add_user`` granted via Django Admin) keep working for
    non-staff users. Pro overrides this function at runtime via
    pro/apps.py:_shadow_authorization_symbols, so this OS bypass does
    not affect Pro deployments.
    """
    if not user:
        return False
    if user.is_anonymous:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.has_perm(permission)


def user_is_superuser_or_global_owner(user: Dojo_User) -> bool:
    """
    True if the user holds system-wide authority: the ``is_superuser`` flag,
    or a ``Global_Role`` whose ``Role.is_owner`` is set — held either directly
    by the user or by a ``Dojo_Group`` the user belongs to.

    The Global_Role arms are dark in practice, not in principle:
    ``dojo_global_role`` has no rows in this deployment and nothing can create
    one until the group/role management UI and API land (PRs 4-5), so today
    this returns exactly ``user.is_superuser`` for every existing account.
    Unlike the role helpers at the bottom of this module, this function has
    live callers (``IsSuperUserOrGlobalOwner`` in ``api_permissions.py``,
    ``dojo/user/authentication.py``, ``dojo/user/ui/{forms,views}.py``), so the
    equivalence above is what makes the change safe to ship dark.
    """
    if not user or getattr(user, "is_anonymous", False):
        return False

    if user.is_superuser:
        return True

    if not getattr(user, "pk", None):
        # Unsaved / synthetic user: no rows can reference it.
        return False

    # ``global_role`` is the reverse side of Global_Role.user, a OneToOneField:
    # attribute access raises Global_Role.DoesNotExist when there is no row.
    # That exception also subclasses AttributeError (Django makes
    # RelatedObjectDoesNotExist dual-inherit precisely for this), so getattr's
    # default is the correct "no global role" path here.
    global_role = getattr(user, "global_role", None)
    if global_role is not None and global_role.role is not None and global_role.role.is_owner:
        return True

    # Global role granted through group membership.
    return Dojo_Group_Member.objects.filter(
        user=user,
        group__global_role__role__is_owner=True,
    ).exists()


def user_has_permission(user: Dojo_User, obj: Model, permission) -> bool:
    """
    Object-level authorization check.

    Resolution order:

      1. anonymous → deny
      2. superuser → allow
      3. action → mapped from Permissions / string / Action via permission_to_action
      4. SuperuserOnly action → deny (already handled superuser above)
      5. dispatch on DD_FEATURE_RBAC — see the module docstring.

    Carrier objects don't expose authorization themselves; they delegate to
    their wrapped product or product type in both modes.
    """
    if not user or getattr(user, "is_anonymous", False):
        return False
    if user.is_superuser:
        return True

    action = permission_to_action(permission)

    if action == Action.SuperuserOnly:
        return False

    flag = feature_rbac_state()

    if flag == FEATURE_RBAC_OFF:
        return _legacy_authorized(user, obj, action)

    if flag == FEATURE_RBAC_SHADOW:
        legacy_answer = _legacy_authorized(user, obj, action)
        rbac_answer = _rbac_authorized(user, obj, action)
        if rbac_answer != legacy_answer:
            logger.warning(
                "RBAC shadow divergence: user=%s obj=%s:%s permission=%s legacy=%s rbac=%s",
                getattr(user, "username", user),
                type(obj).__name__,
                getattr(obj, "pk", None),
                action.value,
                legacy_answer,
                rbac_answer,
            )
        return legacy_answer

    # "on": the role-aware resolver is authoritative. The legacy resolver is
    # deliberately not evaluated here — it would be pure wasted queries.
    return _rbac_authorized(user, obj, action)


# ---------------------------------------------------------------------------
# The two resolvers. Both delegate the object-graph climb to ``_authorized_for``
# and supply only the two callbacks that actually differ between them: how a
# Product / Product_Type leaf is decided, and what "unrestricted" means. Keeping
# one copy of the recursion is what stops the modes drifting apart, and keeps
# this fork's diff against upstream small (INTEGRATIONS_ROADMAP.md R18).
# ---------------------------------------------------------------------------


def _legacy_authorized(user: Dojo_User, obj: Model, action: Action) -> bool:
    """
    The pre-RBAC engine, verbatim.

    The ``{StaffOnly, Delete, Manage, Own}`` short-circuit is what makes
    ``DD_FEATURE_RBAC=off`` byte-identical to the old behavior, and it must not
    be deleted: the legacy leaf check below is action-blind, so a Delete/Manage/
    Own action falling through to it would let any ``authorized_users`` member
    delete products or manage grants.

    ``Manage`` and ``Own`` are in that set because they are exactly the actions
    that ``permission_to_action()`` used to resolve to ``StaffOnly`` — legacy has
    no concept of them, so under ``off`` they must keep meaning "staff only".
    Dropping them from this set would regress the Manage-Members panels
    (dojo/product/ui/views.py, dojo/product_type/ui/views.py).
    """
    if action in {Action.StaffOnly, Action.Delete, Action.Manage, Action.Own}:
        return bool(user.is_staff)
    return _authorized_for(user, obj, action, leaf=_legacy_leaf, unrestricted=_is_unrestricted)


def _rbac_authorized(user: Dojo_User, obj: Model, action: Action) -> bool:
    """Role-aware resolver: same object-graph climb, real per-action leaf check."""
    return _authorized_for(user, obj, action, leaf=_rbac_leaf, unrestricted=_rbac_is_unrestricted)


def _legacy_leaf(user: Dojo_User, obj: Model, action: Action) -> bool:
    """Action-blind ``authorized_users`` membership on a Product / Product_Type."""
    if _is_unrestricted(user, action):
        return True
    if isinstance(obj, Dojo_Group):
        # Legacy has no notion of a group-scoped grant: groups are staff-only,
        # exactly as before the group module existed.
        return False
    if isinstance(obj, Product_Type):
        return obj.pk in authorized_product_type_id_set(user.pk)
    # authorized_product_id_set folds direct Product membership AND
    # inherited membership via prod_type into one cached lookup.
    return obj.pk in authorized_product_id_set(user.pk)


def _rbac_leaf(user: Dojo_User, obj: Model, action: Action) -> bool:
    """
    Per-action leaf check against the union of every grant the user holds on this
    object: legacy ``authorized_users``, direct role membership, group grants, and
    (via ``_rbac_is_unrestricted``) ``Global_Role``.

    The maps are built once per request and are action-aware, so checking several
    different actions on many objects stays at a fixed query count.
    """
    if _rbac_is_unrestricted(user, action):
        return True
    if isinstance(obj, Dojo_Group):
        # A group is its own authorization scope: the role a user holds *in* the
        # group (Dojo_Group_Member.role) decides what they may do to it. This is
        # the same rule dojo/group/queries.py's get_authorized_groups() already
        # applies to the list queryset, so object and list answers agree.
        return any(
            role_has_permission(member.role_id, action)
            for member in Dojo_Group_Member.objects.filter(group=obj, user=user).only("role_id")
        )
    if isinstance(obj, Product_Type):
        action_map = rbac_product_type_action_map(user.pk)
    else:
        action_map = rbac_product_action_map(user.pk)
    return action.value in action_map.get(obj.pk, frozenset())


def _grant_container(obj: Model) -> Model:
    """
    The object a member / group-grant row grants access to.

    ``Dojo_Group_Member`` → its ``Dojo_Group``; ``Product_Member`` /
    ``Product_Group`` → the ``Product``; ``Product_Type_Member`` /
    ``Product_Type_Group`` → the ``Product_Type``.
    """
    if isinstance(obj, Dojo_Group_Member):
        return obj.group
    if isinstance(obj, Product_Member | Product_Group):
        return obj.product
    return obj.product_type


def _authorized_for(user: Dojo_User, obj: Model, action: Action, *, leaf, unrestricted) -> bool:
    """
    Walk ``obj`` up to the closest authorization-bearing parent and ask ``leaf``.

    Identical in both modes — only ``leaf`` (Product / Product_Type) and
    ``unrestricted`` (the carrier objects RBAC does not model yet) vary.
    """
    if obj is None:
        return False

    if isinstance(obj, Product_Type | Product):
        return leaf(user, obj, action)

    if isinstance(obj, Engagement):
        return _authorized_for(user, obj.product, action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Test):
        if not obj.engagement_id:
            return False
        return _authorized_for(user, obj.engagement.product, action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Finding | Finding_Group):
        return _authorized_for(user, obj.test.engagement.product, action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Risk_Acceptance):
        # Risk_Acceptance is reachable from Engagement via the reverse M2M
        # `engagement.risk_acceptance`. Pre-2020 followed the same path
        # (see dojo/user/helper.py at e7805aa14~).
        engagement = obj.engagement_set.first()
        if engagement is not None:
            return _authorized_for(user, engagement.product, action, leaf=leaf, unrestricted=unrestricted)
        return False

    if isinstance(obj, Location):
        return any(
            _authorized_for(user, ref.product, action, leaf=leaf, unrestricted=unrestricted)
            for ref in obj.products.all()
        )

    if isinstance(obj, AbstractLocation):
        return _authorized_for(user, obj.location, action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Endpoint | Languages | App_Analysis | Product_API_Scan_Configuration):
        return _authorized_for(user, obj.product, action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Dojo_Group):
        # A group is an authorization scope in its own right, so it is a leaf:
        # legacy resolves it to staff/superuser only, RBAC to the role the user
        # holds inside the group (plus the Global_Role arm via ``unrestricted``).
        return leaf(user, obj, action)

    if isinstance(obj, Dojo_Group_Member | Product_Member | Product_Type_Member):
        # Membership rows inherit the authorization of the container they grant
        # access to, which is what lets a Product Maintainer manage that
        # product's members under DD_FEATURE_RBAC=on
        # (INTEGRATIONS_ROADMAP.md §7.7).
        #
        # The one carve-out is self-*removal*, restored to the exact pre-3.0
        # scope: `Delete` only. Widening it to every action — as the placeholder
        # here did while the member surface was still fail-closed and nothing
        # could reach it — becomes a privilege-escalation path the moment the
        # member rows are writable: a Reader could PUT their own Product_Member
        # row to Maintainer, since the row references them. Leaving your own
        # membership is self-service; changing your own role is not.
        # (dojo/group/ui/views.py:edit_group_member already re-checked the group
        # explicitly to work around exactly this; that check is now redundant
        # rather than load-bearing, and is left in place.)
        #
        # Under ``off`` nothing changes: _legacy_authorized() short-circuits
        # Delete/Manage/Own to is_staff before ever reaching here, so only
        # View/Add/Edit/Import arrive, and those were staff-or-member on the
        # container already.
        #
        # The invariants a single row cannot express (no duplicate row; a
        # product type or group keeps at least one Owner) are enforced where
        # they belong, in the member serializers and viewsets, not here.
        if action == Action.Delete and obj.user_id == user.pk:
            return True
        return _authorized_for(user, _grant_container(obj), action, leaf=leaf, unrestricted=unrestricted)

    if isinstance(obj, Product_Group | Product_Type_Group):
        # Group-grant rows have no "self" carve-out — there is no user on them —
        # but otherwise follow the same rule: the grant is as manageable as the
        # container it grants access to.
        return _authorized_for(user, _grant_container(obj), action, leaf=leaf, unrestricted=unrestricted)

    msg = f"No authorization implemented for class {type(obj).__name__}"
    raise NoAuthorizationImplementedError(msg)


def user_has_global_permission(user: Dojo_User, permission) -> bool:
    """
    Global (non-object-scoped) authorization check.

    Under ``off`` this reduces to is_superuser / is_staff, exactly as before.
    Under ``on`` a ``Global_Role`` — held directly or through a group — can grant
    the action too, including the extra grants that only make sense globally
    (``get_global_roles_with_permissions()``, e.g. a global Maintainer creating
    top-level objects). ``is_staff`` stays an unconditional bypass in both modes.

    The one Django configuration-permission carve-out preserved from the
    pre-2020 model: ``dojo.add_product_type`` lets a non-staff user
    create product types if explicitly granted via Django auth. It is evaluated
    before the flag so it behaves the same in every state.
    """
    if not user or getattr(user, "is_anonymous", False):
        return False
    if user.is_superuser:
        return True

    action = permission_to_action(permission)

    if permission == "add" and user_has_configuration_permission(user, "dojo.add_product_type"):
        return True

    if action == Action.SuperuserOnly:
        return False

    flag = feature_rbac_state()

    if flag == FEATURE_RBAC_OFF:
        return _legacy_global_authorized(user, action)

    if flag == FEATURE_RBAC_SHADOW:
        legacy_answer = _legacy_global_authorized(user, action)
        rbac_answer = _rbac_global_authorized(user, action)
        if rbac_answer != legacy_answer:
            logger.warning(
                "RBAC shadow divergence (global): user=%s permission=%s legacy=%s rbac=%s",
                getattr(user, "username", user),
                action.value,
                legacy_answer,
                rbac_answer,
            )
        return legacy_answer

    return _rbac_global_authorized(user, action)


def _legacy_global_authorized(user: Dojo_User, action: Action) -> bool:
    """Legacy global authorization: staff only (superuser already handled upstream)."""
    return bool(user.is_staff)


def _rbac_global_authorized(user: Dojo_User, action: Action) -> bool:
    """Role-aware global authorization: is_staff, or a Global_Role granting ``action``."""
    return _rbac_is_unrestricted(user, action)


def user_has_configuration_permission_or_403(user: Dojo_User, permission: str) -> None:
    if not user_has_configuration_permission(user, permission):
        raise PermissionDenied


def user_has_permission_or_403(user: Dojo_User, obj: Model, permission) -> None:
    if not user_has_permission(user, obj, permission):
        raise PermissionDenied


def user_has_global_permission_or_403(user: Dojo_User, permission) -> None:
    if not user_has_global_permission(user, permission):
        raise PermissionDenied


# ---------------------------------------------------------------------------
# Role-based helpers.
#
# Pure functions over the role → action matrix in roles_permissions.py: no DB
# access, no request state. They answer "which roles grant this?" / "does this
# role grant this?" and nothing else — deciding whether a *user* holds such a
# role on a given object is the resolver's job (_rbac_authorized above, and
# query_registrations for the queryset side). get_roles_for_permission() is now
# live: query_registrations._roles_for_action() builds every role-aware queryset
# filter on top of it, which is why both sides can never read the matrix
# differently.
#
# They accept the same permission shapes as user_has_permission(): an Action,
# an action string, a Permissions enum member, or a legacy enum-name string —
# everything is funnelled through permission_to_action(). Unknown/bogus inputs
# resolve to Action.View rather than raising, matching that function's
# deliberate fallback (the pre-3.0 engine raised PermissionDoesNotExistError
# here; that vocabulary no longer exists).
# ---------------------------------------------------------------------------


def _action_value(permission) -> str:
    """
    Normalize any accepted permission shape to the plain action string used as
    the matrix vocabulary. Explicit ``.value`` rather than relying on StrEnum
    members hashing equal to their value, so set membership below can't turn
    into a silent, version-dependent False.
    """
    return permission_to_action(permission).value


def get_roles_for_permission(permission) -> set:
    """Every role whose action set grants ``permission``, as a set of Roles members."""
    action = _action_value(permission)
    return {role for role, actions in get_roles_with_permissions().items() if action in actions}


def role_has_permission(role, permission) -> bool:
    """True if ``role`` (a Roles member or its int value) grants ``permission``."""
    action = _action_value(permission)
    return action in get_roles_with_permissions().get(role, set())


def role_has_global_permission(role, permission) -> bool:
    """
    True if ``role`` grants ``permission`` when held as a Global_Role.

    Global roles carry everything the object-scoped role carries, plus the
    extra grants in ``get_global_roles_with_permissions()`` — e.g. a global
    Maintainer may create top-level objects, which is meaningless scoped to a
    single product.
    """
    if role_has_permission(role, permission):
        return True
    action = _action_value(permission)
    return action in get_global_roles_with_permissions().get(role, set())


class NoAuthorizationImplementedError(Exception):
    def __init__(self, message):
        self.message = message


class PermissionDoesNotExistError(Exception):
    def __init__(self, message):
        self.message = message


class RoleDoesNotExistError(Exception):
    def __init__(self, message):
        self.message = message
