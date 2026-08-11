"""
REST API surface for the two authorization *reference* models
(INTEGRATIONS_ROADMAP.md §7.7).

This package holds no models of its own: ``Role`` and ``Global_Role`` live in
``dojo.authorization.models`` (reactivated by migration
``0278_reactivate_rbac_models``). It exists so the role catalogue and the
global-role grant surface are served from the module that owns authorization,
rather than from the ``dojo/api_v2`` monolith they lived in before 3.0.

Two routes, so there is no single ``path`` constant — see ``urls.py``.
"""

ROLES_PATH = "roles"  # noqa: RUF067
GLOBAL_ROLES_PATH = "global_roles"  # noqa: RUF067
