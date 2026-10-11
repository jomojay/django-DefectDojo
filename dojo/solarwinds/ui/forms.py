"""
Forms for the SolarWinds Service Desk integration.

Two unrelated audiences live here. Most of the module is the **configuration** surface
(a config admin setting up a tenant), and everything below the delete forms is the
**per-finding** surface (:class:`SolarWindsFindingForm` and :func:`build_finding_form`,
the manual push checkbox offered on Add/Edit Finding). They share a file because they
share a module, not because they share a concern - JIRA splits the equivalent pair
across ``dojo/jira/forms.py`` by convention only.

**The load-bearing part of this module is how ``api_token`` is handled (R21).**
``SolarWinds_Instance.api_token`` stores ciphertext, so a plain ``ModelForm`` over
that field is a credential-destroying bug: it renders the ciphertext into the page,
takes it back on submit, and runs ``dojo_crypto_encrypt`` over an already-encrypted
string. The token then decrypts to garbage and every API call 401s - indistinguishable
from the token rotation of R6, and only discovered in production.

Two rules follow, and both are structural rather than conventional:

1. ``api_token`` is **excluded from ``Meta``** and declared as a plain (non-model)
   form field. That is stronger than merely overriding the widget: ``ModelForm``'s
   ``construct_instance()`` only copies fields that are in ``Meta``, so with the
   field excluded there is no code path - present or future - by which a blank or
   rendered value can overwrite the stored ciphertext.
2. The stored value is never rendered (``PasswordInput`` with ``render_value=False``),
   and blank on edit means "keep what is stored". ``set_api_token()`` is called only
   when the admin actually typed something, mirroring
   ``dojo/jira/forms.py``'s ``AdvancedJIRAForm`` blank-means-unchanged behaviour
   (``__init__``/``clean`` at lines 79-87 of that file as of this writing - the
   roadmap's "86-93" citation had drifted) - except JIRA persists its credential in
   plaintext and can therefore copy it back through the form, which this must not do.

The rest is ordinary. State / priority / category fields stay free text on purpose:
the API has no lookup endpoint for the first two (8.1), so a dropdown here would be
a lie. ``dojo/solarwinds/discovery.py`` feeds the same page a read-only panel of what
the tenant actually uses, which is as close to validation as this API permits.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product, SolarWinds_Ticket


class SolarWindsInstanceForm(forms.ModelForm):

    """Create / edit a SolarWinds Service Desk tenant configuration."""

    # Both JSON fields are optional *at the form layer* and default to their empty
    # container. Django treats `{}` and `[]` as empty values, so a required JSONField
    # rejects a literally-typed `{}` with "This field is required" - and a push-only
    # configuration has no business being forced to invent resolved states. What is
    # and is not a valid combination is decided by SolarWinds_Instance.clean()
    # instead, which both this form and the REST serializer run (R22).
    resolved_states = forms.JSONField(
        required=False,
        label=SolarWinds_Instance._meta.get_field("resolved_states").verbose_name,
        help_text=SolarWinds_Instance._meta.get_field("resolved_states").help_text,
    )
    severity_priority_map = forms.JSONField(
        required=False,
        label=SolarWinds_Instance._meta.get_field("severity_priority_map").verbose_name,
        help_text=SolarWinds_Instance._meta.get_field("severity_priority_map").help_text,
    )
    auto_push_severities = forms.JSONField(
        required=False,
        label=SolarWinds_Instance._meta.get_field("auto_push_severities").verbose_name,
        help_text=SolarWinds_Instance._meta.get_field("auto_push_severities").help_text,
    )

    api_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label=_("API token"),
        help_text=_(
            "API token of a dedicated service-account admin user. Stored encrypted and "
            "never displayed. Leave blank when editing to keep the existing token. "
            "Regenerating a token in Service Desk invalidates every prior token issued "
            "to that user, so do not use a shared personal account.",
        ),
    )

    class Meta:
        model = SolarWinds_Instance
        # api_token is deliberately NOT here - see the module docstring (R21).
        fields = [
            "configuration_name",
            "subdomain",
            "region",
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

    def clean_resolved_states(self):
        # None would be written to a non-nullable JSONField; the column's own default
        # is an empty list, so match it.
        return self.cleaned_data.get("resolved_states") or []

    def clean_severity_priority_map(self):
        return self.cleaned_data.get("severity_priority_map") or {}

    def clean_auto_push_severities(self):
        # Empty is a real, and the default, configuration: "nothing fires on its own".
        # The subset-of-severity_priority_map rule lives in the model's clean(), so the
        # REST serializer and the Django admin get it too.
        return self.cleaned_data.get("auto_push_severities") or []

    def clean_api_token(self):
        """Require a token on create; treat blank on edit as "leave it alone"."""
        token = (self.cleaned_data.get("api_token") or "").strip()
        if not token and not self.instance.pk:
            msg = _("An API token is required to create a configuration.")
            raise forms.ValidationError(msg, code="required")
        return token

    def probe_token(self):
        """
        Return the plaintext token a connection test should use.

        Either what the admin just typed, or - when the field was left blank on an
        edit - the stored one, decrypted. Never round-trips ciphertext through the
        form itself.
        """
        typed = (self.cleaned_data.get("api_token") or "").strip() if hasattr(self, "cleaned_data") else ""
        if typed:
            return typed
        if self.instance.pk:
            return self.instance.get_api_token()
        return ""

    def save(self, commit=True):  # noqa: FBT002 -- Django's own signature
        instance = super().save(commit=False)
        # Only re-encrypt when the admin supplied a new token. A blank field leaves
        # instance.api_token untouched, which is the whole point of R21's test:
        # saving twice without retyping must leave the ciphertext byte-identical.
        if token := (self.cleaned_data.get("api_token") or "").strip():
            instance.set_api_token(token)
        if commit:
            instance.save()
        return instance


class DeleteSolarWindsInstanceForm(forms.ModelForm):

    """Confirm-delete form; matches ``DeleteJIRAInstanceForm``'s hidden-id shape."""

    id = forms.IntegerField(required=True, widget=forms.widgets.HiddenInput())

    class Meta:
        model = SolarWinds_Instance
        fields = ["id"]


