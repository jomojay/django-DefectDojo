"""
UI filters for the group list page (AGENTS.md Phase 4).

Ported from ``db1932c9e:dojo/filters.py``'s ``GroupFilter``. No re-export is
added to ``dojo/filters.py``: the only consumer is this module's own list view,
and a re-export there would cycle against the ``DojoFilter`` import below.
"""
from django_filters import CharFilter, OrderingFilter

from dojo.authorization.models import Dojo_Group
from dojo.filters import DojoFilter


class GroupFilter(DojoFilter):
    name = CharFilter(lookup_expr="icontains")
    description = CharFilter(lookup_expr="icontains")

    o = OrderingFilter(
        # tuple-mapping retains order
        fields=(
            ("name", "name"),
        ),
    )

    class Meta:
        model = Dojo_Group
        fields = ["name", "description"]
        exclude = ["users"]
