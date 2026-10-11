"""
DRF serializers for the SolarWinds Service Desk configuration.

``api_token`` is ``write_only`` and is written **only** through
``SolarWinds_Instance.set_api_token()`` (R21). Both halves of that matter and for
different reasons:

* ``write_only`` keeps the stored ciphertext out of every ``GET``/``LIST``
  response. Serving it would leak a credential to anyone with the read
  permission, and an integration that round-tripped it back would double-encrypt
  it - the exact failure R21 describes.
* Routing the write through ``set_api_token()`` is what encrypts it. A plain
  ``ModelSerializer`` would assign the plaintext straight onto the model field and
  store an unencrypted credential, which is the JIRA anti-pattern (R11) this
  module exists to avoid.

``partial_update`` without ``api_token`` leaves the stored value untouched, the
same blank-means-unchanged contract the UI form offers.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product


class SolarWindsInstanceSerializer(serializers.ModelSerializer):

    """Tenant configuration. The token goes in and never comes back out."""

    api_token = serializers.CharField(
        write_only=True,
        required=True,
        help_text=(
            "API token of a dedicated service-account admin user. Stored encrypted; "
            "never returned by the API. Omit on PATCH to keep the existing token."
        ),
    )

    class Meta:
        model = SolarWinds_Instance
        fields = [
            "id",
            "configuration_name",
            "subdomain",
            "region",
            "api_token",
            "service_account_email",
            "default_site_id",
            "default_department_id",
            "default_category",
            "open_state",
            "resolved_states",
            "severity_priority_map",
            "auto_push_severities",
            "push_enabled",
            "poll_enabled",
        ]

    def validate(self, data):
        # Model.clean() carries the severity-map and resolved-states rules; running
        # it here means the API cannot create a configuration the UI would reject.
        # Its Django ValidationError is translated, not propagated: DRF only knows
        # how to turn its own exception into a 400.
        candidate = SolarWinds_Instance(**{**_instance_attrs(self.instance), **data})
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return data

    def create(self, validated_data):
        token = validated_data.pop("api_token")
        instance = SolarWinds_Instance(**validated_data)
        instance.set_api_token(token)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        # Absent means unchanged; a supplied value is re-encrypted. It is never
        # assigned to the field directly - that would store plaintext.
        token = validated_data.pop("api_token", None)
        for attribute, value in validated_data.items():
            setattr(instance, attribute, value)
        if token:
            instance.set_api_token(token)
        instance.save()
        return instance


def _instance_attrs(instance):
    """Current field values of ``instance`` (or ``{}``), for validating a partial update."""
    if instance is None:
        return {}
    # Only the fields clean() actually reads. push_enabled is one of them: the
    # empty-map check is conditional on it, so a PATCH that flips it on without
    # sending a map has to be validated against the stored map. auto_push_severities
    # is here for the mirror-image case - a PATCH that narrows severity_priority_map
    # has to be checked against the *stored* auto-push list, or it can strand a
    # severity that fires automatically with no priority label to fire it under.
    return {
        "open_state": instance.open_state,
        "resolved_states": instance.resolved_states,
        "severity_priority_map": instance.severity_priority_map,
        "auto_push_severities": instance.auto_push_severities,
        "push_enabled": instance.push_enabled,
    }


class SolarWindsProductSerializer(serializers.ModelSerializer):

    """Per-product routing overrides."""

    class Meta:
        model = SolarWinds_Product
        fields = [
            "id",
            "solarwinds_instance",
            "product",
            "site_id",
            "department_id",
            "group_assignee_id",
            "push_all_findings",
        ]

    def validate(self, data):
        """
        Reject a duplicate (instance, product) mapping with a 400 rather than a 500.

        The model carries a ``UniqueConstraint`` for this, but whether DRF derives a
        validator from ``Meta.constraints`` varies by version - and when it does not,
        the constraint surfaces as an ``IntegrityError`` from the database, which is
        an unhandled 500. Checking here makes the behaviour version-independent.
        """
        solarwinds_instance = data.get("solarwinds_instance") or getattr(self.instance, "solarwinds_instance", None)
        product = data.get("product") or getattr(self.instance, "product", None)

        if solarwinds_instance and product:
            duplicates = SolarWinds_Product.objects.filter(
                solarwinds_instance=solarwinds_instance, product=product,
            )
            if self.instance is not None:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                msg = "This product already has a mapping on this SolarWinds configuration."
                raise serializers.ValidationError({"product": [msg]})

        return data
