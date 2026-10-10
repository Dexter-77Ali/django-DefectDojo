"""
company: security-team approval enforced on upstream's risk acceptance paths (DD_COMPANY_RA_APPROVAL_REQUIRED).

Connected from CompanyConfig.ready(). With the setting on, a signed-in user who is not an approver
(dojo.company.risk_requests.is_approver) cannot accept risk any other way than "Request risk acceptance":
creating a risk acceptance, changing its decision, accepted by, owner, reactivation or expiry handling,
moving its expiry later, adding findings to it, reinstating an expired one, simple acceptance, switching
simple acceptance on for a product and copying an engagement that holds risk acceptances are refused with
PermissionDenied (a 403 on pages and in API v2), on every path: UI pages, API v2 (create, PATCH, reinstate,
accept_risks) and helpers. A finding they save as risk accepted that was not accepted before (scan import and
re-import take the flag from the report, copies keep it) is saved open instead. Expiring (expire now),
deleting and removing findings stay allowed here, because they make findings active again; of upstream's
pages only expire stays open to them (GUARDED_URLS), API v2 delete and removal stay open on purpose.

Every guard returns at once, with no query, when the setting is off or nobody is signed in: the celery beat
expiry job, management commands and the anonymous JIRA webhook pass. Celery tasks dispatched from a request
run as that user (dojo/celery.py). The company service passes because it runs as an approver. The M2M receiver
is connected only while the setting is on: any listener turns off Django's fast add, one query per add.
"""
import contextvars
import functools

import crum
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.signals import setting_changed
from django.db.models.signals import m2m_changed, pre_save
from django.utils import timezone
from django.utils.translation import gettext as _

from dojo.company import risk_requests

# Risk_Acceptance fields only an approver may change; owner_id compares the key without a query.
# expiration_date_handled: set, the expiry job skips the acceptance and its findings stay accepted; only expire now sets it.
GUARDED_FIELDS = ("reactivate_expired", "decision", "accepted_by", "owner_id", "expiration_date_handled")
# upstream pages that add, edit, reinstate or delete risk acceptances and simple accept findings: with the
# setting on they also need the approver permission (dojo/authorization/url_permissions.py), a friendlier
# 403 before the view runs. Expire stays open to members.
GUARDED_URLS = (
    "add_risk_acceptance", "edit_risk_acceptance", "reinstate_risk_acceptance", "delete_risk_acceptance",
    "simple_risk_accept_finding",
)
APPROVER_CHECK = ("config", risk_requests.PERMISSION)
M2M_UID = "company_accepted_findings_changed"
expiring = contextvars.ContextVar("company_ra_expiring", default=False)  # inside ra_helper.expire_now


def refuse():
    raise PermissionDenied(_("Only the security team accepts risk. Use Request risk acceptance on the findings."))


def refuse_unless_approver() -> None:
    if risk_requests.approval_required_for(crum.get_current_user()):
        refuse()


def risk_acceptance_pre_save(sender, instance, **kwargs):
    if kwargs.get("raw") or not risk_requests.approval_required_for(crum.get_current_user()):
        return
    if instance._state.adding:
        refuse()
    old = sender.objects.filter(pk=instance.pk).values(*GUARDED_FIELDS, "expiration_date").first()
    if old is None:
        refuse()
    changed = {field for field in GUARDED_FIELDS if getattr(instance, field) != old[field]}
    if expiring.get():
        changed.discard("expiration_date_handled")
    if changed:
        refuse()
    before, after = old["expiration_date"], instance.expiration_date
    # later than the later of the old date and now: an extension, or a reinstatement. Expire now stays allowed.
    if before is not None and (after is None or after > max(before, timezone.now())):
        refuse()


def accepted_findings_changed(sender, action, **kwargs):
    # only additions: removals (unaccept) make findings active again
    if action == "pre_add":
        refuse_unless_approver()


def product_pre_save(sender, instance, **kwargs):
    # any product member may edit this flag upstream, and simple acceptance needs no risk acceptance at all
    if kwargs.get("raw") or not instance.enable_simple_risk_acceptance or not risk_requests.approval_required_for(crum.get_current_user()):
        return
    if instance._state.adding or not sender.objects.filter(pk=instance.pk, enable_simple_risk_acceptance=True).exists():
        refuse()


