import re

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Settings that hold dotted paths resolved through dojo.utils.get_custom_method.
# That helper ignores a missing module silently, so the wiring is asserted here at startup.
HOOK_SETTINGS = (
    "FINDING_SLA_PERIOD_METHOD",
    "FINDING_SLA_EXPIRATION_CALCULATION_METHOD",
)
PALETTE_SHADES = {"50", "100", "200", "300", "400", "500", "600", "700", "800", "900"}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class CompanyConfig(AppConfig):

    """Company customizations: templates, static files, context processor, fields, SLA hooks, LDAP (see CLAUDE.md)."""

    name = "dojo.company"
    label = "company"
    verbose_name = "Company customizations"

    def ready(self):
        from django.utils.module_loading import import_string  # noqa: PLC0415 -- app registry must be ready

        from dojo.utils import get_custom_method  # noqa: PLC0415 -- app registry must be ready

        for name in HOOK_SETTINGS:
            dotted = getattr(settings, name, None)
            if dotted and get_custom_method(name) is None:
                msg = f"{name} = {dotted!r} cannot be imported"
                raise ImproperlyConfigured(msg)
        # dojo.notifications.helper resolves this one with suppress(ModuleNotFoundError)
        manager = getattr(settings, "NOTIFICATION_MANAGER", None)
        if isinstance(manager, str):
            try:
                import_string(manager)
            except ImportError as e:
                msg = f"NOTIFICATION_MANAGER = {manager!r} cannot be imported"
                raise ImproperlyConfigured(msg) from e
        # The LDAP backend needs django-auth-ldap, which only the company image installs.
        if getattr(settings, "COMPANY_LDAP_ENABLED", False):
            try:
                import_string("dojo.company.ldap_backend.CompanyLDAPBackend")
            except ImportError as e:
                msg = "DD_COMPANY_LDAP_ENABLED is on but django-auth-ldap is missing; build with Dockerfile.company"
                raise ImproperlyConfigured(msg) from e
        validate_company_configuration()


def validate_company_configuration() -> None:
    """Fail at startup on malformed DD_COMPANY_* JSON instead of at the first request."""
    palette = getattr(settings, "COMPANY_PALETTE", None) or {}
    if not isinstance(palette, dict) or any(
        str(shade) not in PALETTE_SHADES or not HEX_COLOR.match(str(color)) for shade, color in palette.items()
    ):
        msg = 'DD_COMPANY_PALETTE must map shades 50..900 to "#rrggbb" colours'
        raise ImproperlyConfigured(msg)
    factors = getattr(settings, "COMPANY_SLA_FACTORS", None)
    if factors is not None and (
        not isinstance(factors, dict) or any(not isinstance(v, int | float) or v <= 0 for v in factors.values())
    ):
        msg = "DD_COMPANY_SLA_FACTORS must map criticality names to positive numbers"
        raise ImproperlyConfigured(msg)
    fields = getattr(settings, "COMPANY_FIELDS", None)
    if fields is not None and (
        not isinstance(fields, dict) or not fields or any(not isinstance(spec, dict) for spec in fields.values())
    ):
        msg = "DD_COMPANY_FIELDS must be a non-empty object of field name to {label, choices}"
        raise ImproperlyConfigured(msg)
