"""
REST API surface for the SolarWinds Service Desk configuration (INTEGRATIONS_ROADMAP.md 8.2).

Two routes, so there is no single ``path`` constant - see ``urls.py``. Route and
basename are chosen to mirror the JIRA registrations they sit next to
(``jira_instances``/``jira_instance``), since this module is JIRA-shaped by design.
"""

INSTANCES_PATH = "solarwinds_instances"  # noqa: RUF067
PRODUCTS_PATH = "solarwinds_product_configurations"  # noqa: RUF067
