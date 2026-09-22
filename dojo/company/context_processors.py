from django.conf import settings

from dojo.context_processors import globalize_vars as _upstream_globalize_vars


def globalize_vars(request):
    """Upstream context plus the company name and palette, minus the Pro upsell link (SHOW_PLG_LINK)."""
    context = _upstream_globalize_vars(request)
    context["SHOW_PLG_LINK"] = False
    context["COMPANY_NAME"] = settings.COMPANY_NAME
    context["COMPANY_PALETTE"] = getattr(settings, "COMPANY_PALETTE", None) or {}
    return context
