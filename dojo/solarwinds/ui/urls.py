"""
SolarWinds configuration UI routes.

``re_path`` and the ``^solarwinds...$`` shape match every other module's
``ui/urls.py`` in this tree. Authorization for each of these names is declared in
``dojo.authorization.url_permissions.URL_PERMISSIONS`` - a route added here
without a matching entry there is reachable by any authenticated user, because
``AuthorizationMiddleware`` skips names it does not know.
"""
from django.urls import re_path

from dojo.solarwinds.ui import views

urlpatterns = [
    re_path(r"^solarwinds$", views.solarwinds_instances, name="solarwinds_instances"),
    re_path(r"^solarwinds/add$", views.add_solarwinds_instance, name="add_solarwinds_instance"),
    re_path(r"^solarwinds/(?P<sid>\d+)$", views.view_solarwinds_instance, name="view_solarwinds_instance"),
    re_path(r"^solarwinds/(?P<sid>\d+)/edit$", views.edit_solarwinds_instance, name="edit_solarwinds_instance"),
    re_path(r"^solarwinds/(?P<sid>\d+)/delete$", views.delete_solarwinds_instance, name="delete_solarwinds_instance"),
    re_path(r"^solarwinds/(?P<sid>\d+)/test$", views.test_solarwinds_instance, name="test_solarwinds_instance"),
    re_path(r"^solarwinds/(?P<sid>\d+)/product/add$", views.add_solarwinds_product, name="add_solarwinds_product"),
    re_path(r"^solarwinds/product/(?P<spid>\d+)/edit$", views.edit_solarwinds_product, name="edit_solarwinds_product"),
    re_path(r"^solarwinds/product/(?P<spid>\d+)/delete$", views.delete_solarwinds_product, name="delete_solarwinds_product"),
]
