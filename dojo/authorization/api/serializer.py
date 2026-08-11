"""
Serializers for ``Role`` and ``Global_Role`` (INTEGRATIONS_ROADMAP.md §7.7).

Ported from the pre-3.0 ``dojo/api_v2/serializers.py`` (commit ``db1932c9e``).
``RoleSerializer`` is unchanged; ``GlobalRoleSerializer`` keeps its
``validate()`` body verbatim — it is the only enforcement of the
"exactly one of user / group" invariant, since ``Global_Role`` carries two
nullable OneToOneFields and no DB-level check constraint.
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from dojo.authorization.models import Global_Role, Role


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = "__all__"


class GlobalRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Global_Role
        fields = "__all__"

    def validate(self, data):
        user = None
        group = None

        if self.instance is not None:
            user = self.instance.user
            group = self.instance.group

        if "user" in data:
            user = data.get("user")
        if "group" in data:
            group = data.get("group")

        if user is None and group is None:
            msg = "Global_Role must have either user or group"
            raise ValidationError(msg)
        if user is not None and group is not None:
            msg = "Global_Role cannot have both user and group"
            raise ValidationError(msg)

        return data
