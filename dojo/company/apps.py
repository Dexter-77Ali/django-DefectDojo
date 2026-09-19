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
        from dojo.utils import get_custom_method  # noqa: PLC0415 -- app registry must be ready

        for name in HOOK_SETTINGS:
            dotted = getattr(settings, name, None)
            if dotted and get_custom_method(name) is None:
                msg = f"{name} = {dotted!r} cannot be imported"
                raise ImproperlyConfigured(msg)
