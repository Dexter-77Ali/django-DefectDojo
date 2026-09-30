"""
company: views served under <url_prefix>company/ (dojo/company/urls.py).

Every view checks authorization itself, against dojo objects, with the upstream helpers.
"""
import logging

import pghistory
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from dojo.authorization.authorization import user_has_permission_or_403
from dojo.company.forms import NO_COMPONENT, QuickUploadForm, upload_engagement
from dojo.importers.services import auto_import_scan
from dojo.jira import services as jira_services
from dojo.models import Product, Test
from dojo.utils import Product_Tab, add_breadcrumb, get_system_setting

logger = logging.getLogger(__name__)


def _object_or_404(model, raw_id):
    if not raw_id:
        return None
    if not (raw_id.isascii() and raw_id.isdigit()):  # isdigit alone accepts "²", which int() rejects
        raise Http404
    return get_object_or_404(model, pk=raw_id)


def _push_to_jira(target):
    """Like the v2/v3 import endpoints (dojo/api_v3/import_routes.py _resolve_push_to_jira) with push_to_jira unset."""
    if get_system_setting("enable_jira") and (project := jira_services.get_project(target)):
        return bool(project.push_all_issues)
    return False


def _remove_empty_tests(product, test_ids_before, scan_type):
    """
    Upstream creates the engagement and the test before parsing and keeps them when parsing fails
    (dojo/importers/default_importer.py parse_findings_static_test_type); an empty test left behind
    would be the target of the next upload. Only tests this request created, and only when empty.
    The engagement stays: it is the product's upload engagement, the next upload reuses it, and its
    "Engagement created" notification has already gone out.
    """
    engagement = upload_engagement(product)
    if engagement is None:
        return
    for test in engagement.test_set.filter(scan_type=scan_type).exclude(id__in=test_ids_before):
        if not test.finding_set.exists():
            test.delete()


def _upload(request, form, test, product):
    """Run the import; returns the redirect on success, None after adding the error to the form."""
    data = form.cleaned_data
    scan_type = form.scan_type_value
    test = test or form.target_test
    engagement = test.engagement if test else upload_engagement(product)
    if test is None and engagement is None:
        user_has_permission_or_403(request.user, product, "add")  # the upload creates the engagement
    test_ids_before = set(engagement.test_set.values_list("id", flat=True)) if engagement else set()
    # outside the try: a renamed upstream JIRA helper must fail loudly, not read as a bad report
    push_to_jira = _push_to_jira(test or engagement or product)
    if test is not None:
        target = {"test": test}
    else:
        target = {
            "product_name": product.name,
            "product_type_name": product.prod_type.name,
            "engagement_name": settings.COMPANY_UPLOAD_ENGAGEMENT_NAME,
            "test_title": data["component"] or None,
            "auto_create_context": True,
        }
    try:
        with pghistory.context(source="company_quick_upload", scan_type=scan_type):
            result = auto_import_scan(
                user=request.user,
                scan_file=data["file"],
                scan_type=scan_type,
                close_old_findings=data["close_old_findings"],
                push_to_jira=push_to_jira,
                **target,
            )
    except Exception as e:
        logger.exception("company quick upload of a %s report failed", scan_type)
        if test is None:
            _remove_empty_tests(product, test_ids_before, scan_type)
        detail = "; ".join(e.messages) if isinstance(e, ValidationError) else e
        form.add_error(None, _("The report could not be imported. Check that it is a %(scanner)s report "
                               "and choose the file again. Details: %(error)s") % {"scanner": scan_type, "error": detail})
        return None
    counts = {"new": result.new, "closed": result.closed, "reactivated": result.reactivated, "untouched": result.untouched}
    summary = _("Upload complete: %(new)d new, %(closed)d closed, %(reactivated)d reactivated, %(untouched)d unchanged.")
    if result.closed and not (result.new or result.reactivated or result.untouched):
        # a report of another tool parses to nothing and closes everything; never let that pass silently
        summary += " " + _("The report contained no findings, so every open finding of this test was closed. "
                           "If this was the wrong scanner or file, upload the right report to reopen them.")
        messages.warning(request, summary % counts, extra_tags="alert-warning")
    else:
        messages.success(request, summary % counts, extra_tags="alert-success")
    return redirect("view_test", result.test.id)


@login_required
@require_http_methods(["GET", "POST"])
def quick_upload(request):
    """Upload a report for a product (?product=<id>) or a new report for a test (?test=<id>)."""
    test = _object_or_404(Test, request.GET.get("test"))
    if test is not None:
        user_has_permission_or_403(request.user, test, "import")
        product = test.engagement.product
    else:
        source = request.POST if request.method == "POST" else request.GET
        product = _object_or_404(Product, source.get("product"))
        if product is not None:
            user_has_permission_or_403(request.user, product, "import")

    if request.method == "POST":
        form = QuickUploadForm(request.POST, request.FILES, user=request.user, test=test)
        if form.is_valid() and (response := _upload(request, form, test, product)) is not None:
            return response
    else:
        form = QuickUploadForm(user=request.user, test=test, product=product, scan_type=request.GET.get("scan_type"))
        product = product or form.initial.get("product")

    product_tab = None
    all_options_url = None
    if test is not None:
        product_tab = Product_Tab(product, title=_("Upload new report"), tab="engagements")
        product_tab.setEngagement(test.engagement)
        all_options_url = reverse("re_import_scan_results", args=(test.id,))
    elif product is not None:
        product_tab = Product_Tab(product, title=_("Upload scan"), tab="findings")
        all_options_url = reverse("import_scan_results_prod", args=(product.id,))
    else:
        add_breadcrumb(title=_("Upload scan"), top_level=True, request=request)
    component = form["component"].value() if "component" in form.fields else ""
    return render(request, "company/quick_upload.html", {
        "form": form,
        "test": test,
        "product_tab": product_tab,
        "all_options_url": all_options_url,
        "no_component": NO_COMPONENT,
        "component_pick": component if component in {*form.component_titles, NO_COMPONENT} else ("__new__" if component else ""),
        "has_products": test is not None or form.fields["product"].queryset.exists(),
    })
