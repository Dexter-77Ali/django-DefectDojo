"""
company: views served under <url_prefix>company/ (dojo/company/urls.py).

Every view checks authorization itself, against dojo objects, with the upstream helpers.
"""
import logging
from datetime import timedelta

import pghistory
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, F, Prefetch
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

import dojo.risk_acceptance.helper as ra_helper
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.company import risk_requests, teams
from dojo.company.forms import NO_COMPONENT, QuickUploadForm, RiskRequestForm, upload_engagement
from dojo.company.models import RiskAcceptanceRequest
from dojo.engagement.queries import get_authorized_engagements
from dojo.finding import helper as finding_helper
from dojo.finding.queries import get_authorized_findings_for_queryset
from dojo.finding.ui.views import get_missing_mandatory_notetypes
from dojo.importers.services import auto_import_scan
from dojo.jira import services as jira_services
from dojo.middleware import set_language_cookie
from dojo.models import Finding, Note_Type, Product, Risk_Acceptance, Test, UserContactInfo
from dojo.risk_acceptance.queries import get_authorized_risk_acceptances
from dojo.utils import (
    Product_Tab,
    add_breadcrumb,
    get_page_items,
    get_return_url,
    get_system_setting,
    redirect_to_return_url_or_else,
)

logger = logging.getLogger(__name__)


def _object_or_404(model, raw_id):
    if not raw_id:
        return None
    if not (raw_id.isascii() and raw_id.isdigit()):  # isdigit alone accepts "²", which int() rejects
        raise Http404
    return get_object_or_404(model, pk=raw_id)


def _return_url(request):
    """The posted or queried return_url, "" without one, None when it leaves this site (upstream's redirect() then raises, a 500)."""
    return_url = (get_return_url(request) or "").strip()
    if return_url and not url_has_allowed_host_and_scheme(return_url, allowed_hosts=None):
        return None
    return return_url


def _editable_findings(request, raw_ids):
    """The findings of the ids, by severity; PermissionDenied unless the user may edit every one (nothing changes then)."""
    ids = {int(value) for value in raw_ids if value.isascii() and value.isdigit()}
    findings = list(
        get_authorized_findings_for_queryset("edit", Finding.objects.filter(id__in=ids), user=request.user)
        .select_related("test__engagement__product").prefetch_related("tags").order_by("numerical_severity", "id"),
    )
    if len(findings) != len(set(raw_ids)):  # a finding the user may not edit, a missing one or a forged id
        raise PermissionDenied
    return findings


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


@login_required
@require_POST
def assign(request):
    """
    Assign findings (finding_to_update, at most teams.MAX_FINDINGS) to a team and optionally a person.

    Posted without a team (the bulk "Assign…" button), it shows the choices; with a team (the assign page,
    the finding page) it saves and returns to return_url. Every finding must be editable by the user,
    otherwise 403 and nothing changes.
    """
    return_url = _return_url(request)
    if return_url is None:
        return HttpResponseBadRequest(_("return_url must stay on this site"))
    raw_ids = request.POST.getlist("finding_to_update")
    if not raw_ids or len(raw_ids) > teams.MAX_FINDINGS:
        messages.warning(request, _("Select between 1 and %(max)d findings to assign.") % {"max": teams.MAX_FINDINGS},
                         extra_tags="alert-warning")
        return redirect_to_return_url_or_else(request, reverse("open_findings"))
    findings = _editable_findings(request, raw_ids)

    choices = teams.assignable_teams(findings)
    team = request.POST.get("team", "")
    person_id = request.POST.get("person", "")
    error = None
    if team:
        members = choices.get(team)
        person = next((user for user in members if str(user.id) == person_id), None) if members is not None else None
        if members is None:
            error = _("Choose one of the teams.")
        elif person_id and person is None:
            error = _("The chosen person is not an active member of this team who can open every selected finding.")
        else:
            teams.assign(findings, team, person, request.user)
            teams.notify(len(findings), team, person, members, request.user)
            target = f"{team} / {person}" if person else team
            done = ngettext("Assigned %(count)d finding to %(target)s.", "Assigned %(count)d findings to %(target)s.", len(findings))
            messages.success(request, done % {"count": len(findings), "target": target}, extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("open_findings"))

    add_breadcrumb(title=_("Assign findings"), top_level=True, request=request)
    return render(request, "company/assign.html", {
        "findings": findings,
        "teams": choices,
        "team": team,
        "person_id": person_id,
        "error": error,
        "return_url": return_url or reverse("open_findings"),
    })


