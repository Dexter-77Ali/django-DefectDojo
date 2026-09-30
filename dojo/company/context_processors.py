from django.conf import settings

from dojo.context_processors import globalize_vars as _upstream_globalize_vars

# Source tag of the "Get Pro" banners upstream queues in the session after imports, slow
# requests and error pages (dojo/product_announcements.py).
PRODUCT_ANNOUNCEMENT_SOURCE = "product_announcement"


def globalize_vars(request):
    """Upstream context plus the company name and palette, minus the Pro upsell link (SHOW_PLG_LINK) and banners."""
    context = _upstream_globalize_vars(request)
    context["SHOW_PLG_LINK"] = False
    context["COMPANY_NAME"] = settings.COMPANY_NAME
    context["COMPANY_PALETTE"] = getattr(settings, "COMPANY_PALETTE", None) or {}
    # Upstream has already popped the session queue; keep every banner that is not an upsell.
    banners = [
        banner for banner in context.pop("additional_banners", [])
        if banner.get("source") != PRODUCT_ANNOUNCEMENT_SOURCE
    ]
    if banners:
        context["additional_banners"] = banners
    return context
