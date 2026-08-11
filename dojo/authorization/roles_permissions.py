from enum import IntEnum, StrEnum

from django.conf import settings

# Valid states of the DD_FEATURE_RBAC rollout flag (INTEGRATIONS_ROADMAP.md §7.6).
FEATURE_RBAC_OFF = "off"
FEATURE_RBAC_SHADOW = "shadow"
FEATURE_RBAC_ON = "on"
FEATURE_RBAC_STATES = frozenset({FEATURE_RBAC_OFF, FEATURE_RBAC_SHADOW, FEATURE_RBAC_ON})


def feature_rbac_state() -> str:
    """
    Current DD_FEATURE_RBAC state, read fresh from settings on *every* call.

    Deliberately not cached and never bound to a module-level constant: the whole
    point of the "shadow" state is to compare two live code paths on every check,
    and tests flip this with ``override_settings`` without restarting the process.
    Unknown / misspelled values resolve to "off" so a typo in the environment fails
    closed onto the legacy engine rather than silently enabling enforcement.
    """
    state = getattr(settings, "FEATURE_RBAC", FEATURE_RBAC_OFF)
    state = str(state or FEATURE_RBAC_OFF).strip().lower()
    return state if state in FEATURE_RBAC_STATES else FEATURE_RBAC_OFF


# The action set implied by legacy ``authorized_users`` membership on a Product or
# Product_Type, expressed as its own constant rather than borrowed from a named Role
# - no Role below matches these semantics exactly (Writer is the closest, but the
# equivalence is a coincidence of the current matrix, not a definition).
#
# Frozen by definition: this is exactly what the pre-RBAC model granted a non-staff
# authorized_users member, and it must never silently widen. It has to keep matching
# what the legacy short-circuit + action-blind membership check in
# ``authorization._legacy_authorized()`` actually grants today - view/add/edit/import
# allowed, delete/manage/own/staff_only denied - otherwise a plain authorized_users
# member would gain or lose capability purely from flipping DD_FEATURE_RBAC.
LEGACY_AUTHORIZED_USERS_ACTIONS = frozenset({"view", "add", "edit", "import"})


class Action(StrEnum):

    """
    Permission actions. The fine-grained Permissions enum below is preserved
    so existing call sites (`@user_is_authorized(Permissions.X, …)`) keep
    compiling, but every check flattens to one of these intents:

      * View          — read-only access to an object (membership in
                        authorized_users, or staff/superuser bypass)
      * Edit / Add    — mutating an existing object or creating one
                        (membership in authorized_users + staff bypass)
      * Delete        — destroying an object (staff/superuser only today;
                        Maintainer/Owner under the role-aware resolver)
      * Import        — bulk ingest of scan results (staff bypass + per-product
                        membership)
      * Manage        — member/group grant management on a container
                        (Product / Product_Type / Dojo_Group)
      * Own           — container-level ownership: delete the container,
                        grant the Owner role to somebody else
      * StaffOnly     — administrative actions like member management or
                        configuration changes. Fully superseded by
                        ``Manage``/``Own``: no permission name resolves here any
                        more, and no role grants it. Retained as an accepted
                        input shape (callers may still pass it explicitly) and
                        as the legacy "staff bypass only" marker.
      * SuperuserOnly — system-wide changes that legacy never delegated

    ``Manage`` and ``Own`` exist so that the role matrix in
    ``get_roles_with_permissions()`` can express Maintainer > Writer and
    Owner > Maintainer. ``permission_to_action()`` resolves ``_Manage_`` and
    ``_Add_Owner`` permission names to them, so they are produced by real
    request-path checks — though only ``DD_FEATURE_RBAC=on`` resolves them
    through the role matrix; ``off`` keeps treating both as staff-only
    (INTEGRATIONS_ROADMAP.md §7.6).
    """

    View = "view"
    Add = "add"
    Edit = "edit"
    Delete = "delete"
    Import = "import"
    Manage = "manage"
    Own = "own"
    StaffOnly = "staff_only"
    SuperuserOnly = "superuser_only"