@login_required
@require_http_methods(["GET", "POST"])
def risk_request(request):
    """
    Ask the security team to accept the risk of findings for a number of days: GET ?finding=<id> (the finding
    page), or POST finding_to_update (the bulk "Request risk acceptance…" button, which shows this page; the page
    posts again with the justification). Every finding must be editable by the user, otherwise 403 and nothing
    changes. Findings that cannot be requested are listed with the reason and left out.
    """
    return_url = _return_url(request)
    if return_url is None:
        return HttpResponseBadRequest(_("return_url must stay on this site"))
    raw_ids = request.POST.getlist("finding_to_update") if request.method == "POST" else request.GET.getlist("finding")
    if not raw_ids or len(raw_ids) > risk_requests.MAX_FINDINGS:
        messages.warning(request, _("Select between 1 and %(max)d findings to request risk acceptance for.") % {
            "max": risk_requests.MAX_FINDINGS}, extra_tags="alert-warning")
        return redirect_to_return_url_or_else(request, reverse("open_findings"))
    findings = _editable_findings(request, raw_ids)
    eligible, skipped = risk_requests.split_eligible(findings)
    submitted = request.method == "POST" and "justification" in request.POST
    form = RiskRequestForm(request.POST if submitted else None)
    if submitted and eligible and form.is_valid():
        with pghistory.context(source="company_risk_request"):
            created = risk_requests.create(eligible, request.user, form.cleaned_data["justification"], form.cleaned_data["days"])
        if created is not None:
            done = ngettext("Requested risk acceptance of %(count)d finding. The security team has been notified.",
                            "Requested risk acceptance of %(count)d findings. The security team has been notified.",
                            len(eligible))
            messages.success(request, done % {"count": len(eligible)}, extra_tags="alert-success")
            return redirect_to_return_url_or_else(request, reverse("company_risk_inbox") + "?tab=mine")

    add_breadcrumb(title=_("Request risk acceptance"), top_level=True, request=request)
    return render(request, "company/risk_request.html", {
        "form": form,
        "eligible": eligible,
        "skipped": skipped,
        "return_url": return_url or (reverse("view_finding", args=(findings[0].id,)) if len(findings) == 1 else reverse("open_findings")),
    })


@login_required
@require_POST
def mark_fixed(request, fid=None):
    """
    Close findings as fixed in one click: from the finding page (fid) or the findings bulk menu (finding_to_update,
    at most teams.MAX_FINDINGS), through upstream's close_finding with the note "Marked fixed". Every finding must
    be editable by the user, otherwise 403 and nothing changes. Mandatory note types need upstream's close page,
    which asks for them: a single finding goes there, a bulk selection holding such a finding changes nothing.
    The assignment tags stay, so a re-upload that reactivates the finding keeps its assignee.
    Off (controls hidden, view refused) with DD_COMPANY_MARK_FIXED=False.
    """
    if not settings.COMPANY_MARK_FIXED:
        raise PermissionDenied
    return_url = _return_url(request)
    if return_url is None:
        return HttpResponseBadRequest(_("return_url must stay on this site"))
    raw_ids = [fid] if fid else request.POST.getlist("finding_to_update")
    if not raw_ids or len(raw_ids) > teams.MAX_FINDINGS:
        messages.warning(request, _("Select between 1 and %(max)d findings to mark fixed.") % {"max": teams.MAX_FINDINGS},
                         extra_tags="alert-warning")
        return redirect_to_return_url_or_else(request, reverse("open_findings"))
    findings = [finding for finding in _editable_findings(request, raw_ids) if finding.active]
    if not findings:
        messages.info(request, _("The selected findings are closed already."), extra_tags="alert-info")
        return redirect_to_return_url_or_else(request, reverse("open_findings"))
    if Note_Type.objects.filter(is_mandatory=True, is_active=True).exists():
        missing = [finding for finding in findings if get_missing_mandatory_notetypes(finding).exists()]
        if len(findings) == 1 and missing:
            return redirect("close_finding", findings[0].id)
        if missing:
            messages.warning(request, ngettext(
                "%(count)d of the selected findings needs mandatory notes before it can be closed: close it from its page. Nothing was changed.",
                "%(count)d of the selected findings need mandatory notes before they can be closed: close them from their pages. Nothing was changed.",
                len(missing)) % {"count": len(missing)}, extra_tags="alert-warning")
            return redirect_to_return_url_or_else(request, reverse("open_findings"))
    with teams.instance_language():
        note = _("Marked fixed")
    with pghistory.context(source="company_mark_fixed"):
        for finding in findings:
            finding_helper.close_finding(
                finding=finding, user=request.user, is_mitigated=True, mitigated=None, mitigated_by=None,
                false_p=False, out_of_scope=False, duplicate=False, note_entry=note,
            )
    messages.success(request, ngettext("Marked %(count)d finding fixed.", "Marked %(count)d findings fixed.", len(findings)) % {
        "count": len(findings)}, extra_tags="alert-success")
    return redirect_to_return_url_or_else(request, reverse("open_findings"))


