"""
company: the company app's only model (migrations in dojo/company/migrations/, never dojo/db_migrations).

A risk acceptance request: someone asks the security team to accept the risk of findings for a number of
days; an approver approves it (dojo/company/risk_requests.py then creates upstream Risk_Acceptance rows,
one per engagement) or rejects it with a reason. A row never changes after its decision, so it is its own
audit record.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class RiskAcceptanceRequest(models.Model):

    """Findings someone asked the security team to risk accept, and the decision."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (PENDING, _("Waiting for approval")),
        (APPROVED, _("Approved")),
        (REJECTED, _("Rejected")),
        (CANCELLED, _("Cancelled")),
    ]

    findings = models.ManyToManyField("dojo.Finding", related_name="company_risk_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    justification = models.TextField()
    days = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    # the upstream risk acceptances an approval created, one per engagement
    risk_acceptances = models.ManyToManyField("dojo.Risk_Acceptance", blank=True, related_name="+")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("approve_riskacceptancerequest", "Can approve risk acceptance requests")]

    def __str__(self):
        return f"#{self.pk}"
