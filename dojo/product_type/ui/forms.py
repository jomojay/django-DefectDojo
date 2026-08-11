import logging

from django import forms
from django.db.models import Q

from dojo.authorization.models import (
    Dojo_Group,
    Product_Type_Group,
    Product_Type_Member,
)
from dojo.labels import get_labels
from dojo.models import Dojo_User
from dojo.product_type.models import Product_Type

logger = logging.getLogger(__name__)

labels = get_labels()


class Product_TypeForm(forms.ModelForm):
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["critical_product"].label = labels.ORG_CRITICAL_PRODUCT_LABEL
        self.fields["key_product"].label = labels.ORG_KEY_PRODUCT_LABEL

    class Meta:
        model = Product_Type
        fields = ["name", "description", "critical_product", "key_product"]


class Delete_Product_TypeForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product_Type
        fields = ["id"]


class Add_Product_Type_AuthorizedUsersForm(forms.Form):
    users = forms.ModelMultipleChoiceField(
        queryset=Dojo_User.objects.none(), required=True, label="Users",
    )

    def __init__(self, *args, product_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product_type = product_type
        current = product_type.authorized_users.values_list("pk", flat=True)
        self.fields["users"].queryset = (
            Dojo_User.objects.filter(is_active=True)
            .exclude(is_superuser=True)
            .exclude(pk__in=current)
            .order_by("first_name", "last_name")
        )


# ---------------------------------------------------------------------------
# Role-grant forms for a Product_Type (INTEGRATIONS_ROADMAP.md §7.5 / §7.8).
# Mirrors dojo/product/ui/forms.py exactly; see the note there.
# ---------------------------------------------------------------------------


class Add_Product_Type_MemberForm(forms.ModelForm):

    """Multi-user picker for one product type and one role."""

    users = forms.ModelMultipleChoiceField(queryset=Dojo_User.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product_type"].disabled = True
        self.fields["product_type"].label = labels.ORG_LABEL
        current_members = Product_Type_Member.objects.filter(
            product_type=self.initial["product_type"],
        ).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True) | Q(id__in=current_members),
        ).exclude(is_active=False).order_by("first_name", "last_name")

    class Meta:
        model = Product_Type_Member
        fields = ["product_type", "users", "role"]


class Edit_Product_Type_MemberForm(forms.ModelForm):

    """One existing ``Product_Type_Member`` row; only the role is editable."""

    class Meta:
        model = Product_Type_Member
        fields = ["role"]


class Add_Product_Type_GroupForm(forms.ModelForm):

    """Multi-group picker for one product type and one role."""

    groups = forms.ModelMultipleChoiceField(queryset=Dojo_Group.objects.none(), required=True, label="Groups")

    def __init__(self, *args, **kwargs):
        # Lazy: dojo.group.queries pulls in dojo.authorization.authorization,
        # which this module is otherwise below in the import graph.
        from dojo.group.queries import get_authorized_groups  # noqa: PLC0415

        super().__init__(*args, **kwargs)
        self.fields["product_type"].disabled = True
        self.fields["product_type"].label = labels.ORG_LABEL
        current_groups = Product_Type_Group.objects.filter(
            product_type=self.initial["product_type"],
        ).values_list("group", flat=True)
        self.fields["groups"].queryset = get_authorized_groups("view").exclude(id__in=current_groups)

    class Meta:
        model = Product_Type_Group
        fields = ["product_type", "groups", "role"]


class Edit_Product_Type_GroupForm(forms.ModelForm):

    """One existing ``Product_Type_Group`` row; only the role is editable."""

    class Meta:
        model = Product_Type_Group
        fields = ["role"]