@login_required
@require_POST
def set_language(request):
    """
    The EN / Arabic switch of the user menu: saves the user's language (UserContactInfo.language, read by upstream's
    LanguagePreferenceMiddleware on other devices) and sets the cookie Django's LocaleMiddleware reads on this one.
    """
    return_url = _return_url(request)
    if return_url is None:
        return HttpResponseBadRequest(_("return_url must stay on this site"))
    language = request.POST.get("language", "")
    if language not in dict(settings.LANGUAGES):
        return HttpResponseBadRequest(_("Unknown language"))
    contact = UserContactInfo.objects.get_or_create(user=request.user)[0]
    contact.language = language
    contact.save(update_fields=["language"])
    return set_language_cookie(redirect(return_url or "dashboard"), language)


INBOX_TABS = ("waiting", "mine", "active", "expiring")
EXPIRING_DAYS = 30


@login_required
@require_GET
def risk_inbox(request):
    """
    Risk acceptance in four tabs: requests waiting for the approver (approvers only), the user's own requests,
    and active and soon expiring risk acceptances (read-only, scoped by upstream's get_authorized_risk_acceptances).
    """
    user = request.user
    tabs = [tab for tab in INBOX_TABS if tab != "waiting" or risk_requests.is_approver(user)]
    tab = request.GET.get("tab") if request.GET.get("tab") in tabs else tabs[0]
    findings = Prefetch("findings", queryset=Finding.objects.select_related("test__engagement__product").order_by("numerical_severity", "id"))
    if tab == "waiting":
        items = risk_requests.waiting_for(user).select_related("requested_by").prefetch_related(findings).order_by("created", "id")
    elif tab == "mine":
        # only the findings the requester can still open (access may have changed since the request)
        visible = Prefetch("findings", queryset=get_authorized_findings_for_queryset("view", findings.queryset, user=user))
        items = (RiskAcceptanceRequest.objects.filter(requested_by=user).select_related("decided_by")
                 .prefetch_related(visible, "risk_acceptances__engagement_set").order_by("-created", "-id"))
    else:
        items = (get_authorized_risk_acceptances("view").filter(expiration_date_handled__isnull=True)
                 .select_related("owner").prefetch_related("engagement_set__product")
                 .annotate(finding_count=Count("accepted_findings", distinct=True)))
        if tab == "expiring":
            items = items.filter(expiration_date__isnull=False, expiration_date__lte=timezone.now() + timedelta(days=EXPIRING_DAYS))
        items = items.order_by(F("expiration_date").asc(nulls_last=True), "id")
    page = get_page_items(request, items, 25)
    editable = set()
    if tab == "expiring":
        # "Let expire" for whoever may edit the engagement, "Extend" for approvers among them: one query for the page
        engagement_ids = {engagement.id for acceptance in page for engagement in acceptance.engagement_set.all()}
        editable = set(get_authorized_engagements("edit").filter(id__in=engagement_ids).values_list("id", flat=True))
    add_breadcrumb(title=_("Risk acceptance"), top_level=True, request=request)
    return render(request, "company/risk_inbox.html", {
        "tab": tab,
        "tabs": tabs,
        "page": page,
        "expiring_days": EXPIRING_DAYS,
        "editable_engagements": editable,
        "can_extend": tab == "expiring" and risk_requests.is_approver(user),
        "extend_days": settings.COMPANY_RA_DAYS,
    })


