from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Product_Type_Group, Product_Type_Member
from dojo.product_type.models import Product_Type


class ProductTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_Type
        fields = "__all__"


# ---------------------------------------------------------------------------
# Role-grant rows on a Product_Type (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from the pre-3.0 dojo/api_v2/serializers.py (commit db1932c9e), with
# both validate() bodies preserved wholesale.
#
# ProductTypeMemberSerializer carries one invariant its Product-level sibling
# does not: **a product type must keep at least one Owner**. It is enforced in
# two places, and both are required —
#
#   * here, for the demote path (PUT changing an Owner row to a lesser role),
#   * in ProductTypeMemberViewSet.destroy(), for the delete path.
#
# Losing the last Owner of a product type would leave nobody able to grant the
# Owner role back short of a superuser, so this is not cosmetic validation.
# ---------------------------------------------------------------------------


class ProductTypeMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_Type_Member
        fields = "__all__"

    def validate(self, data):
        if (
            self.instance is not None
            and data.get("product_type") != self.instance.product_type
            and not user_has_permission(
                self.context["request"].user,
                data.get("product_type"),
                "manage",
            )
        ):
            msg = "You are not permitted to add a member to this product type"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or data.get("product_type") != self.instance.product_type
            or data.get("user") != self.instance.user
        ):
            members = Product_Type_Member.objects.filter(
                product_type=data.get("product_type"), user=data.get("user"),
            )
            if members.count() > 0:
                msg = "Product_Type_Member already exists"
                raise ValidationError(msg)

        if self.instance is not None and not data.get("role").is_owner:
            owners = (
                Product_Type_Member.objects.filter(
                    product_type=data.get("product_type"), role__is_owner=True,
                )
                .exclude(id=self.instance.id)
                .count()
            )
            if owners < 1:
                msg = "There must be at least one owner"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            data.get("product_type"),
            "own",
        ):
            msg = "You are not permitted to add a member as Owner to this product type"
            raise PermissionDenied(msg)

        return data


class ProductTypeGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_Type_Group
        fields = "__all__"

    def validate(self, data):
        if (
            self.instance is not None
            and data.get("product_type") != self.instance.product_type
            and not user_has_permission(
                self.context["request"].user,
                data.get("product_type"),
                "manage",
            )
        ):
            msg = "You are not permitted to add a group to this product type"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or data.get("product_type") != self.instance.product_type
            or data.get("group") != self.instance.group
        ):
            members = Product_Type_Group.objects.filter(
                product_type=data.get("product_type"), group=data.get("group"),
            )
            if members.count() > 0:
                msg = "Product_Type_Group already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            data.get("product_type"),
            "own",
        ):
            msg = "You are not permitted to add a group as Owner to this product type"
            raise PermissionDenied(msg)

        return data