class SolarWindsProductForm(forms.ModelForm):

    """
    Per-product routing overrides, analogous to configuring a ``JIRA_Project``.

    Every override is blank-means-inherit: a blank ``site_id`` falls back to the
    instance default rather than clearing it (``services._override``).
    """

    class Meta:
        model = SolarWinds_Product
        fields = [
            "solarwinds_instance",
            "product",
            "site_id",
            "department_id",
            "group_assignee_id",
            "push_all_findings",
        ]

    def clean(self):
        cleaned_data = super().clean()
        instance = cleaned_data.get("solarwinds_instance")
        product = cleaned_data.get("product")

        if instance and product:
            duplicates = SolarWinds_Product.objects.filter(solarwinds_instance=instance, product=product)
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                # There is a DB UniqueConstraint too; this exists so the admin gets a
                # field-level message instead of the constraint's opaque wording.
                msg = _("This product already has a mapping on this SolarWinds configuration.")
                raise forms.ValidationError(msg, code="duplicate_mapping")

        return cleaned_data


class DeleteSolarWindsProductForm(forms.ModelForm):

    """Confirm-delete form for a per-product mapping."""

    id = forms.IntegerField(required=True, widget=forms.widgets.HiddenInput())

    class Meta:
        model = SolarWinds_Product
        fields = ["id"]


