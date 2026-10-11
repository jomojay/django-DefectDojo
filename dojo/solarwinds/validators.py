"""
Field-level validators for the SolarWinds Service Desk integration.

The one that matters is :func:`validate_subdomain`. ``SolarWinds_Instance.subdomain``
is interpolated straight into the API base URL
(``https://{subdomain}.samanage.com`` - see INTEGRATIONS_ROADMAP.md 8.1/8.3), so an
unvalidated value is a request-forgery primitive: a subdomain of
``evil.example.com/x?`` or ``attacker.com#`` would silently retarget every outbound
call - including the one that carries the decrypted API token in a header. Restrict
it to a single DNS label so the host can never be anything but a samanage.com tenant.
"""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# A single DNS label: RFC 1123, 1-63 chars, alphanumeric with internal hyphens.
# Deliberately NOT a dotted name - a dot would let a value escape the
# ".samanage.com" suffix by prefixing a different registrable domain.
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_subdomain(value):
    """Validate that ``value`` is a bare tenant subdomain label, e.g. ``acme``."""
    if not isinstance(value, str) or not _SUBDOMAIN_RE.fullmatch(value.strip().lower()):
        msg = _(
            "Enter the bare tenant subdomain only (e.g. 'acme' for "
            "https://acme.samanage.com/), lowercase letters, digits and hyphens - "
            "not a full URL or hostname.",
        )
        raise ValidationError(msg, code="invalid_subdomain")
