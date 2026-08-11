from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Product_Group, Product_Member
from dojo.models import DojoMeta, Product, Product_API_Scan_Configuration


class ProductMetaSerializer(serializers.ModelSerializer):
    class Meta:
        model = DojoMeta
        fields = ("name", "value")


class ProductAPIScanConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_API_Scan_Configuration
        fields = "__all__"


class ProductSerializer(serializers.ModelSerializer):
    findings_count = serializers.SerializerMethodField()
    findings_list = serializers.SerializerMethodField()

    business_criticality = serializers.ChoiceField(choices=Product.BUSINESS_CRITICALITY_CHOICES, allow_blank=True, allow_null=True, required=False)
    platform = serializers.ChoiceField(choices=Product.PLATFORM_CHOICES, allow_blank=True, allow_null=True, required=False)
    lifecycle = serializers.ChoiceField(choices=Product.LIFECYCLE_CHOICES, allow_blank=True, allow_null=True, required=False)
    origin = serializers.ChoiceField(choices=Product.ORIGIN_CHOICES, allow_blank=True, allow_null=True, required=False)

    product_meta = ProductMetaSerializer(read_only=True, many=True)

    class Meta:
        model = Product
        exclude = (
            "tid",
            "updated",
            "async_updating",
        )

    def get_fields(self):
        from dojo.api_v2.serializers import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
            TagListSerializerField,
        )
        fields = super().get_fields()
        fields["tags"] = TagListSerializerField(required=False)
        return fields

    def validate(self, data):
        async_updating = getattr(self.instance, "async_updating", None)
        if async_updating:
            new_sla_config = data.get("sla_configuration", None)
            old_sla_config = getattr(self.instance, "sla_configuration", None)
            if new_sla_config and old_sla_config and new_sla_config != old_sla_config:
                msg = "Finding SLA expiration dates are currently being recalculated. The SLA configuration for this product cannot be changed until the calculation is complete."
                raise serializers.ValidationError(msg)
        return data

    def get_findings_count(self, obj) -> int:
        return obj.findings_count

    # TODO: maybe extend_schema_field is needed here?
    def get_findings_list(self, obj) -> list[int]:
        return obj.open_findings_list()


# ---------------------------------------------------------------------------
# Role-grant rows on a Product (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from the pre-3.0 dojo/api_v2/serializers.py (commit db1932c9e). Both
# validate() bodies are preserved wholesale: with no unique constraint on
# (product, user) / (product, group) - see roadmap §7.2, migration 0279 is a
# separate follow-up - the duplicate guard below is the only thing preventing
# two rows for the same principal, where the highest role would silently win.
#
# Permission retarget: Product_Manage_Members / Product_Group_Add -> "manage"
# (both were Maintainer+), *_Add_Owner -> "own" (Owner only). See the block
# comment above the UserHas*MemberPermission classes in
# dojo/authorization/api_permissions.py for why "manage" and not the mechanical
# permission_to_action() result.
# ---------------------------------------------------------------------------


class ProductMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_Member
        fields = "__all__"

    def validate(self, data):
        if (
            self.instance is not None
            and data.get("product") != self.instance.product
            and not user_has_permission(
                self.context["request"].user,
                data.get("product"),
                "manage",
            )
        ):
            msg = "You are not permitted to add a member to this product"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or data.get("product") != self.instance.product
            or data.get("user") != self.instance.user
        ):
            members = Product_Member.objects.filter(
                product=data.get("product"), user=data.get("user"),
            )
            if members.count() > 0:
                msg = "Product_Member already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            data.get("product"),
            "own",
        ):
            msg = "You are not permitted to add a member as Owner to this product"
            raise PermissionDenied(msg)

        return data


class ProductGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product_Group
        fields = "__all__"

    def validate(self, data):
        if (
            self.instance is not None
            and data.get("product") != self.instance.product
            and not user_has_permission(
                self.context["request"].user,
                data.get("product"),
                "manage",
            )
        ):
            msg = "You are not permitted to add a group to this product"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or data.get("product") != self.instance.product
            or data.get("group") != self.instance.group
        ):
            members = Product_Group.objects.filter(
                product=data.get("product"), group=data.get("group"),
            )
            if members.count() > 0:
                msg = "Product_Group already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            data.get("product"),
            "own",
        ):
            msg = "You are not permitted to add a group as Owner to this product"
            raise PermissionDenied(msg)

        return data