class Roles(IntEnum):

    """
    The role hierarchy: Reader < API_Importer/Writer < Maintainer < Owner.

    The integer values are the primary keys of the seeded ``dojo_role`` rows
    and must not be renumbered. The action set each role grants is defined by
    ``get_roles_with_permissions()`` below; role assignments live in
    ``Product_Member`` / ``Product_Type_Member`` / ``Global_Role`` and the
    corresponding ``*_Group`` tables.

    These are consulted by the role-aware resolver in
    ``dojo.authorization.query_registrations``, which ``user_has_permission()``
    selects when ``DD_FEATURE_RBAC`` is ``on`` (and evaluates alongside the
    legacy resolver when it is ``shadow``). Under the default ``off`` they are
    never read — see INTEGRATIONS_ROADMAP.md §7.6.
    """

    Reader = 5
    API_Importer = 1
    Writer = 2
    Maintainer = 3
    Owner = 4

    @classmethod
    def has_value(cls, value):
        try:
            Roles(value)
        except ValueError:
            return False
        return True


def django_enum(cls):
    # decorator needed to enable enums in django templates
    # see
    # https://stackoverflow.com/questions/35953132/how-to-access-enum-types-in-django-templates
    cls.do_not_call_in_templates = True
    return cls


@django_enum
class Permissions(IntEnum):
    Product_Type_Add_Product = 1001
    Product_Type_View = 1002
    Product_Type_Member_Delete = 1003
    Product_Type_Manage_Members = 1004
    Product_Type_Member_Add_Owner = 1005
    Product_Type_Edit = 1006
    Product_Type_Delete = 1007
    Product_Type_Add = 1008

    Product_View = 1102
    Product_Member_Delete = 1103
    Product_Manage_Members = 1104
    Product_Member_Add_Owner = 1105
    Product_Configure_Notifications = 1106
    Product_Edit = 1107
    Product_Delete = 1108

    Engagement_View = 1202
    Engagement_Add = 1203
    Engagement_Edit = 1206
    Engagement_Delete = 1207
    Risk_Acceptance = 1208

    Test_View = 1302
    Test_Add = 1303
    Test_Edit = 1306
    Test_Delete = 1307

    Finding_View = 1402
    Finding_Add = 1403
    Import_Scan_Result = 1404
    Finding_Edit = 1406
    Finding_Delete = 1407

    Location_View = 1502
    Location_Add = 1503
    Location_Edit = 1506
    Location_Delete = 1507

    Benchmark_Edit = 1606
    Benchmark_Delete = 1607

    Component_View = 1702

    Note_View_History = 1802
    Note_Add = 1803
    Note_Edit = 1806
    Note_Delete = 1807

    Finding_Group_View = 1902
    Finding_Group_Add = 1903
    Finding_Group_Edit = 1906
    Finding_Group_Delete = 1907

    Product_Type_Group_View = 2002
    Product_Type_Group_Add = 2003
    Product_Type_Group_Add_Owner = 2005
    Product_Type_Group_Edit = 2006
    Product_Type_Group_Delete = 2007

    Product_Group_View = 2102
    Product_Group_Add = 2103
    Product_Group_Add_Owner = 2105
    Product_Group_Edit = 2106
    Product_Group_Delete = 2107

    Group_View = 2202
    Group_Member_Delete = 2203
    Group_Manage_Members = 2204
    Group_Add_Owner = 2205
    Group_Edit = 2206
    Group_Delete = 2207

    Language_View = 2302
    Language_Add = 2303
    Language_Edit = 2306
    Language_Delete = 2307

    Technology_View = 2402
    Technology_Add = 2403
    Technology_Edit = 2406
    Technology_Delete = 2407

    Product_API_Scan_Configuration_View = 2502
    Product_API_Scan_Configuration_Add = 2503
    Product_API_Scan_Configuration_Edit = 2506
    Product_API_Scan_Configuration_Delete = 2507

    Product_Tracking_Files_View = 2602
    Product_Tracking_Files_Add = 2603
    Product_Tracking_Files_Edit = 2606
    Product_Tracking_Files_Delete = 2607

    @classmethod
    def has_value(cls, value):
        try:
            Permissions(value)
        except ValueError:
            return False
        return True

    @classmethod
    def get_engagement_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
            "add",
            "import",
        }.union(cls.get_test_permissions())

    @classmethod
    def get_test_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
            "add",
            "import",
        }.union(cls.get_finding_permissions())

    @classmethod
    def get_finding_permissions(cls):
        return {
            "view",
            "edit",
            "add",
            "import",
            "delete",
        }.union(cls.get_finding_group_permissions())

    @classmethod
    def get_finding_group_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_location_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_product_member_permissions(cls):
        return {
            "view",
            "staff_only",
            "delete",
        }

    @classmethod
    def get_product_type_member_permissions(cls):
        return {
            "view",
            "staff_only",
            "delete",
        }

    @classmethod
    def get_product_group_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_product_type_group_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_group_permissions(cls):
        return {
            "view",
            "delete",
            "staff_only",
            "edit",
        }

    @classmethod
    def get_group_member_permissions(cls):
        return {
            "view",
            "staff_only",
            "delete",
        }

    @classmethod
    def get_language_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_technology_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }

    @classmethod
    def get_product_api_scan_configuration_permissions(cls):
        return {
            "view",
            "edit",
            "delete",
        }


