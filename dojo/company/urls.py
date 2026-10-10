"""
company: root URLconf (ROOT_URLCONF = "dojo.company.urls" in dojo/settings/local_settings.py).

Company views live under <url_prefix>company/ and are matched first; every upstream route
follows unchanged, so upstream URL names reverse and resolve exactly as before. Django reads
the error handlers from the root URLconf, hence the re-export.
"""
from django.urls import include, re_path

from dojo.company import views
from dojo.urls import handler400, handler403, handler500
from dojo.urls import urlpatterns as dojo_urlpatterns
from dojo.utils import get_system_setting

__all__ = ["company_patterns", "handler400", "handler403", "handler500", "urlpatterns"]

# Company views append here (URL names start with "company_"); served under <url_prefix>company/.
company_patterns = [
    re_path(r"^upload$", views.quick_upload, name="company_quick_upload"),
    re_path(r"^assign$", views.assign, name="company_assign"),
]

urlpatterns = [
    re_path(rf"^{get_system_setting('url_prefix')}company/", include(company_patterns)),
    *dojo_urlpatterns,
]
