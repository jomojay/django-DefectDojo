from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.dates import MONTHS

from dojo.authorization.models import Dojo_Group, Product_Group, Product_Member
from dojo.labels import get_labels
from dojo.models import (
    Dojo_User,
    Product,
    Product_API_Scan_Configuration,
    Product_Type,
    SLA_Configuration,
    Tool_Configuration,
)
from dojo.product.queries import get_authorized_products
from dojo.product_type.queries import get_authorized_product_types
from dojo.validators import tag_validator

labels = get_labels()


class ProductForm(forms.ModelForm):
    name = forms.CharField(max_length=255, required=True)
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=True)

    prod_type = forms.ModelChoiceField(label=labels.ORG_LABEL,
                                       queryset=Product_Type.objects.none(),
                                       required=True)

    sla_configuration = forms.ModelChoiceField(label="SLA Configuration",
                                        queryset=SLA_Configuration.objects.all(),
                                        required=True,
                                        initial="Default")

    product_manager = forms.ModelChoiceField(label=labels.ASSET_MANAGER_LABEL,
                                             queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)
    technical_contact = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)
    team_manager = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prod_type"].queryset = get_authorized_product_types("add")
        self.fields["enable_product_tag_inheritance"].label = labels.ASSET_TAG_INHERITANCE_ENABLE_LABEL
        self.fields["enable_product_tag_inheritance"].help_text = labels.ASSET_TAG_INHERITANCE_ENABLE_HELP
        if prod_type_id := kwargs.get("instance", Product()).prod_type_id:  # we are editing existing instance
            self.fields["prod_type"].queryset |= Product_Type.objects.filter(pk=prod_type_id)  # even if user does not have permission for any other ProdType we need to add at least assign ProdType to make form submittable (otherwise empty list was here which generated invalid form)

        # if this product has findings being asynchronously updated, disable the sla config field
        if self.instance.async_updating:
            self.fields["sla_configuration"].disabled = True
            self.fields["sla_configuration"].widget.attrs["message"] = (
                "Finding SLA expiration dates are currently being recalculated. "
                "This field cannot be changed until the calculation is complete."
            )

    class Meta:
        model = Product
        fields = ["name", "description", "tags", "product_manager", "technical_contact", "team_manager", "prod_type", "sla_configuration", "regulations",
                "business_criticality", "platform", "lifecycle", "origin", "user_records", "revenue", "external_audience", "enable_product_tag_inheritance",
                "internet_accessible", "enable_simple_risk_acceptance", "enable_full_risk_acceptance", "disable_sla_breach_notifications"]

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class DeleteProductForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product
        fields = ["id"]


class Add_Product_AuthorizedUsersForm(forms.Form):
    users = forms.ModelMultipleChoiceField(
        queryset=Dojo_User.objects.none(), required=True, label="Users",
    )

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product = product
        current = product.authorized_users.values_list("pk", flat=True)
        self.fields["users"].queryset = (
            Dojo_User.objects.filter(is_active=True)
            .exclude(is_superuser=True)
            .exclude(pk__in=current)
            .order_by("first_name", "last_name")
        )


class Authorize_User_For_ProductsForm(forms.Form):
    products = forms.ModelMultipleChoiceField(
        queryset=Product.objects.none(), required=True, label=labels.ASSET_PLURAL_LABEL,
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # Show products the user is not already directly authorized for.
        self.fields["products"].queryset = (
            Product.objects.exclude(authorized_users=user).order_by("name")
        )


def get_years():
    now = timezone.now()
    return [(now.year, now.year), (now.year - 1, now.year - 1), (now.year - 2, now.year - 2)]


class ProductCountsFormBase(forms.Form):
    month = forms.ChoiceField(choices=list(MONTHS.items()), required=True, error_messages={
        "required": "*"})
    year = forms.ChoiceField(choices=get_years, required=True, error_messages={
        "required": "*"})


class ProductTagCountsForm(ProductCountsFormBase):
    product_tag = forms.ModelChoiceField(required=True,
                                         queryset=Product.tags.tag_model.objects.none().order_by("name"),
                                         label=labels.ASSET_TAG_LABEL,
                                         error_messages={
                                             "required": "*"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        prods = get_authorized_products("view")
        tags_available_to_user = Product.tags.tag_model.objects.filter(product__in=prods)
        self.fields["product_tag"].queryset = tags_available_to_user


class Product_API_Scan_ConfigurationForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    tool_configuration = forms.ModelChoiceField(
        label="Tool Configuration",
        queryset=Tool_Configuration.objects.all().order_by("name"),
        required=True,
    )

    class Meta:
        model = Product_API_Scan_Configuration
        exclude = ["product"]


class DeleteProduct_API_Scan_ConfigurationForm(forms.ModelForm):
    id = forms.IntegerField(required=True, widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product_API_Scan_Configuration
        fields = ["id"]


# ---------------------------------------------------------------------------
# Role-grant forms for a Product (INTEGRATIONS_ROADMAP.md §7.5 / §7.8).
#
# Ported from the pre-3.0 dojo/forms.py (commit db1932c9e) and landed in the
# owning module per current convention rather than back in the dojo/forms.py
# monolith. Only the "add" forms are multi-pick pages; role changes are
# single-field posts driven from the panel row menus, which is why the Edit
# variants expose `role` alone instead of a disabled copy of the whole row.
# ---------------------------------------------------------------------------


class Add_Product_MemberForm(forms.ModelForm):

    """
    Multi-user picker for one product and one role.

    The queryset excludes superusers (they already reach everything), inactive
    users, and anybody who already holds a grant on this product — the last of
    which is the only guard against duplicate rows until migration ``0279``
    adds a uniqueness constraint (INTEGRATIONS_ROADMAP.md R15).
    """

    users = forms.ModelMultipleChoiceField(queryset=Dojo_User.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        self.fields["product"].label = labels.ASSET_LABEL
        current_members = Product_Member.objects.filter(
            product=self.initial["product"],
        ).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True) | Q(id__in=current_members),
        ).exclude(is_active=False).order_by("first_name", "last_name")

    class Meta:
        model = Product_Member
        fields = ["product", "users", "role"]


class Edit_Product_MemberForm(forms.ModelForm):

    """One existing ``Product_Member`` row; only the role is editable."""

    class Meta:
        model = Product_Member
        fields = ["role"]


class Add_Product_GroupForm(forms.ModelForm):

    """Multi-group picker for one product and one role."""

    groups = forms.ModelMultipleChoiceField(queryset=Dojo_Group.objects.none(), required=True, label="Groups")

    def __init__(self, *args, **kwargs):
        # Lazy: dojo.group.queries pulls in dojo.authorization.authorization,
        # which this module is otherwise below in the import graph.
        from dojo.group.queries import get_authorized_groups  # noqa: PLC0415

        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        self.fields["product"].label = labels.ASSET_LABEL
        current_groups = Product_Group.objects.filter(
            product=self.initial["product"],
        ).values_list("group", flat=True)
        # Only groups the user may actually see are offerable — otherwise the
        # picker leaks the existence (and names) of every group in the install.
        self.fields["groups"].queryset = get_authorized_groups("view").exclude(id__in=current_groups)

    class Meta:
        model = Product_Group
        fields = ["product", "groups", "role"]


class Edit_Product_GroupForm(forms.ModelForm):

    """One existing ``Product_Group`` row; only the role is editable."""

    class Meta:
        model = Product_Group
        fields = ["role"]