class SolarWindsFindingForm(forms.Form):

    """
    The per-finding "push this one now" checkbox, offered on add/edit finding.

    The manual half of the trigger, and the counterpart to ``auto_push_severities``:
    the configured severities file themselves, everything else in
    ``severity_priority_map`` is reachable only through this box. Modelled on
    ``dojo/jira/forms.py``'s ``JIRAFindingForm`` in spirit, not in size - JIRA's also
    owns issue linking/unlinking and finding-group routing, none of which exists here
    (there is no outbound "update an existing incident" path at all), so this form is
    one field.

    Two states are shown rather than hidden, both because a checkbox that silently does
    nothing is worse than no checkbox:

    * ``push_all`` - this finding's severity is in ``auto_push_severities`` on a
      ``push_all_findings`` product, so it is going to be filed regardless. Checked and
      disabled, the same treatment and for the same reason as JIRA's push-all case.
    * ``existing_incident`` - a ticket already exists. This integration has no outbound
      update path, so re-checking the box would be a no-op swallowed by
      ``helper.has_ticket``. Disabled, with the incident named.

    The form is only *constructed* when a manual push is actually available
    (``helper.is_manual_push_available`` on edit, "the product has a push-enabled
    mapping" on add, where the severity is not yet known). ``finding_form`` is the
    add/edit form on the same page; it is how the severity typed into *this* submission
    - not the stored one - gets validated against the severity map, mirroring how
    ``JIRAFindingForm.clean`` reaches into ``self.finding_form`` for active/verified.
    """

    push_to_solarwinds = forms.BooleanField(
        required=False,
        label=_("Push to SolarWinds"),
        help_text=_(
            "Open a SolarWinds Service Desk incident for this finding. Leave unchecked "
            "to keep it in DefectDojo only.",
        ),
    )

    prefix = "solarwindsform"

    def __init__(self, *args, push_all=False, instance=None, solarwinds_instance=None,
                 existing_incident="", finding_form=None, **kwargs):
        self.push_all = push_all
        self.instance = instance
        self.solarwinds_instance = solarwinds_instance
        self.existing_incident = existing_incident
        self.finding_form = finding_form
        super().__init__(*args, **kwargs)

        if self.push_all:
            self.fields["push_to_solarwinds"].help_text = _(
                "This finding's severity is set to push automatically for this product, "
                "so an incident will be opened whether or not this is checked. Change "
                "the auto-push severities on the SolarWinds configuration, or turn off "
                "'Push all findings' for this product, to stop that.",
            )
            self.fields["push_to_solarwinds"].widget.attrs["checked"] = "checked"
            self.fields["push_to_solarwinds"].disabled = True
        elif self.existing_incident:
            self.fields["push_to_solarwinds"].help_text = _(
                "Already linked to incident %(incident)s. This integration does not "
                "update an incident it has already opened, so there is nothing further "
                "to push.",
            ) % {"incident": self.existing_incident}
            self.fields["push_to_solarwinds"].widget.attrs["checked"] = "checked"
            self.fields["push_to_solarwinds"].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("push_to_solarwinds"):
            return cleaned_data

        # Validate against the severity being submitted, not the one currently stored:
        # on add-finding there is no stored severity at all, and on edit the user may be
        # changing it in the same submission. Without this an Info finding accepts the
        # checkbox, dispatches a task and is dropped by the unmapped-severity rule
        # (8.5) with nothing said - which is exactly the "it did nothing and did not
        # tell me" outcome this form exists to avoid.
        severity = self.finding_form["severity"].value() if self.finding_form else None
        if self.solarwinds_instance is not None and severity and not self.solarwinds_instance.priority_for_severity(severity):
            self.add_error(
                "push_to_solarwinds",
                forms.ValidationError(
                    _(
                        "%(severity)s findings have no SolarWinds priority mapping on "
                        "'%(config)s', so they cannot be pushed. Map that severity on the "
                        "SolarWinds configuration first.",
                    ) % {"severity": severity, "config": self.solarwinds_instance},
                    code="unmapped_severity",
                ),
            )

        return cleaned_data


def build_finding_form(request, *, finding=None, product=None, finding_form=None):
    """
    Return the :class:`SolarWindsFindingForm` for an add/edit finding page, or ``None``.

    ``None`` means "render no SolarWinds section at all": the deployment has no
    push-enabled mapping, this product has none, or this finding's severity has no
    priority label. JIRA's templates make the same call through ``{% if jform %}``,
    driven by ``jira_services.get_project(...)`` returning ``None``; this is the
    equivalent gate, just with a stricter question behind it, because SolarWinds can
    also be configured in a way that makes one particular finding unpushable while the
    product as a whole is wired up.

    Two different questions, because the two pages know different things:

    * **edit** (``finding`` given) - the severity is known, so eligibility, the
      auto-push state and any existing incident can all be resolved exactly.
    * **add** (``product`` given) - the severity is being typed on this very page, so
      the most that can be said is "this product can push". The box is therefore
      offered enabled even for a severity that will auto-push anyway; checking it then
      is redundant, not harmful, because ``Finding.save()`` dispatches at most one push
      either way. A severity with no mapping at all is caught by the form's ``clean()``
      against ``finding_form`` rather than by hiding the field.
    """
    from dojo.solarwinds import helper as solarwinds_helper  # noqa: PLC0415 -- lazy import, avoids circular dependency

    if not solarwinds_helper.any_push_mapping_exists():
        return None

    existing_incident = ""
    if finding is not None and finding.id:
        mappings = solarwinds_helper.get_product_mappings(finding)
        if not solarwinds_helper.is_manual_push_available(finding, mappings=mappings):
            return None
        solarwinds_instance = solarwinds_helper.resolve_push_target(
            finding, push_requested=True, mappings=mappings,
        )[0]
        push_all = solarwinds_helper.is_push_all_findings_auto(finding, mappings=mappings)
        if (ticket := SolarWinds_Ticket.objects.filter(finding=finding).first()) is not None:
            existing_incident = ticket.incident_number or ticket.incident_id
    else:
        mappings = solarwinds_helper.get_mappings_for_product(product)
        if not mappings:
            return None
        solarwinds_instance = mappings[0].solarwinds_instance
        push_all = False

    args = [request.POST] if request.method == "POST" else []
    return SolarWindsFindingForm(
        *args,
        push_all=push_all,
        instance=finding,
        solarwinds_instance=solarwinds_instance,
        existing_incident=existing_incident,
        finding_form=finding_form,
    )