def finding_pre_save(sender, instance, **kwargs):
    """
    A finding a non-approver saves as risk accepted that was not accepted before is saved open instead: scan
    import and re-import take the flag from the report (re-import also deactivates the matched finding for it),
    and copies keep it. Cleared rather than refused, so uploads of such reports still work. The user is checked
    first: no field read (a deferred one would load) and no query while the setting is off or nobody is signed in.
    """
    if kwargs.get("raw") or not risk_requests.approval_required_for(crum.get_current_user()) or not instance.risk_accepted:
        return
    if instance.pk is not None and sender.objects.filter(pk=instance.pk, risk_accepted=True).exists():
        return
    instance.risk_accepted = False
    instance.active = not (instance.is_mitigated or instance.false_p or instance.out_of_scope or instance.duplicate)


def adds_findings(user, risk_acceptance, findings, *args, **kwargs):
    # the API v2 PATCH calls the helper on every update, with no findings unless some are added
    return len(findings) > 0


def has_risk_acceptances(engagement, *args, **kwargs):
    return engagement.risk_acceptance.exists()


def guard(function, when=None):
    """
    Run the approval check before the upstream helper does anything. add_findings_to_risk_acceptance saves each
    finding as accepted before adding it, reinstate re-accepts the findings before saving the acceptance and
    Engagement.copy copies tests and findings before the risk acceptances, so a check at the save or the M2M
    alone would leave half-done work after the 403. `when` runs only for a user the check applies to.
    """
    if getattr(function, "company_guarded", False):
        return function

    @functools.wraps(function)
    def guarded(*args, **kwargs):
        if risk_requests.approval_required_for(crum.get_current_user()) and (when is None or when(*args, **kwargs)):
            refuse()
        return function(*args, **kwargs)

    guarded.company_guarded = True
    return guarded


def allow_expiry(function):
    """Expire now sets expiration_date_handled, which the pre_save guard refuses everywhere else."""
    if getattr(function, "company_guarded", False):
        return function

    @functools.wraps(function)
    def expire(*args, **kwargs):
        token = expiring.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            expiring.reset(token)

    expire.company_guarded = True
    return expire


def apply_url_permissions(*, enabled: bool) -> None:
    from dojo.authorization.url_permissions import URL_PERMISSIONS  # noqa: PLC0415 -- imports models

    for name in GUARDED_URLS:
        checks = URL_PERMISSIONS[name]
        if enabled and APPROVER_CHECK not in checks:
            checks.append(APPROVER_CHECK)
        elif not enabled and APPROVER_CHECK in checks:
            checks.remove(APPROVER_CHECK)


def apply_setting(*, enabled: bool) -> None:
    from dojo.models import Risk_Acceptance  # noqa: PLC0415 -- app registry must be ready

    apply_url_permissions(enabled=enabled)
    through = Risk_Acceptance.accepted_findings.through
    if enabled:
        m2m_changed.connect(accepted_findings_changed, sender=through, dispatch_uid=M2M_UID)
    else:
        m2m_changed.disconnect(sender=through, dispatch_uid=M2M_UID)


def on_setting_changed(setting, value, **kwargs):
    # override_settings in tests; in production the setting is read once at startup
    if setting == "COMPANY_RA_APPROVAL_REQUIRED":
        apply_setting(enabled=bool(value))


def connect() -> None:
    import dojo.risk_acceptance.helper as ra_helper  # noqa: PLC0415 -- app registry must be ready
    from dojo.models import Engagement, Finding, Product, Risk_Acceptance  # noqa: PLC0415

    pre_save.connect(risk_acceptance_pre_save, sender=Risk_Acceptance, dispatch_uid="company_risk_acceptance_pre_save")
    pre_save.connect(product_pre_save, sender=Product, dispatch_uid="company_product_pre_save")
    pre_save.connect(finding_pre_save, sender=Finding, dispatch_uid="company_finding_pre_save")
    # every upstream caller reaches these as ra_helper.<name> (test_upstream_contract checks), so the wrapper applies
    ra_helper.add_findings_to_risk_acceptance = guard(ra_helper.add_findings_to_risk_acceptance, when=adds_findings)
    ra_helper.simple_risk_accept = guard(ra_helper.simple_risk_accept)
    ra_helper.reinstate = guard(ra_helper.reinstate, when=lambda risk_acceptance, *_args, **_kwargs: bool(risk_acceptance.expiration_date_handled))
    ra_helper.expire_now = allow_expiry(ra_helper.expire_now)
    # refused before anything is copied (copy_engagement has no transaction); the copy needs Risk_Acceptance.copy()
    Engagement.copy = guard(Engagement.copy, when=has_risk_acceptances)
    apply_setting(enabled=settings.COMPANY_RA_APPROVAL_REQUIRED)
    setting_changed.connect(on_setting_changed, dispatch_uid="company_ra_setting_changed")
