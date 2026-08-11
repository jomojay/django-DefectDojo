from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Product_Type_Group, Product_Type_Member
from dojo.models import Product_Type
from dojo.product_type.queries import get_authorized_product_types


class RelatedOrganizationField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return get_authorized_product_types("view")


class OrganizationSerializer(serializers.ModelSerializer):
    critical_asset = serializers.BooleanField(source="critical_product", default=False)
    key_asset = serializers.BooleanField(source="key_product", default=False)

    class Meta:
        model = Product_Type
        exclude = ("critical_product", "key_product")


# ---------------------------------------------------------------------------
# Role-grant rows on an Organization (= Product_Type) - the v3 twin of
# dojo/product_type/api/serializer.py's member/group serializers
# (INTEGRATIONS_ROADMAP.md §7.7).
#
# Ported from db1932c9e:dojo/organization/api/serializers.py. The parallel
# surface is a real, permanent second API over the same rows, not an alias:
# the payload key is "organization" (sourced from product_type) and the
# RelatedOrganizationField restricts the choosable destinations to the
# requester's authorized product types.
#
# ONE CORRECTION to the ported source, and it is load-bearing. The original
# read the destination out of validate()'s `data` as data.get("organization").
# `data` is validated_data, which DRF keys by a field's **source**, not its
# declared name - so with organization = RelatedOrganizationField(
# source="product_type"), that lookup was always None: the duplicate guard
# filtered Product_Type_Member.objects.filter(product_type=None, ...), matched
# nothing, and never fired, and the Owner check tested permission on None.
# Reading data["product_type"] fixes both.
#
# The pre-3.0 author had evidently already hit this: the last-Owner lookup in
# the member serializer was written data.get("organization",
# data.get("product_type")), and the fallback arm is the only reason THAT
# guard worked while its two neighbours silently did not. With the lookup
# corrected the fallback is redundant, so it is gone rather than left as a
# misleading hint that "organization" is ever a valid key here.
# ---------------------------------------------------------------------------


class OrganizationMemberSerializer(serializers.ModelSerializer):
    organization = RelatedOrganizationField(source="product_type")

    class Meta:
        model = Product_Type_Member
        exclude = ("product_type",)

    def validate(self, data):
        # `data` is validated_data: keyed by source ("product_type"), not by
        # the declared field name ("organization"). See the note above.
        organization = data.get("product_type")

        if (
            self.instance is not None
            and organization != self.instance.product_type
            and not user_has_permission(
                self.context["request"].user,
                organization,
                "manage",
            )
        ):
            msg = "You are not permitted to add a member to this Organization"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or organization != self.instance.product_type
            or data.get("user") != self.instance.user
        ):
            members = Product_Type_Member.objects.filter(
                product_type=organization, user=data.get("user"),
            )
            if members.count() > 0:
                msg = "Organization Member already exists"
                raise ValidationError(msg)

        if self.instance is not None and not data.get("role").is_owner:
            owners = (
                Product_Type_Member.objects.filter(
                    product_type=organization, role__is_owner=True,
                )
                .exclude(id=self.instance.id)
                .count()
            )
            if owners < 1:
                msg = "There must be at least one owner"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            organization,
            "own",
        ):
            msg = "You are not permitted to add a member as Owner to this Organization"
            raise PermissionDenied(msg)

        return data


class OrganizationGroupSerializer(serializers.ModelSerializer):
    organization = RelatedOrganizationField(source="product_type")

    class Meta:
        model = Product_Type_Group
        exclude = ("product_type",)

    def validate(self, data):
        organization = data.get("product_type")

        if (
            self.instance is not None
            and organization != self.instance.product_type
            and not user_has_permission(
                self.context["request"].user,
                organization,
                "manage",
            )
        ):
            msg = "You are not permitted to add a group to this Organization"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or organization != self.instance.product_type
            or data.get("group") != self.instance.group
        ):
            members = Product_Type_Group.objects.filter(
                product_type=organization, group=data.get("group"),
            )
            if members.count() > 0:
                msg = "Organization Group already exists"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            organization,
            "own",
        ):
            msg = "You are not permitted to add a group as Owner to this Organization"
            raise PermissionDenied(msg)

        return data
