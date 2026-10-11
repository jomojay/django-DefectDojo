"""
Configuration UI for the SolarWinds Service Desk integration (INTEGRATIONS_ROADMAP.md 8.2/8.4).

Plain function views, matching the reorganized modules in this tree
(``dojo/group/ui/views.py`` is the most recent reference) rather than the
class-based views ``dojo/jira/views.py`` still uses - those exist only so Pro can
subclass them, which is permanently irrelevant here.

**Authorization is declared centrally**, in
``dojo.authorization.url_permissions.URL_PERMISSIONS``, and applied by
``dojo.authorization.middleware.AuthorizationMiddleware``. Every route here is
gated on the Django *configuration* permissions for its model
(``dojo.view_solarwinds_instance`` and friends) - the same permission level
``dojo/tool_config/ui/views.py`` gets for tool credentials. Note that file has no
decorator or in-view check of its own: its protection is entirely the central
table, which is why there is none here either.

Verified rather than assumed: JIRA gates the equivalent routes to the *same*
permission level but by a different mechanism - explicit
``user_has_configuration_permission(request.user, "dojo.add_jira_instance")``
calls inside each view method (``dojo/jira/views.py:341`` and following), with
nothing in ``URL_PERMISSIONS``. Either mechanism is a real gate; the central table
is the convention the recently reorganized modules use (``dojo/group/ui/views.py``)
and keeps the authorization surface auditable in one file.

The "test connection" action is the same discovery probe in two places: a button on
the create/edit form (works before the row exists, via
``discovery.build_probe_instance``) and a link from the detail page for a saved
configuration. Both render the same read-only panel. Nothing here writes tenant
data - the probe is four ``GET`` lookups plus one filtered incident sample.
"""
import logging

from django.contrib import messages
from django.contrib.admin.utils import NestedObjects
from django.db import DEFAULT_DB_ALIAS
from django.db.models.deletion import RestrictedError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _

from dojo.solarwinds import discovery
from dojo.solarwinds.models import SolarWinds_Instance, SolarWinds_Product
from dojo.solarwinds.ui.forms import (
    DeleteSolarWindsInstanceForm,
    DeleteSolarWindsProductForm,
    SolarWindsInstanceForm,
    SolarWindsProductForm,
)
from dojo.utils import add_breadcrumb, get_setting, redirect_to_return_url_or_else

logger = logging.getLogger(__name__)

# Name of the submit button that means "probe the tenant" instead of "save".
TEST_CONNECTION_ACTION = "test_connection"


# ---------------------------------------------------------------------------
# Instance CRUD
# ---------------------------------------------------------------------------


def solarwinds_instances(request):
    """List every configured Service Desk tenant."""
    instances = SolarWinds_Instance.objects.all()

    add_breadcrumb(title=_("SolarWinds Configurations"), top_level=not len(request.GET), request=request)

    return render(request, "dojo/solarwinds_instances.html", {
        "instances": instances,
    })


def view_solarwinds_instance(request, sid):
    """Configuration detail plus its per-product routing mappings."""
    instance = get_object_or_404(SolarWinds_Instance, id=sid)

    add_breadcrumb(title=_("View SolarWinds Configuration"), top_level=False, request=request)

    return render(request, "dojo/view_solarwinds_instance.html", {
        "instance": instance,
        "mappings": instance.solarwinds_products.select_related("product").all(),
    })


def add_solarwinds_instance(request):
    form = SolarWindsInstanceForm(request.POST or None)
    discovery_result = None

    if request.method == "POST":
        if TEST_CONNECTION_ACTION in request.POST:
            discovery_result = _run_probe(request, form)
        elif form.is_valid():
            instance = form.save()
            messages.add_message(
                request,
                messages.SUCCESS,
                _("SolarWinds configuration added successfully."),
                extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("view_solarwinds_instance", args=(instance.id,)))
        else:
            messages.add_message(
                request,
                messages.ERROR,
                _("SolarWinds configuration was not added."),
                extra_tags="alert-danger")

    add_breadcrumb(title=_("Add SolarWinds Configuration"), top_level=False, request=request)

    return render(request, "dojo/solarwinds_instance_form.html", {
        "name": _("Add SolarWinds Configuration"),
        "form": form,
        "form_action": reverse("add_solarwinds_instance"),
        "discovery": discovery_result,
    })


