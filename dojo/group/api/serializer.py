"""
Serializers for ``Dojo_Group`` / ``Dojo_Group_Member``
(INTEGRATIONS_ROADMAP.md §7.7).

Ported from the pre-3.0 ``dojo/api_v2/serializers.py`` (commit ``db1932c9e``),
with every ``Permissions.X`` reference retargeted onto this fork's ``Action``
strings:

  * ``Permissions.Group_Manage_Members`` → ``"manage"``
  * ``Permissions.Group_Add_Owner``      → ``"own"``

Both ``validate()`` bodies are preserved wholesale. They are the *only*
enforcement of two invariants — no duplicate ``(group, user)`` row, and a group
must keep at least one Owner — because no membership table carries a DB
uniqueness constraint (INTEGRATIONS_ROADMAP.md §7.2; migration ``0279`` is a
deliberately separate follow-up).
"""
from django.contrib.auth.models import Group, Permission
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from dojo.authorization.authorization import user_has_permission
from dojo.authorization.models import Dojo_Group, Dojo_Group_Member
from dojo.group.signals import get_auth_group_name
from dojo.user.utils import get_configuration_permissions_codenames


class DojoGroupSerializer(serializers.ModelSerializer):
    configuration_permissions = serializers.PrimaryKeyRelatedField(
        allow_null=True,
        queryset=Permission.objects.filter(
            codename__in=get_configuration_permissions_codenames(),
        ),
        many=True,
        required=False,
        source="auth_group.permissions",
    )

    class Meta:
        model = Dojo_Group
        exclude = ("auth_group",)

    def to_representation(self, instance):
        if not instance.auth_group:
            # Self-heal a group whose mirrored auth.Group went missing: the
            # mirror is what makes configuration permissions work for group
            # members at all (dojo/group/signals.py).
            auth_group = Group(name=get_auth_group_name(instance))
            auth_group.save()
            instance.auth_group = auth_group
            members = instance.users.all()
            for member in members:
                auth_group.user_set.add(member)
            instance.save()
        ret = super().to_representation(instance)
        # This will show only "configuration_permissions" even if user has also
        # other permissions
        all_permissions = set(ret["configuration_permissions"])
        allowed_configuration_permissions = set(
            self.fields[
                "configuration_permissions"
            ].child_relation.queryset.values_list("id", flat=True),
        )
        ret["configuration_permissions"] = list(
            all_permissions.intersection(allowed_configuration_permissions),
        )

        return ret

    def create(self, validated_data):
        new_configuration_permissions = None
        if (
            "auth_group" in validated_data
            and "permissions" in validated_data["auth_group"]
        ):  # This field was renamed from "configuration_permissions" in the meantime
            new_configuration_permissions = set(
                validated_data.pop("auth_group")["permissions"],
            )

        instance = super().create(validated_data)

        # This will update only Permissions from category
        # "configuration_permissions". There are no other Permissions.
        if new_configuration_permissions:
            instance.auth_group.permissions.set(new_configuration_permissions)

        return instance

    def update(self, instance, validated_data):
        permissions_in_payload = None
        new_configuration_permissions = None
        if (
            "auth_group" in validated_data
            and "permissions" in validated_data["auth_group"]
        ):  # This field was renamed from "configuration_permissions" in the meantime
            permissions_in_payload = validated_data.pop("auth_group")["permissions"]
            new_configuration_permissions = set(permissions_in_payload)

        instance = super().update(instance, validated_data)

        # This will update only Permissions from category
        # "configuration_permissions". Others will be untouched
        if new_configuration_permissions:
            allowed_configuration_permissions = set(
                self.fields[
                    "configuration_permissions"
                ].child_relation.queryset.all(),
            )
            non_configuration_permissions = (
                set(instance.auth_group.permissions.all())
                - allowed_configuration_permissions
            )
            new_permissions = non_configuration_permissions.union(
                new_configuration_permissions,
            )
            instance.auth_group.permissions.set(new_permissions)

        # Clear all configuration permissions if an empty list is provided
        if isinstance(permissions_in_payload, list) and len(permissions_in_payload) == 0:
            instance.auth_group.permissions.clear()

        return instance


class DojoGroupMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dojo_Group_Member
        fields = "__all__"

    def validate(self, data):
        if (
            self.instance is not None
            and data.get("group") != self.instance.group
            and not user_has_permission(
                self.context["request"].user,
                data.get("group"),
                "manage",
            )
        ):
            msg = "You are not permitted to add a user to this group"
            raise PermissionDenied(msg)

        if (
            self.instance is None
            or data.get("group") != self.instance.group
            or data.get("user") != self.instance.user
        ):
            members = Dojo_Group_Member.objects.filter(
                group=data.get("group"), user=data.get("user"),
            )
            if members.count() > 0:
                msg = "Dojo_Group_Member already exists"
                raise ValidationError(msg)

        if self.instance is not None and not data.get("role").is_owner:
            owners = (
                Dojo_Group_Member.objects.filter(
                    group=data.get("group"), role__is_owner=True,
                )
                .exclude(id=self.instance.id)
                .count()
            )
            if owners < 1:
                msg = "There must be at least one owner"
                raise ValidationError(msg)

        if data.get("role").is_owner and not user_has_permission(
            self.context["request"].user,
            data.get("group"),
            "own",
        ):
            msg = "You are not permitted to add a user as Owner to this group"
            raise PermissionDenied(msg)

        return data
