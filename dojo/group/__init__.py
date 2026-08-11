"""
Group management module (INTEGRATIONS_ROADMAP.md §7.5).

The models this module manages (``Dojo_Group`` / ``Dojo_Group_Member``) live in
``dojo.authorization.models`` — they are authorization data, reactivated by
migration ``0278_reactivate_rbac_models`` — so there is deliberately no
``models.py`` / ``admin.py`` here and nothing for this ``__init__`` to import.
See ``AGENTS.md`` for the canonical module layout.
"""
