from dojo.solarwinds.api import INSTANCES_PATH, PRODUCTS_PATH
from dojo.solarwinds.api.views import SolarWindsInstanceViewSet, SolarWindsProductViewSet


def add_solarwinds_urls(router):
    # Route / basename pairs deliberately mirror the JIRA registrations in
    # dojo/urls.py (route "jira_instances", basename "jira_instance").
    router.register(INSTANCES_PATH, SolarWindsInstanceViewSet, basename="solarwinds_instance")
    router.register(PRODUCTS_PATH, SolarWindsProductViewSet, basename="solarwinds_product_configuration")
    return router
