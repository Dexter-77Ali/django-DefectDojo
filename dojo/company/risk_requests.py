"""
company: risk acceptance requested by a member and approved by the security team.

A member asks for findings to be risk accepted for a number of days (create). An approver approves it,
which creates one upstream Risk_Acceptance per engagement through upstream's helpers, or rejects it with a
reason; the requester may cancel while it waits. Every step adds a note to the findings and notifies the
other side. Approvers hold company.approve_riskacceptancerequest: the Django group risk-approvers (synced
from DD_COMPANY_LDAP_RISK_APPROVER_GROUP, or `manage.py company_teams --approver` for local accounts), a
direct grant, and superusers; never through is_staff. Nobody decides their own request. With
DD_COMPANY_RA_APPROVAL_REQUIRED on, dojo/company/signals.py refuses the upstream ways around this for
everyone else.
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

import dojo.risk_acceptance.helper as ra_helper
from dojo.company.models import RiskAcceptanceRequest
from dojo.company.teams import add_notes, members_of_every_product
from dojo.finding.helper import NOT_ACCEPTED_FINDINGS_QUERY
from dojo.finding.queries import get_authorized_findings
from dojo.models import Dojo_User, Finding, Risk_Acceptance
from dojo.notifications.helper import create_notification

logger = logging.getLogger(__name__)

CODENAME = "approve_riskacceptancerequest"
PERMISSION = f"company.{CODENAME}"
APPROVER_GROUP = "risk-approvers"
# both in NOTIFICATIONS_SYSTEM_LEVEL_TRUMP (local_settings.py); company_risk_requested is also an escalation event
EVENT_REQUESTED = "company_risk_requested"
EVENT_DECIDED = "company_risk_decided"
MAX_FINDINGS = 500
PENDING = RiskAcceptanceRequest.PENDING


def is_approver(user) -> bool:
    """
    May decide risk acceptance requests (and use upstream's risk acceptance pages when approval is required):
    active superusers and holders of the permission, the same people approvers_for notifies. Not upstream's
    user_has_configuration_permission, which passes every staff user.
    """
    return bool(user is not None and user.has_perm(PERMISSION))


def approval_required_for(user) -> bool:
    """True when DD_COMPANY_RA_APPROVAL_REQUIRED is on and the user is signed in but no approver. No query unless it is on."""
    return bool(
        settings.COMPANY_RA_APPROVAL_REQUIRED and user is not None and user.is_authenticated and not is_approver(user),
    )


def approver_group() -> Group:
    """The Django group risk-approvers, holding the approver permission."""
    group = Group.objects.get_or_create(name=APPROVER_GROUP)[0]
    group.permissions.add(Permission.objects.get(content_type__app_label="company", codename=CODENAME))
    return group


def approvers_for(findings) -> list:
    """
    Active approvers who can open every product of the findings. Candidates in one query (superusers, and
    holders of the permission through a group or directly; not has_perm per user), then the membership check.
    """
    granted = (
        Q(is_superuser=True)
        | Q(groups__permissions__codename=CODENAME, groups__permissions__content_type__app_label="company")
        | Q(user_permissions__codename=CODENAME, user_permissions__content_type__app_label="company")
    )
    candidates = list(Dojo_User.objects.filter(is_active=True).filter(granted).distinct().order_by("username"))
    members = members_of_every_product({finding.test.engagement.product_id for finding in findings})
    return [user for user in candidates if user.is_superuser or user.is_staff or user.pk in members]


def split_eligible(findings) -> tuple[list, list]:
    """(findings that can be requested, [(finding, reason)] for the others). Findings come with test__engagement__product."""
    pending = set(
        Finding.objects.filter(id__in=[finding.id for finding in findings], company_risk_requests__status=PENDING)
        .values_list("id", flat=True),
    )
    eligible, skipped = [], []
    for finding in findings:
        if not finding.active or finding.duplicate or finding.risk_accepted:
            skipped.append((finding, _("not open, or already risk accepted")))
        elif finding.id in pending:
            skipped.append((finding, _("already waiting for approval")))
        elif not finding.test.engagement.product.enable_full_risk_acceptance:
            skipped.append((finding, _("risk acceptance is turned off for its product")))
        else:
            eligible.append(finding)
    return eligible, skipped


def create(findings, requested_by, justification: str, days: int):
    """A pending request for the eligible findings, a note on each and a notification to the approvers; None when none is eligible."""
    eligible, _skipped = split_eligible(findings)
    if not eligible:
        return None
    who = Dojo_User.generate_full_name(requested_by)
    with transaction.atomic():
        request = RiskAcceptanceRequest.objects.create(requested_by=requested_by, justification=justification, days=days)
        request.findings.add(*eligible)
        add_notes(eligible, _("%(user)s requested risk acceptance for %(days)d days (request #%(id)d): %(justification)s") % {
            "user": who, "days": days, "id": request.pk, "justification": justification}, requested_by)
    recipients = [user.username for user in approvers_for(eligible) if user.pk != requested_by.pk]
    title = ngettext(
        "%(user)s requests risk acceptance of %(count)d finding for %(days)d days",
        "%(user)s requests risk acceptance of %(count)d findings for %(days)d days",
        len(eligible),
    ) % {"user": who, "count": len(eligible), "days": days}
    # with no approver to notify the escalation copy (CompanyNotificationManager) still goes out
    create_notification(
        event=EVENT_REQUESTED,
        title=title,
        description=f"{title}: {justification}",
        recipients=recipients,
        url=reverse("company_risk_inbox") + "?tab=waiting",
        icon="hourglass-half",
    )
    return request


def waiting_for(user):
    """Pending requests the approver may decide: not their own, and every finding editable by them."""
    pending = RiskAcceptanceRequest.objects.filter(status=PENDING).exclude(requested_by=user)
    if user.is_superuser or user.is_staff:
        return pending
    editable = get_authorized_findings("edit", user=user).values("id")
    return pending.exclude(findings__in=Finding.objects.exclude(id__in=editable))


def _check_four_eyes(request, user) -> None:
    if request.requested_by_id is not None and request.requested_by_id == user.pk:
        raise PermissionDenied(_("Nobody decides their own risk acceptance request."))


def _decide(request, status: str, user, note: str) -> bool:
    """Compare-and-set from pending; False when the request was decided already (a second approve does nothing)."""
    now = timezone.now()
    done = RiskAcceptanceRequest.objects.filter(pk=request.pk, status=PENDING).update(
        status=status, decided_by=user, decided_at=now, decision_note=note,
    )
    if done:
        request.status, request.decided_by, request.decided_at, request.decision_note = status, user, now, note
    return bool(done)


def approve(request, approver, note: str = ""):
    """
    Approve a pending request: per engagement, one upstream Risk_Acceptance (owner the requester, accepted by
    the approver, decision accept, expiring after the requested days) holding the request's findings that are
    still open and not accepted. Returns the created risk acceptances, or None when the request was no longer
    pending. On an error the request is pending again, so approving it again is safe.

    No transaction around upstream's helpers: they queue JIRA pushes at once (dojo_dispatch_task has no
    on_commit), and a worker must read the committed finding. So an error undoes by hand: acceptances still
    empty are deleted, those holding findings stay linked to the request, and approving again adds the rest.
    """
    _check_four_eyes(request, approver)
    if not _decide(request, RiskAcceptanceRequest.APPROVED, approver, note):
        return None
    try:
        created = _accept(request, approver, note)
    except Exception:
        request.risk_acceptances.filter(accepted_findings__isnull=True).delete()
        RiskAcceptanceRequest.objects.filter(pk=request.pk).update(status=PENDING, decided_by=None, decided_at=None, decision_note="")
        raise
    who = Dojo_User.generate_full_name(approver)
    title = _("%(user)s approved your risk acceptance request #%(id)d") % {"user": who, "id": request.pk}
    url = ra_helper.get_view_risk_acceptance(created[0]) if created else None
    _notify_requester(request, title, url)
    return created


def _accept(request, approver, note):
    findings = list(
        request.findings.filter(active=True, duplicate=False).filter(NOT_ACCEPTED_FINDINGS_QUERY)
        .select_related("test__engagement__product").order_by("numerical_severity", "id"),
    )
    by_engagement = defaultdict(list)
    for finding in findings:
        by_engagement[finding.test.engagement].append(finding)
    owner = request.requested_by or approver  # the requester's account may be gone (SET_NULL)
    expiration = timezone.now() + timedelta(days=request.days)
    reason = _("request #%(id)d") % {"id": request.pk} + (f": {note}" if note else "")
    created = []
    for engagement, items in by_engagement.items():
        if not engagement.product.enable_full_risk_acceptance:  # switched off since the request
            continue
        name = ngettext("Request #%(id)d: %(title)s", "Request #%(id)d: %(title)s and %(more)d more", len(items))
        risk_acceptance = Risk_Acceptance.objects.create(
            name=(name % {"id": request.pk, "title": items[0].title, "more": len(items) - 1})[:300],
            owner=owner,
            accepted_by=Dojo_User.generate_full_name(approver)[:200],
            decision=Risk_Acceptance.TREATMENT_ACCEPT,
            decision_details=request.justification,
            expiration_date=expiration,
        )
        engagement.risk_acceptance.add(risk_acceptance)
        request.risk_acceptances.add(risk_acceptance)
        ra_helper.add_findings_to_risk_acceptance(approver, risk_acceptance, items)
        ra_helper.note_on_risk_acceptance(risk_acceptance, approver, "approved", reason=reason)
        created.append(risk_acceptance)
    return created


def reject(request, approver, reason: str) -> bool:
    """Reject a pending request with a reason; the findings stay as they are and get a note. False when already decided."""
    _check_four_eyes(request, approver)
    if not reason.strip():
        msg = "a rejection needs a reason"
        raise ValueError(msg)
    if not _decide(request, RiskAcceptanceRequest.REJECTED, approver, reason):
        return False
    who = Dojo_User.generate_full_name(approver)
    add_notes(list(request.findings.all()), _("%(user)s rejected risk acceptance request #%(id)d: %(reason)s") % {
        "user": who, "id": request.pk, "reason": reason}, approver)
    _notify_requester(request, _("%(user)s rejected your risk acceptance request #%(id)d: %(reason)s") % {
        "user": who, "id": request.pk, "reason": reason}, None)
    return True


def cancel(request, user) -> bool:
    """The requester withdraws a pending request. False when already decided."""
    if request.requested_by_id != user.pk:
        raise PermissionDenied
    if not _decide(request, RiskAcceptanceRequest.CANCELLED, user, ""):
        return False
    add_notes(list(request.findings.all()), _("%(user)s cancelled risk acceptance request #%(id)d") % {
        "user": Dojo_User.generate_full_name(user), "id": request.pk}, user)
    return True


def _notify_requester(request, title: str, url) -> None:
    requester = request.requested_by
    if requester is None or not requester.is_active:
        return
    create_notification(
        event=EVENT_DECIDED,
        title=title,
        description=title,
        recipients=[requester.username],
        url=url or reverse("company_risk_inbox") + "?tab=mine",
        icon="scale-balanced",
    )


def pending_count(user):
    """Sidebar "Risk acceptance": requests waiting for the user; None, without the count query, unless they approve."""
    return waiting_for(user).count() if is_approver(user) else None


def tiles(user) -> dict:
    """Dashboard tiles: (count, inbox URL) of the user's pending requests and, for approvers, of those waiting for them."""
    inbox = reverse("company_risk_inbox")
    mine = RiskAcceptanceRequest.objects.filter(requested_by=user, status=PENDING).count()
    waiting = pending_count(user)
    return {"mine": (mine, inbox + "?tab=mine"), "waiting": None if waiting is None else (waiting, inbox + "?tab=waiting")}


def finding_state(finding) -> dict:
    """Finding page: a request is waiting (badge), or one can be made (button). One query."""
    pending = finding.company_risk_requests.filter(status=PENDING).exists()
    can_request = (not pending and finding.active and not finding.duplicate and not finding.risk_accepted
                   and finding.test.engagement.product.enable_full_risk_acceptance)
    return {"pending": pending, "can_request": can_request}
