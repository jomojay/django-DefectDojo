"""
REST API surface for group management (INTEGRATIONS_ROADMAP.md §7.7).

Two routes (``dojo_groups`` and ``dojo_group_members``), so there is no single
``path`` constant — see ``urls.py``. The models served here
(``Dojo_Group`` / ``Dojo_Group_Member``) live in ``dojo.authorization.models``,
same as for the UI layer in ``dojo/group/ui/``.
"""

GROUPS_PATH = "dojo_groups"  # noqa: RUF067
GROUP_MEMBERS_PATH = "dojo_group_members"  # noqa: RUF067
