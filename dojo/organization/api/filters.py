from django_filters import BooleanFilter, NumberFilter
from django_filters.rest_framework import FilterSet

from dojo.authorization.models import Product_Type_Group, Product_Type_Member
from dojo.labels import get_labels
from dojo.models import Product_Type

labels = get_labels()


class OrganizationFilterSet(FilterSet):
    critical_asset = BooleanFilter(field_name="critical_product")
    key_asset = BooleanFilter(field_name="key_product")

    class Meta:
        model = Product_Type
        fields = ("id", "name", "created", "updated")


# The v3 Organization twins of the product_type_members / product_type_groups
# filtersets (INTEGRATIONS_ROADMAP.md §7.7), restored from
# db1932c9e:dojo/organization/api/filters.py.
#
# Deviation from that source, deliberately corrected here: it declared the
# group filter as ``asset_type_id`` - a copy/paste slip, since these rows hang
# off a Product_Type (Organization), not an Asset. Restoring the typo would
# have shipped an Organization route whose only scope filter is named after a
# different object, so the field is ``organization_id`` on both, matching the
# member filterset it was cloned from.


class OrganizationMemberFilterSet(FilterSet):
    organization_id = NumberFilter(field_name="product_type_id")

    class Meta:
        model = Product_Type_Member
        fields = ("id", "user_id")


class OrganizationGroupFilterSet(FilterSet):
    organization_id = NumberFilter(field_name="product_type_id")

    class Meta:
        model = Product_Type_Group
        fields = ("id", "group_id")
