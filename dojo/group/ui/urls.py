"""
Group UI routes.

URL names mirror the pre-3.0 module (``db1932c9e:dojo/group/urls.py``) so that
templates, tests and the future Pro-shaped overrides keep reversing the same
names. ``re_path`` matches every other module's urls.py in this tree.

Authorization for each of these names is declared in
``dojo.authorization.url_permissions.URL_PERMISSIONS``.
"""
from django.urls import re_path

from dojo.group.ui import views

urlpatterns = [
    re_path(r"^group$", views.groups, name="groups"),
    re_path(r"^group/add$", views.add_group, name="add_group"),
    re_path(r"^group/(?P<group_id>\d+)$", views.view_group, name="view_group"),
    re_path(r"^group/(?P<group_id>\d+)/edit$", views.edit_group, name="edit_group"),
    re_path(r"^group/(?P<group_id>\d+)/delete$", views.delete_group, name="delete_group"),
    re_path(r"^group/(?P<gid>\d+)/add_group_member$", views.add_group_member, name="add_group_member"),
    re_path(r"^group/member/(?P<mid>\d+)/edit_group_member$", views.edit_group_member, name="edit_group_member"),
    re_path(r"^group/member/(?P<mid>\d+)/delete_group_member$", views.delete_group_member, name="delete_group_member"),
]