@login_required
@require_POST
def risk_expiring(request):
    """
    Expiring tab actions on one risk acceptance (acceptance=<id>): action=extend moves its expiry `days` (one of
    DD_COMPANY_RA_DAYS) later, for approvers on a product with full risk acceptance on; action=expire lets it expire now (findings reopen when it reactivates
    them), for anyone who may edit its engagement, like upstream's expire page. Both need edit on the engagement.
    """
    action = request.POST.get("action")
    raw_id = request.POST.get("acceptance", "")
    if action not in {"extend", "expire"} or not raw_id:
        return HttpResponseBadRequest(_("Choose an action and a risk acceptance."))
    acceptance = _object_or_404(Risk_Acceptance, raw_id)
    engagement = acceptance.engagement
    if engagement is None:
        raise Http404
    user_has_permission_or_403(request.user, engagement, "edit")
    back = redirect(reverse("company_risk_inbox") + "?tab=expiring")
    if acceptance.expiration_date_handled:
        messages.info(request, _("This risk acceptance has expired already."), extra_tags="alert-info")
        return back
    if acceptance.expiration_date is None:  # never expires: not in the Expiring tab, and extending would time-box it
        return HttpResponseBadRequest(_("This risk acceptance never expires."))
    if action == "expire":
        with pghistory.context(source="company_risk_expiring"), teams.instance_language():
            ra_helper.expire_now(acceptance, user=request.user, reason=_("let expire in the risk acceptance inbox"))
        messages.success(request, _("The risk acceptance has expired; its findings are open again if it reactivates them."),
                         extra_tags="alert-success")
        return back
    # like upstream's reinstate and edit pages: no extending once the product switched full risk acceptance off
    if not risk_requests.is_approver(request.user) or not engagement.product.enable_full_risk_acceptance:
        raise PermissionDenied
    days = request.POST.get("days", "")
    if not (days.isascii() and days.isdigit()) or int(days) not in settings.COMPANY_RA_DAYS:
        return HttpResponseBadRequest(_("Choose one of the offered durations."))
    with pghistory.context(source="company_risk_expiring"):
        risk_requests.extend(acceptance, int(days), request.user)
    messages.success(request, _("The risk acceptance now expires on %(date)s.") % {
        "date": date_format(timezone.localtime(acceptance.expiration_date), "DATE_FORMAT")}, extra_tags="alert-success")
    return back


@login_required
@require_POST
def risk_decide(request):
    """
    Approve or reject pending requests (request=<id>, several for a bulk approve; a rejection needs a note) as an
    approver who may edit every finding of them, never on their own request. The requester cancels their own
    pending request here too (action=cancel). Any request failing a check: 403 and nothing changes.
    """
    action = request.POST.get("action")
    ids = {int(value) for value in request.POST.getlist("request") if value.isascii() and value.isdigit()}
    if action not in {"approve", "reject", "cancel"} or not ids:
        return HttpResponseBadRequest(_("Choose an action and at least one request."))
    chosen = list(RiskAcceptanceRequest.objects.filter(id__in=ids).order_by("id"))
    if len(chosen) != len(ids):
        raise Http404
    inbox = reverse("company_risk_inbox")
    if action == "cancel":
        if any(item.requested_by_id != request.user.pk for item in chosen):
            raise PermissionDenied
        done = sum(risk_requests.cancel(item, request.user) for item in chosen)
        messages.success(request, ngettext("Cancelled %(count)d request.", "Cancelled %(count)d requests.", done) % {"count": done},
                         extra_tags="alert-success")
        return redirect(inbox + "?tab=mine")

    if not risk_requests.is_approver(request.user):
        raise PermissionDenied
    for item in chosen:
        if item.requested_by_id == request.user.pk:  # four eyes; the inbox never offers it
            raise PermissionDenied
        finding_ids = list(item.findings.values_list("id", flat=True))
        editable = get_authorized_findings_for_queryset("edit", Finding.objects.filter(id__in=finding_ids), user=request.user)
        if editable.count() != len(finding_ids):
            raise PermissionDenied
    note = request.POST.get("note", "").strip()
    if action == "reject" and not note:
        messages.warning(request, _("Give the reason for the rejection; the requester sees it."), extra_tags="alert-warning")
        return redirect(inbox + "?tab=waiting")

    decided = created = failed = 0
    with pghistory.context(source="company_risk_decide"):
        for item in chosen:
            if action == "reject":
                decided += risk_requests.reject(item, request.user, note)
                continue
            try:
                acceptances = risk_requests.approve(item, request.user, note)
            except Exception:
                logger.exception("company risk request #%d could not be approved", item.pk)
                failed += 1
                continue
            if acceptances is not None:
                decided += 1
                created += len(acceptances)
    if action == "approve":
        done = ngettext("Approved %(count)d request: %(created)d risk acceptances created.",
                        "Approved %(count)d requests: %(created)d risk acceptances created.", decided)
        messages.success(request, done % {"count": decided, "created": created}, extra_tags="alert-success")
    else:
        messages.success(request, ngettext("Rejected %(count)d request.", "Rejected %(count)d requests.", decided) % {"count": decided},
                         extra_tags="alert-success")
    if failed:
        messages.error(request, ngettext("%(count)d request could not be approved and is still waiting.",
                                         "%(count)d requests could not be approved and are still waiting.", failed) % {"count": failed},
                       extra_tags="alert-danger")
    if already := len(chosen) - decided - failed:
        messages.info(request, ngettext("%(count)d request had been decided already.", "%(count)d requests had been decided already.",
                                        already) % {"count": already}, extra_tags="alert-info")
    return redirect(inbox + "?tab=waiting")
