from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from dojo.api_v2.serializers import ProductMetaSerializer, TagListSerializerField
from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Product_Group, Product_Member
from dojo.models import (
    Dojo_User,
    Product,
    Product_API_Scan_Configuration,
)
from dojo.organization.api.serializers import RelatedOrganizationField
from dojo.product.queries import get_authorized_products


class RelatedAssetField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return get_authorized_products("view")


class AssetAPIScanConfigurationSerializer(serializers.ModelSerializer):
    asset = RelatedAssetField(source="product")

    class Meta:
        model = Product_API_Scan_Configuration
        exclude = ("product",)


class AssetSerializer(serializers.ModelSerializer):
    findings_count = serializers.SerializerMethodField()
    findings_list = serializers.SerializerMethodField()

    tags = TagListSerializerField(required=False)

    # V3 fields
    asset_meta = ProductMetaSerializer(source="product_meta", read_only=True, many=True)
    organization = RelatedOrganizationField(source="prod_type")
    asset_numeric_grade = serializers.IntegerField(source="prod_numeric_grade", required=False, allow_null=True)
    enable_asset_tag_inheritance = serializers.BooleanField(source="enable_product_tag_inheritance", required=False, default=False)
    asset_managers = serializers.PrimaryKeyRelatedField(
        source="product_manager",
        queryset=Dojo_User.objects.exclude(is_active=False),
        required=False, allow_null=True,
    )
    business_criticality = serializers.ChoiceField(choices=Product.BUSINESS_CRITICALITY_CHOICES, allow_blank=True, allow_null=True, required=False)
    platform = serializers.ChoiceField(choices=Product.PLATFORM_CHOICES, allow_blank=True, allow_null=True, required=False)
    lifecycle = serializers.ChoiceField(choices=Product.LIFECYCLE_CHOICES, allow_blank=True, allow_null=True, required=False)
    origin = serializers.ChoiceField(choices=Product.ORIGIN_CHOICES, allow_blank=True, allow_null=True, required=False)

    class Meta:
        model = Product
        exclude = (
            "tid",
            "updated",
            "async_updating",
            # Below here excluded for V3 migration
            "prod_type",
            "prod_numeric_grade",
            "enable_product_tag_inheritance",
            "product_manager",
        )

    def validate(self, data):
        async_updating = getattr(self.instance, "async_updating", None)
        if async_updating:
            new_sla_config = data.get("sla_configuration", None)
            old_sla_config = getattr(self.instance, "sla_configuration", None)
            if new_sla_config and old_sla_config and new_sla_config != old_sla_config:
                msg = "Finding SLA expiration dates are currently being recalculated. The SLA configuration for this asset cannot be changed until the calculation is complete."
                raise serializers.ValidationError(msg)
        return data

    def get_findings_count(self, obj) -> int:
        return obj.findings_count

    # TODO: maybe extend_schema_field is needed here?
    def get_findings_list(self, obj) -> list[int]:
        return obj.open_findings_list()


# ---------------------------------------------------------------------------
# Role-grant rows on an Asset (= Product) - the v3 twin of
# dojo/product/api/serializer.py's member/group serializers
# (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from db1932c9e:dojo/asset/api/serializers.py. Same rows, same
# invariants; the payload key is "asset" (sourced from product) and
# RelatedAssetField restricts the choosable destinations to the requester's
# authorized products - a narrowing the Product-named twin deliberately does
# not have (it ships fields="__all__", exactly as it did pre-3.0). The
# destination is authorized either way by UserHasProductMemberPermission /
# UserHasAssetMemberPermission before the serializer runs; the difference is
# only whether an unauthorized destination fails as 400 or 403.
#
# ONE CORRECTION to the ported source, and it is load-bearing. The original
# read the destination out of validate()'s `data` as data.get("asset").
# `data` is validated_data, which DRF keys by a field's **source**, not its
# declared name - so with asset = RelatedAssetField(source="product"), that
# lookup was always None. The duplicate guard therefore filtered
# Product_Member.objects.filter(product=None, ...), matched nothing, and never
# fired: POSTing a grant that already existed returned 201 through the Asset
# routes while the identical POST through the Product routes returned 400.
# The Owner check had the same defect (user_has_permission(user, None, "own")
# is always False for a non-superuser, always True for a superuser, i.e. it
# checked nothing). Reading data["product"] fixes both. The pre-3.0 author had
# clearly hit this once already: the Organization member serializer carries a
# data.get("organization", data.get("product_type")) fallback on exactly the
# line where the fallback is what made the guard work.
# ---------------------------------------------------------------------------


class AssetMemberSerializer(serializers.ModelSerializer):
    asset = RelatedAssetField(source="product")

    class Meta:
        model = Product_Member
        exclude = ("product",)

    def validate(self, data):
        # `data` is validated_data: keyed by source ("product"), not by the
        # declared field name ("asset"). See the note above this class.
        asset = data.get("product")

        if (
            self.instance is not None
            and asset != self.instance.product
            and not user_has_permission(
                self.context["request"].user,
                asset,
                "manage",
            )
        ):
            msg = "You are not permitted to add a member to this Asset"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or asset != self.instance.product
            or data.get("user") != self.instance.user
        ):
            members = Product_Member.objects.filter(
                product=asset, user=data.get("user"),
            )
            if members.count() > 0:
                msg = "Asset Member already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            asset,
            "own",
        ):
            msg = "You are not permitted to add a member as Owner to this Asset"
            raise PermissionDenied(msg)

        return data


class AssetGroupSerializer(serializers.ModelSerializer):
    asset = RelatedAssetField(source="product")

    class Meta:
        model = Product_Group
        exclude = ("product",)

    def validate(self, data):
        asset = data.get("product")

        if (
            self.instance is not None
            and asset != self.instance.product
            and not user_has_permission(
                self.context["request"].user,
                asset,
                "manage",
            )
        ):
            msg = "You are not permitted to add a group to this Asset"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or asset != self.instance.product
            or data.get("group") != self.instance.group
        ):
            members = Product_Group.objects.filter(
                product=asset, group=data.get("group"),
            )
            if members.count() > 0:
                msg = "Asset Group already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            asset,
            "own",
        ):
            msg = "You are not permitted to add a group as Owner to this Asset"
            raise PermissionDenied(msg)

        return data