def edit_solarwinds_instance(request, sid):
    instance = get_object_or_404(SolarWinds_Instance, id=sid)
    form = SolarWindsInstanceForm(request.POST or None, instance=instance)
    discovery_result = None

    if request.method == "POST":
        if TEST_CONNECTION_ACTION in request.POST:
            discovery_result = _run_probe(request, form)
        elif form.is_valid():
            form.save()
            messages.add_message(
                request,
                messages.SUCCESS,
                _("SolarWinds configuration saved successfully."),
                extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("view_solarwinds_instance", args=(instance.id,)))
        else:
            messages.add_message(
                request,
                messages.ERROR,
                _("SolarWinds configuration was not saved."),
                extra_tags="alert-danger")

    add_breadcrumb(title=_("Edit SolarWinds Configuration"), top_level=False, request=request)

    return render(request, "dojo/solarwinds_instance_form.html", {
        "name": _("Edit SolarWinds Configuration"),
        "form": form,
        "instance": instance,
        "form_action": reverse("edit_solarwinds_instance", args=(instance.id,)),
        "discovery": discovery_result,
    })


def delete_solarwinds_instance(request, sid):
    instance = get_object_or_404(SolarWinds_Instance, id=sid)
    form = DeleteSolarWindsInstanceForm(request.POST or None, instance=instance)

    if request.method == "POST" and form.is_valid():
        try:
            instance.delete()
        except RestrictedError as err:
            messages.add_message(
                request,
                messages.WARNING,
                _("Configuration cannot be deleted: %(error)s") % {"error": err},
                extra_tags="alert-warning")
        else:
            messages.add_message(
                request,
                messages.SUCCESS,
                _("SolarWinds configuration and relationships removed."),
                extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("solarwinds_instances"))

    # Previewing the cascade is expensive on large graphs, so it honours the same
    # DELETE_PREVIEW setting every other delete page in this codebase does.
    rels = [_("Previewing the relationships has been disabled."), ""]
    if get_setting("DELETE_PREVIEW"):
        collector = NestedObjects(using=DEFAULT_DB_ALIAS)
        collector.collect([instance])
        rels = collector.nested()

    add_breadcrumb(title=_("Delete SolarWinds Configuration"), top_level=False, request=request)

    return render(request, "dojo/delete_solarwinds_instance.html", {
        "form": form,
        "to_delete": instance,
        "rels": rels,
    })


def test_solarwinds_instance(request, sid):
    """
    Probe a saved tenant and show what it actually has configured.

    Read-only: four lookup GETs plus one filtered incident sample. The states and
    priorities it reports are *observed*, not exhaustive - the template carries the
    caveat, and the model fields stay free text (see ``discovery``'s docstring).
    """
    instance = get_object_or_404(SolarWinds_Instance, id=sid)
    discovery_result = discovery.discover_tenant_configuration(instance)

    if not discovery_result.ok:
        messages.add_message(
            request,
            messages.WARNING,
            _("Some SolarWinds lookups failed: %(sections)s. See the details below.")
            % {"sections": ", ".join(sorted(discovery_result.errors))},
            extra_tags="alert-warning")

    add_breadcrumb(title=_("Test SolarWinds Connection"), top_level=False, request=request)

    return render(request, "dojo/view_solarwinds_instance.html", {
        "instance": instance,
        "mappings": instance.solarwinds_products.select_related("product").all(),
        "discovery": discovery_result,
    })


# ---------------------------------------------------------------------------
# Per-product routing mappings
# ---------------------------------------------------------------------------