def get_roles_with_permissions():
    """
    The role → action-set matrix (INTEGRATIONS_ROADMAP.md §7.3).

    Cumulative by design — each role is a strict superset of the one below it:

      Reader       view                                        (read-only stakeholder)
      Writer       + add, edit, import                         (no delete, historically)
      Maintainer   + delete, manage                            (member/group grants)
      Owner        + own                                       (delete container, grant Owner)

    ``API_Importer`` sits outside the ladder: same actions as Writer, so that
    a token-only integration account can push scan results without gaining
    ``delete`` or member management.

    Pure function, no DB access — the ``dojo_role`` rows carry only the name
    and ``is_owner`` flag; the authority behind each role lives here.
    """
    return {
        Roles.Reader: {
            "view",
        },
        Roles.API_Importer: {
            "view",
            "add",
            "edit",
            "import",
        },
        Roles.Writer: {
            "view",
            "add",
            "edit",
            "import",
        },
        Roles.Maintainer: {
            "view",
            "add",
            "edit",
            "import",
            "delete",
            "manage",
        },
        Roles.Owner: {
            "view",
            "add",
            "edit",
            "import",
            "delete",
            "manage",
            "own",
        },
    }


def get_global_roles_with_permissions():
    """Extra permissions for global roles, on top of the permissions granted to the "normal" roles above."""
    return {
        Roles.Maintainer: {"add"},
        Roles.Owner: {"add"},
    }


def permission_to_action(permission):
    """
    Map a fine-grained Permissions enum member, action string, or legacy
    enum-name string (e.g. "Product_Edit") to an Action.

    The suffix-based mapping captures every Permissions name (which all
    follow the ``<Noun>_<Verb>`` convention); the noun is irrelevant
    because legacy authorization is not noun-aware (the object passed at
    check time determines the membership scope).
    """
    if isinstance(permission, Action):
        return permission

    if isinstance(permission, str):
        try:
            return Action(permission)
        except ValueError:
            name = permission
    else:
        name = getattr(permission, "name", "") or str(permission)

    if name == "Risk_Acceptance":
        return Action.Edit
    if name == "Import_Scan_Result":
        return Action.Import
    if name.endswith(("_View", "_View_History")):
        return Action.View
    if name.endswith(("_Edit", "_Configure_Notifications")):
        return Action.Edit
    if name.endswith("_Delete"):
        return Action.Delete
    if name.endswith(("_Add_Product", "_Add")):
        return Action.Add
    # Repointed at Action.Manage / Action.Own in PR 3, together with the
    # flag-gating of the short-circuit these used to rely on. The two changes are
    # one coupled edit and had to land in the same commit:
    #
    #   * before: `_Manage_`/`_Add_Owner` resolved to Action.StaffOnly purely so
    #     they would hit `if action in {StaffOnly, Delete}: return user.is_staff`
    #     in user_has_permission(). Repointing them alone would have dropped the
    #     Manage-Members views through to the action-blind membership check,
    #     letting any authorized_users member manage grants.
    #   * now: authorization._legacy_authorized() keeps treating Manage and Own as
    #     staff-only (they *are* the actions that used to be StaffOnly), so with
    #     DD_FEATURE_RBAC=off those views resolve exactly as they always did. Under
    #     "on", the role-aware resolver answers them from the role matrix instead,
    #     which is what makes Maintainer/Owner mean anything at all.
    #
    # Action.StaffOnly is retained as an input shape (callers may still pass it
    # explicitly, and it stays granted by no role) but is no longer produced here.
    if "_Manage_" in name:
        return Action.Manage
    if name.endswith("_Add_Owner"):
        return Action.Own

    return Action.View
