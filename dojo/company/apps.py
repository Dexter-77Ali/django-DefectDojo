from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Settings that hold dotted paths resolved through dojo.utils.get_custom_method.
# That helper ignores a missing module silently, so the wiring is asserted here at startup.
HOOK_SETTINGS = (
    "FINDING_SLA_PERIOD_METHOD",
    "FINDING_SLA_EXPIRATION_CALCULATION_METHOD",
)


class CompanyConfig(AppConfig):

    """Company customizations: templates, static files, context processor, fields, SLA hooks (see CLAUDE.md)."""

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