def add_solarwinds_product(request, sid):
    instance = get_object_or_404(SolarWinds_Instance, id=sid)
    form = SolarWindsProductForm(request.POST or None, initial={"solarwinds_instance": instance})

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Product mapping added successfully."),
            extra_tags="alert-success")
        return HttpResponseRedirect(reverse("view_solarwinds_instance", args=(instance.id,)))

    if request.method == "POST":
        messages.add_message(
            request,
            messages.ERROR,
            _("Product mapping was not added."),
            extra_tags="alert-danger")

    add_breadcrumb(title=_("Add Product Mapping"), top_level=False, request=request)

    return render(request, "dojo/solarwinds_product_form.html", {
        "name": _("Add Product Mapping"),
        "form": form,
        "instance": instance,
        "form_action": reverse("add_solarwinds_product", args=(instance.id,)),
    })


def edit_solarwinds_product(request, spid):
    mapping = get_object_or_404(SolarWinds_Product, id=spid)
    form = SolarWindsProductForm(request.POST or None, instance=mapping)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Product mapping saved successfully."),
            extra_tags="alert-success")
        return HttpResponseRedirect(reverse("view_solarwinds_instance", args=(mapping.solarwinds_instance_id,)))

    if request.method == "POST":
        messages.add_message(
            request,
            messages.ERROR,
            _("Product mapping was not saved."),
            extra_tags="alert-danger")

    add_breadcrumb(title=_("Edit Product Mapping"), top_level=False, request=request)

    return render(request, "dojo/solarwinds_product_form.html", {
        "name": _("Edit Product Mapping"),
        "form": form,
        "instance": mapping.solarwinds_instance,
        "mapping": mapping,
        "form_action": reverse("edit_solarwinds_product", args=(mapping.id,)),
    })


def delete_solarwinds_product(request, spid):
    mapping = get_object_or_404(SolarWinds_Product, id=spid)
    form = DeleteSolarWindsProductForm(request.POST or None, instance=mapping)
    instance_id = mapping.solarwinds_instance_id

    if request.method == "POST" and form.is_valid():
        mapping.delete()
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Product mapping deleted successfully."),
            extra_tags="alert-success")
        return HttpResponseRedirect(reverse("view_solarwinds_instance", args=(instance_id,)))

    add_breadcrumb(title=_("Delete Product Mapping"), top_level=False, request=request)

    return render(request, "dojo/delete_solarwinds_product.html", {
        "form": form,
        "to_delete": mapping,
    })


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------


def _run_probe(request, form):
    """
    Run the discovery probe for whatever the form currently holds.

    Works on the create form too: the probe runs against an unsaved instance built
    from the typed subdomain and token, so an admin can verify a tenant before
    committing a configuration row. On edit with the token field left blank, the
    stored token is decrypted and used - the ciphertext is never round-tripped
    through the form (R21).

    Returns a ``DiscoveryResult`` or ``None`` when the form is not valid enough to
    probe with; API failures come back *inside* the result, not as exceptions.
    """
    if not form.is_valid():
        messages.add_message(
            request,
            messages.ERROR,
            _("Fix the errors below before testing the connection."),
            extra_tags="alert-danger")
        return None

    token = form.probe_token()
    if not token:
        messages.add_message(
            request,
            messages.ERROR,
            _("Enter an API token to test the connection."),
            extra_tags="alert-danger")
        return None

    probe = discovery.build_probe_instance(
        form.cleaned_data["subdomain"],
        token,
        region=form.cleaned_data.get("region") or "US",
        configuration_name=form.cleaned_data.get("configuration_name") or "(unsaved)",
    )
    result = discovery.discover_tenant_configuration(probe)

    if result.ok:
        messages.add_message(
            request,
            messages.SUCCESS,
            _("Connected to %(host)s.") % {"host": probe.base_url},
            extra_tags="alert-success")
    else:
        messages.add_message(
            request,
            messages.WARNING,
            _("Some SolarWinds lookups failed: %(sections)s. See the details below.")
            % {"sections": ", ".join(sorted(result.errors))},
            extra_tags="alert-warning")

    return result
