"""
company: risk acceptance requested by a member and approved by the security team (dojo/company/risk_requests.py,
the views company_risk_request, company_risk_inbox and company_risk_decide, the finding page, bulk menu, sidebar and
dashboard entries) and the enforcement on upstream's risk acceptance paths (dojo/company/signals.py, behind
DD_COMPANY_RA_APPROVAL_REQUIRED). Every class pins the setting: the ZainCash env-file turns it on in the dev stack.
"""
import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import crum
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import transaction
from django.db.models.signals import m2m_changed
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

import dojo.risk_acceptance.helper as ra_helper
from dojo.authorization.url_permissions import URL_PERMISSIONS
from dojo.company import ldap as company_ldap
from dojo.company import risk_requests, signals
from dojo.company.models import RiskAcceptanceRequest
from dojo.models import Alerts, Dojo_User, Engagement, Finding, Product, Product_Type, Risk_Acceptance, Test, Test_Type
from dojo.notifications.helper import create_notification
from dojo.utils import grade_debounce_cache_key
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


def tile(html, testid):
    found = re.search(rf'data-testid="{testid}".*?stat-card-value">(\d+)<', html, re.DOTALL)
    return int(found.group(1)) if found else None


class RiskData(DojoTestCase):

    """A product with two engagements (findings f1, f2 in the first, f3 in the second), another product, and users."""

    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.baseline = set(Risk_Acceptance.objects.values_list("id", flat=True))  # the fixture has its own
        self.product_type = Product_Type.objects.create(name="Company RA")
        self.product = Product.objects.create(name="Company RA product", description="x", prod_type=self.product_type)
        self.other = Product.objects.create(name="Company RA other", description="x", prod_type=self.product_type)
        for product in (self.product, self.other):
            # saving findings leaves upstream's product-grade marker, which would skew later pinned query counts
            self.addCleanup(cache.delete, grade_debounce_cache_key(product.id))
        test_type = Test_Type.objects.get_or_create(name="Manual Code Review")[0]
        now = timezone.now()
        self.engagements = [
            Engagement.objects.create(name=name, product=product, target_start=now, target_end=now)
            for name, product in (("e1", self.product), ("e2", self.product), ("e3", self.other))
        ]
        tests = [Test.objects.create(engagement=e, test_type=test_type, target_start=now, target_end=now) for e in self.engagements]
        admin = self.get_test_admin()

        def finding(title, test):
            return Finding.objects.create(title=title, test=test, severity="High", numerical_severity="S1",
                                          reporter=admin, active=True, verified=True)

        self.f1, self.f2, self.f3 = finding("f1", tests[0]), finding("f2", tests[0]), finding("f3", tests[1])
        self.f_other = finding("other", tests[2])
        self.dev = self.user("dev", member=True)
        self.sec = self.user("sec", member=True, approver=True)
        self.far = self.user("far", approver=True)  # an approver without access to the product

    def user(self, username, *, member=False, approver=False):
        user = Dojo_User.objects.create(username=username, first_name=username.title(), last_name="Tester")
        if member:
            self.product.authorized_users.add(user)
        if approver:
            user.groups.add(risk_requests.approver_group())
        return user

    def request(self, findings=None, user=None, days=30):
        return risk_requests.create(findings or [self.f1, self.f2, self.f3], user or self.dev, "compensating control", days)

    def refreshed(self, *findings):
        return [Finding.objects.get(id=finding.id) for finding in findings]

    def new_acceptances(self):
        return Risk_Acceptance.objects.exclude(id__in=self.baseline)


@versioned_fixtures
@override_settings(COMPANY_RA_APPROVAL_REQUIRED=False, OS_MESSAGE_ENABLED=False)
class TestRiskRequests(RiskData):

    def test_request_leaves_findings_active_adds_notes_and_notifies_approvers(self):
        notes = [finding.notes.count() for finding in (self.f1, self.f2, self.f3)]
        self.client.force_login(self.dev)
        with patch("dojo.company.risk_requests.create_notification") as notify:
            response = self.client.post(reverse("company_risk_request"), {
                "finding_to_update": [self.f1.id, self.f2.id, self.f3.id], "justification": "WAF rule in place",
                "days": "90", "return_url": "/finding/open",
            })
        self.assertRedirects(response, "/finding/open", fetch_redirect_response=False)
        request = RiskAcceptanceRequest.objects.get()
        self.assertEqual((request.status, request.days, request.requested_by_id), ("pending", 90, self.dev.id))
        self.assertEqual(set(request.findings.all()), {self.f1, self.f2, self.f3})
        for finding, count in zip(self.refreshed(self.f1, self.f2, self.f3), notes, strict=True):
            self.assertTrue(finding.active)
            self.assertFalse(finding.risk_accepted)
            self.assertEqual(finding.notes.count(), count + 1)
            self.assertIn("WAF rule in place", finding.notes.order_by("-id").first().entry)
        self.assertFalse(self.new_acceptances().exists())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "company_risk_requested")
        recipients = notify.call_args.kwargs["recipients"]
        self.assertIn("sec", recipients)
        self.assertNotIn("far", recipients)  # no access to the product
        self.assertNotIn("dev", recipients)

    def test_finding_page_get_and_bulk_post_show_the_form(self):
        self.client.force_login(self.dev)
        response = self.client.get(reverse("company_risk_request"), {"finding": self.f1.id})
        self.assertTemplateUsed(response, "company/risk_request.html")
        self.assertContains(response, f'name="finding_to_update" value="{self.f1.id}"')
        self.assertContains(response, '<option value="30"')
        self.assertContains(response, f'value="{reverse("view_finding", args=(self.f1.id,))}"')  # back to the finding
        response = self.client.post(reverse("company_risk_request"), {"finding_to_update": [self.f1.id, self.f3.id]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="finding_to_update" value="{self.f3.id}"')
        self.assertFalse(RiskAcceptanceRequest.objects.exists())
        # a justification is required
        response = self.client.post(reverse("company_risk_request"), {"finding_to_update": [self.f1.id], "justification": "", "days": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RiskAcceptanceRequest.objects.exists())

    def test_findings_that_cannot_be_requested_are_left_out(self):
        self.request([self.f1])
        Finding.objects.filter(id=self.f2.id).update(active=False)
        off = Product.objects.create(name="Company RA off", description="x", prod_type=self.product_type, enable_full_risk_acceptance=False)
        self.f3.test.engagement.product = off
        self.f3.test.engagement.save()
        eligible, skipped = risk_requests.split_eligible(self.refreshed(self.f1, self.f2, self.f3, self.f_other))
        self.assertEqual(eligible, [self.f_other])
        self.assertEqual([str(reason) for _finding, reason in skipped],
                         ["already waiting for approval", "not open, or already risk accepted", "risk acceptance is turned off for its product"])
        self.assertIsNone(risk_requests.create(self.refreshed(self.f2), self.dev, "x", 30))

    def test_one_finding_not_editable_gives_403_and_creates_nothing(self):
        self.client.force_login(self.dev)
        response = self.client.post(reverse("company_risk_request"), {
            "finding_to_update": [self.f1.id, self.f_other.id], "justification": "x", "days": "30"})
        self.assertTemplateUsed(response, "403.html")
        self.assertFalse(RiskAcceptanceRequest.objects.exists())
        response = self.client.get(reverse("company_risk_request"), {"finding": self.f1.id, "return_url": "https://evil.example/"})
        self.assertEqual(response.status_code, 400)

    def test_approve_across_two_engagements_creates_two_risk_acceptances(self):
        request = self.request(days=30)
        self.client.force_login(self.sec)
        with patch("dojo.company.risk_requests.create_notification") as notify:
            response = self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "approve", "note": "ok"})
        self.assertRedirects(response, reverse("company_risk_inbox") + "?tab=waiting", fetch_redirect_response=False)
        request.refresh_from_db()
        self.assertEqual((request.status, request.decided_by_id, request.decision_note), ("approved", self.sec.id, "ok"))
        self.assertIsNotNone(request.decided_at)
        acceptances = list(request.risk_acceptances.order_by("id"))
        self.assertEqual(len(acceptances), 2)
        self.assertEqual([a.engagement for a in acceptances], self.engagements[:2])
        expected_expiry = timezone.now() + timedelta(days=30)
        for acceptance in acceptances:
            self.assertEqual(acceptance.owner_id, self.dev.id)
            self.assertEqual(acceptance.accepted_by, "Sec Tester (sec)")
            self.assertEqual(acceptance.decision, Risk_Acceptance.TREATMENT_ACCEPT)
            self.assertEqual(acceptance.decision_details, "compensating control")
            self.assertLess(abs(acceptance.expiration_date - expected_expiry), timedelta(minutes=5))
            self.assertIn("approved this risk acceptance", acceptance.notes.get().entry)
        self.assertEqual(set(acceptances[0].accepted_findings.all()), {self.f1, self.f2})
        for finding in self.refreshed(self.f1, self.f2, self.f3):
            self.assertFalse(finding.active)
            self.assertTrue(finding.risk_accepted)
        self.assertEqual(notify.call_args.kwargs["event"], "company_risk_decided")
        self.assertEqual(notify.call_args.kwargs["recipients"], ["dev"])

    def test_double_approve_is_a_no_op(self):
        request = self.request()
        self.assertEqual(len(risk_requests.approve(request, self.sec)), 2)
        self.assertIsNone(risk_requests.approve(RiskAcceptanceRequest.objects.get(id=request.id), self.sec))
        self.client.force_login(self.sec)
        response = self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "approve"}, follow=True)
        self.assertContains(response, "1 request had been decided already.")
        self.assertEqual(self.new_acceptances().count(), 2)
        self.assertFalse(risk_requests.reject(RiskAcceptanceRequest.objects.get(id=request.id), self.sec, "late"))

    def test_reject_needs_a_reason_keeps_findings_active_and_adds_notes(self):
        request = self.request()
        self.client.force_login(self.sec)
        self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "reject", "note": " "})
        self.assertEqual(RiskAcceptanceRequest.objects.get(id=request.id).status, "pending")
        with patch("dojo.company.risk_requests.create_notification") as notify:
            self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "reject", "note": "fix it instead"})
        request.refresh_from_db()
        self.assertEqual((request.status, request.decision_note), ("rejected", "fix it instead"))
        for finding in self.refreshed(self.f1, self.f2, self.f3):
            self.assertTrue(finding.active)
            self.assertFalse(finding.risk_accepted)
            self.assertIn("rejected risk acceptance request", finding.notes.order_by("-id").first().entry)
        self.assertFalse(self.new_acceptances().exists())
        self.assertIn("fix it instead", notify.call_args.kwargs["title"])
        self.assertEqual(notify.call_args.kwargs["recipients"], ["dev"])

    def test_nobody_decides_their_own_request(self):
        for requester in (self.sec, self.get_test_admin()):  # an approver, and a superuser
            with self.subTest(requester=requester.username):
                request = self.request([self.f1], user=requester)
                self.client.force_login(requester)
                response = self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "approve"})
                self.assertTemplateUsed(response, "403.html")
                with self.assertRaises(PermissionDenied):
                    risk_requests.approve(request, requester)
                self.assertEqual(RiskAcceptanceRequest.objects.get(id=request.id).status, "pending")
                self.assertNotIn(request, risk_requests.waiting_for(requester))
                request.delete()

    def test_only_approvers_with_access_to_every_finding_decide(self):
        request = self.request()
        for user in (self.dev, self.far):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "approve"})
                self.assertTemplateUsed(response, "403.html")
        self.assertEqual(RiskAcceptanceRequest.objects.get(id=request.id).status, "pending")
        self.assertNotIn(request, risk_requests.waiting_for(self.far))
        self.assertIn(request, risk_requests.waiting_for(self.sec))
        # a bulk approve with one request the approver may not decide changes nothing
        other = self.request([self.f_other], user=self.get_test_admin())
        self.client.force_login(self.sec)
        response = self.client.post(reverse("company_risk_decide"), {"request": [request.id, other.id], "action": "approve"})
        self.assertTemplateUsed(response, "403.html")
        self.assertEqual(set(RiskAcceptanceRequest.objects.values_list("status", flat=True)), {"pending"})

    def test_requester_cancels_while_pending(self):
        request = self.request()
        self.client.force_login(self.sec)
        self.assertTemplateUsed(self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "cancel"}), "403.html")
        self.client.force_login(self.dev)
        self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "cancel"})
        self.assertEqual(RiskAcceptanceRequest.objects.get(id=request.id).status, "cancelled")
        self.assertIn("cancelled risk acceptance request", self.refreshed(self.f1)[0].notes.order_by("-id").first().entry)
        self.assertIsNone(risk_requests.approve(RiskAcceptanceRequest.objects.get(id=request.id), self.sec))

    def test_an_error_puts_the_request_back_to_pending(self):
        request = self.request()
        with patch("dojo.risk_acceptance.helper.add_findings_to_risk_acceptance", side_effect=RuntimeError("jira down")), \
                self.assertRaises(RuntimeError):
            risk_requests.approve(request, self.sec, "ok")
        request.refresh_from_db()
        self.assertEqual((request.status, request.decided_by, request.decided_at, request.decision_note), ("pending", None, None, ""))
        self.assertFalse(self.new_acceptances().exists())  # no transaction: the empty acceptance is deleted by hand
        # failing at the second engagement: the first acceptance keeps its findings and its link, approving again adds the rest
        real, calls = ra_helper.add_findings_to_risk_acceptance, []

        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                msg = "jira down"
                raise RuntimeError(msg)
            return real(*args, **kwargs)

        with patch("dojo.risk_acceptance.helper.add_findings_to_risk_acceptance", side_effect=fail_second), \
                self.assertRaises(RuntimeError):
            risk_requests.approve(request, self.sec, "ok")
        request.refresh_from_db()
        self.assertEqual(request.status, "pending")
        self.assertEqual([set(a.accepted_findings.all()) for a in request.risk_acceptances.all()], [{self.f1, self.f2}])
        self.assertTrue(self.refreshed(self.f3)[0].active)
        self.assertEqual(len(risk_requests.approve(request, self.sec)), 1)  # approving again is safe
        self.assertEqual(request.risk_acceptances.count(), 2)
        self.assertTrue(self.refreshed(self.f3)[0].risk_accepted)

    def test_inbox_tabs(self):
        request = self.request()
        self.client.force_login(self.sec)
        response = self.client.get(reverse("company_risk_inbox"))
        self.assertEqual(response.context["tab"], "waiting")
        self.assertContains(response, f'data-testid="company-risk-waiting-{request.id}"')
        self.assertContains(response, f'href="{reverse("view_finding", args=(self.f3.id,))}"')
        self.assertContains(response, "severity-High")
        self.assertContains(response, 'data-testid="company-risk-approve-selected"')

        self.client.force_login(self.dev)
        response = self.client.get(reverse("company_risk_inbox"), {"tab": "waiting"})
        self.assertEqual(response.context["tabs"], ["mine", "active", "expiring"])
        self.assertEqual(response.context["tab"], "mine")
        self.assertContains(response, f'data-testid="company-risk-mine-{request.id}"')
        self.assertContains(response, 'data-testid="company-risk-cancel"')

        risk_requests.approve(request, self.sec)
        Risk_Acceptance.objects.filter(engagement=self.engagements[1]).update(expiration_date=timezone.now() + timedelta(days=90))
        near, far = Risk_Acceptance.objects.get(engagement=self.engagements[0]), Risk_Acceptance.objects.get(engagement=self.engagements[1])
        active = self.client.get(reverse("company_risk_inbox"), {"tab": "active"})
        expiring = self.client.get(reverse("company_risk_inbox"), {"tab": "expiring"})
        for acceptance in (near, far):
            self.assertContains(active, f'data-testid="company-risk-acceptance-{acceptance.id}"')
        self.assertContains(expiring, f'data-testid="company-risk-acceptance-{near.id}"')
        self.assertNotContains(expiring, f'data-testid="company-risk-acceptance-{far.id}"')
        self.assertContains(active, reverse("view_risk_acceptance", args=(self.engagements[0].id, near.id)))
        # read-only and scoped: someone without the product sees none of them
        self.client.force_login(self.far)
        self.assertNotContains(self.client.get(reverse("company_risk_inbox"), {"tab": "active"}), "company-risk-acceptance-")

    def test_my_requests_hide_findings_the_requester_can_no_longer_open(self):
        request = self.request([self.f1])
        self.client.force_login(self.dev)
        link = f'href="{reverse("view_finding", args=(self.f1.id,))}"'
        self.assertContains(self.client.get(reverse("company_risk_inbox"), {"tab": "mine"}), link)
        self.product.authorized_users.remove(self.dev)
        response = self.client.get(reverse("company_risk_inbox"), {"tab": "mine"})
        self.assertContains(response, f'data-testid="company-risk-mine-{request.id}"')
        self.assertNotContains(response, link)
        self.assertNotContains(response, ">f1<")

    def test_finding_page_button_and_pending_badge(self):
        page = reverse("view_finding", args=(self.f1.id,))
        self.client.force_login(self.dev)
        html = self.client.get(page).content.decode()
        self.assertIn('<a class="btn btn-default btn-sm" href="' + reverse("company_risk_request") + f"?finding={self.f1.id}&amp;return_url=", html)
        self.assertIn("Request risk acceptance", html)
        self.request([self.f1])
        html = self.client.get(page).content.decode()
        self.assertIn('data-testid="company-risk-pending"', html)
        self.assertNotIn('data-testid="company-risk-request"', html)
        # no button when the product has full risk acceptance off, or for someone who may not edit the finding
        Product.objects.filter(id=self.product.id).update(enable_full_risk_acceptance=False)
        self.assertNotIn("company-risk-request", self.client.get(reverse("view_finding", args=(self.f2.id,))).content.decode())
        self.client.force_login(self.far)
        self.assertNotIn("company-risk-request", self.client.get(reverse("view_finding", args=(self.f_other.id,))).content.decode())

    def test_bulk_menu_form(self):
        self.client.force_login(self.get_test_admin())
        html = self.client.get(reverse("open_findings")).content.decode()
        self.assertIn(f'<form method="post" action="{reverse("company_risk_request")}" class="company-bulk-assign" data-testid="company-bulk-risk-form">', html)
        self.assertIn('<button type="submit" class="btn btn-default btn-sm" data-testid="company-bulk-risk">', html)

    def test_sidebar_count_and_dashboard_tiles(self):
        self.request()
        self.client.force_login(self.sec)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertRegex(html, r'data-testid="company-nav-risk-count"[^>]*>1<')
        self.assertEqual(tile(html, "company-tile-risk-waiting"), 1)
        self.assertEqual(tile(html, "company-tile-risk-mine"), 0)

        self.client.force_login(self.dev)
        with patch("dojo.company.risk_requests.waiting_for") as waiting:
            html = self.client.get(reverse("dashboard")).content.decode()
        waiting.assert_not_called()  # the count query runs for approvers only
        self.assertIn('data-testid="company-nav-risk"', html)
        self.assertNotIn("company-nav-risk-count", html)
        self.assertEqual(tile(html, "company-tile-risk-mine"), 1)
        self.assertIsNone(tile(html, "company-tile-risk-waiting"))

    def test_approvers_come_from_the_group_without_staff(self):
        self.assertFalse(self.sec.is_staff)
        self.assertTrue(risk_requests.is_approver(Dojo_User.objects.get(id=self.sec.id)))
        self.assertFalse(risk_requests.is_approver(self.dev))
        self.sec.groups.remove(Group.objects.get(name=risk_requests.APPROVER_GROUP))
        self.assertFalse(risk_requests.is_approver(Dojo_User.objects.get(id=self.sec.id)))
        # staff alone is no approver (upstream's configuration check would pass it); superusers are
        Dojo_User.objects.filter(id=self.dev.id).update(is_staff=True)
        self.assertFalse(risk_requests.is_approver(Dojo_User.objects.get(id=self.dev.id)))
        self.assertTrue(risk_requests.is_approver(self.get_test_admin()))

    def test_approvers_for_reads_candidates_in_one_query(self):
        self.user("inactive", member=True, approver=True)
        Dojo_User.objects.filter(username="inactive").update(is_active=False)
        with self.assertNumQueries(3):  # candidates, then the product memberships (direct, through the product type)
            approvers = risk_requests.approvers_for([self.f1, self.f3])
        names = {user.username for user in approvers}
        self.assertIn("sec", names)
        self.assertTrue(names - {"sec"} <= set(Dojo_User.objects.filter(is_superuser=True).values_list("username", flat=True)))

    def test_notifications_reach_approvers_requester_and_escalation_mailbox(self):
        with self.settings(COMPANY_ESCALATION_EMAILS=["soc@company.test"], COMPANY_ESCALATION_EVENTS=["company_risk_requested"]):
            request = self.request()
        self.assertTrue(Alerts.objects.filter(user_id=self.sec, url__contains=reverse("company_risk_inbox")).exists())
        self.assertFalse(Alerts.objects.filter(user_id=self.far).exists())
        self.assertEqual(len([m for m in mail.outbox if m.to == ["soc@company.test"]]), 1)
        risk_requests.approve(request, self.sec)
        self.assertTrue(Alerts.objects.filter(user_id=self.dev, title__contains="approved your risk acceptance request").exists())

    def test_expiration_notice_also_reaches_the_owner(self):
        acceptance = Risk_Acceptance.objects.create(name="ra", owner=self.dev)
        self.engagements[0].risk_acceptance.add(acceptance)
        create_notification(event="risk_acceptance_expiration", title="expires soon", risk_acceptance=acceptance,
                            engagement=self.engagements[0], product=self.product, url="/")
        self.assertEqual(Alerts.objects.filter(user_id=self.dev, title="expires soon").count(), 1)

    @override_settings(COMPANY_LDAP_RISK_APPROVER_GROUP="cn=dojo-security,ou=groups,dc=company,dc=test")
    def test_directory_decides_the_approver_role_and_never_staff(self):
        class LdapUser:
            group_dns = {"CN=dojo-security,ou=groups,dc=company,dc=test"}

        self.assertTrue(company_ldap.sync_risk_approver(self.dev, LdapUser()))
        self.assertTrue(risk_requests.is_approver(Dojo_User.objects.get(id=self.dev.id)))
        LdapUser.group_dns = set()
        self.assertFalse(company_ldap.sync_risk_approver(self.dev, LdapUser()))
        dev = Dojo_User.objects.get(id=self.dev.id)
        self.assertFalse(risk_requests.is_approver(dev))
        self.assertFalse(dev.is_staff)
        with self.settings(COMPANY_LDAP_RISK_APPROVER_GROUP=""):
            self.assertIsNone(company_ldap.sync_risk_approver(self.sec, LdapUser()))  # without the setting, nothing changes
        self.assertTrue(risk_requests.is_approver(Dojo_User.objects.get(id=self.sec.id)))

    def test_company_migrations_are_complete(self):
        # the company app's own migrations (dojo/company/migrations), never dojo/db_migrations; exits 1 on a missing one
        call_command("makemigrations", "company", "--check", "--dry-run", stdout=StringIO())

    def test_company_teams_grants_and_revokes_the_approver_role(self):
        out = StringIO()
        call_command("company_teams", "--approver", "DEV", stdout=out)
        self.assertTrue(risk_requests.is_approver(Dojo_User.objects.get(id=self.dev.id)))
        call_command("company_teams", "list", stdout=out)
        self.assertIn("risk-approvers: dev, far, sec", out.getvalue())
        call_command("company_teams", "--no-approver", "dev", stdout=out)
        self.assertFalse(risk_requests.is_approver(Dojo_User.objects.get(id=self.dev.id)))


@versioned_fixtures
@override_settings(COMPANY_RA_APPROVAL_REQUIRED=True, OS_MESSAGE_ENABLED=False)
class TestEnforcement(RiskData):

    """Approval required: a member (dev) cannot accept risk on any upstream path; f2 sits in an existing acceptance."""

    def setUp(self):
        super().setUp()
        self.acceptance = Risk_Acceptance.objects.create(name="existing", owner=self.dev, expiration_date=timezone.now() + timedelta(days=10))
        self.engagements[0].risk_acceptance.add(self.acceptance)
        ra_helper.add_findings_to_risk_acceptance(None, self.acceptance, [self.f2])  # no current user: exempt
        self.api = APIClient()
        self.api.force_authenticate(user=self.dev)

    def assert_f1_untouched(self):
        f1 = self.refreshed(self.f1)[0]
        self.assertTrue(f1.active)
        self.assertFalse(f1.risk_accepted)
        self.assertFalse(f1.risk_acceptance_set.exists())

    def add_form(self, owner):
        return {"name": "self service", "accepted_findings": [self.f1.id], "recommendation": "A", "decision": "A", "owner": owner.id}

    def test_ui_add_is_refused_by_the_page_check_and_by_the_save(self):
        self.client.force_login(self.dev)
        url = reverse("add_risk_acceptance", args=(self.engagements[0].id,))
        self.assertTemplateUsed(self.client.post(url, self.add_form(self.dev)), "403.html")
        self.assert_f1_untouched()
        # without the friendlier URL check the pre_save guard still refuses
        signals.apply_url_permissions(enabled=False)
        self.addCleanup(signals.apply_url_permissions, enabled=True)
        self.assertTemplateUsed(self.client.post(url, self.add_form(self.dev)), "403.html")
        self.assert_f1_untouched()
        self.assertEqual(self.new_acceptances().count(), 1)

    def test_view_post_add_findings_is_refused(self):
        self.client.force_login(self.dev)
        url = reverse("view_risk_acceptance", args=(self.engagements[0].id, self.acceptance.id))
        response = self.client.post(url, {"add_findings": "1", "accepted_findings": [self.f1.id]})
        self.assertTemplateUsed(response, "403.html")
        self.assert_f1_untouched()

    def test_view_post_edit_cannot_change_guarded_fields(self):
        self.client.force_login(self.dev)
        url = reverse("view_risk_acceptance", args=(self.engagements[0].id, self.acceptance.id))
        form = {"decision": "A", "recommendation": "F", "name": "existing", "owner": self.dev.id,
                "expiration_date": timezone.localtime(self.acceptance.expiration_date).strftime("%Y-%m-%d %H:%M:%S")}  # reactivate_expired unticked
        self.assertTemplateUsed(self.client.post(url, form), "403.html")
        self.assertTrue(Risk_Acceptance.objects.get(id=self.acceptance.id).reactivate_expired)

    def test_api_post_is_refused(self):
        response = self.api.post(reverse("risk_acceptance-list"), {**self.add_form(self.dev), "accepted_by": "me"}, format="json")
        self.assertContains(response, "Only the security team accepts risk", status_code=403)
        self.assert_f1_untouched()
        self.assertEqual(self.new_acceptances().count(), 1)

    def test_api_patch_cannot_extend_or_make_permanent(self):
        url = reverse("risk_acceptance-detail", args=(self.acceptance.id,))
        before = Risk_Acceptance.objects.get(id=self.acceptance.id)
        for payload in ({"expiration_date": (timezone.now() + timedelta(days=400)).isoformat()}, {"expiration_date": None},
                        {"reactivate_expired": False}, {"accepted_by": "someone else"}, {"accepted_findings": [self.f2.id, self.f1.id]},
                        {"expiration_date_handled": timezone.now().isoformat()}):  # handled: the expiry job would skip it
            with self.subTest(payload=payload):
                with transaction.atomic():  # an m2m refusal aborts the m2m atomic block, which has no savepoint of its own
                    response = self.api.patch(url, payload, format="json")
                self.assertContains(response, "Only the security team accepts risk", status_code=403)
                after = Risk_Acceptance.objects.get(id=self.acceptance.id)
                self.assertEqual((after.expiration_date, after.reactivate_expired, after.accepted_by, after.expiration_date_handled),
                                 (before.expiration_date, before.reactivate_expired, before.accepted_by, None))
                self.assert_f1_untouched()

    def test_api_patch_of_other_fields_and_removing_findings_still_work(self):
        ra_helper.add_findings_to_risk_acceptance(None, self.acceptance, [self.f1])  # no current user: exempt
        url = reverse("risk_acceptance-detail", args=(self.acceptance.id,))
        response = self.api.patch(url, {"accepted_findings": [self.f2.id]}, format="json")  # f1 dropped
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_f1_untouched()  # active again, in no acceptance
        # upstream's PATCH drops every finding it is not sent, so the name edit sends f2 along
        response = self.api.patch(url, {"name": "renamed", "accepted_findings": [self.f2.id]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Risk_Acceptance.objects.get(id=self.acceptance.id).name, "renamed")
        self.assertTrue(self.refreshed(self.f2)[0].risk_accepted)

    def test_api_reinstate_of_an_expired_acceptance_is_refused_before_findings_change(self):
        ra_helper.expire_now(self.acceptance)  # no current user: the expiry job
        self.assertTrue(self.refreshed(self.f2)[0].active)
        response = self.api.post(reverse("risk_acceptance-reinstate", args=(self.acceptance.id,)), {}, format="json")
        self.assertContains(response, "Only the security team accepts risk", status_code=403)
        f2 = self.refreshed(self.f2)[0]
        self.assertTrue(f2.active)
        self.assertFalse(f2.risk_accepted)
        # nor marked unexpired by hand
        response = self.api.patch(reverse("risk_acceptance-detail", args=(self.acceptance.id,)), {"expiration_date_handled": None}, format="json")
        self.assertContains(response, "Only the security team accepts risk", status_code=403)
        self.assertTrue(Risk_Acceptance.objects.get(id=self.acceptance.id).is_expired)

    def test_simple_acceptance_cannot_be_switched_on_or_used(self):
        response = self.api.patch(reverse("product-detail", args=(self.product.id,)), {"enable_simple_risk_acceptance": True}, format="json")
        self.assertContains(response, "Only the security team accepts risk", status_code=403)
        self.assertFalse(Product.objects.get(id=self.product.id).enable_simple_risk_acceptance)
        with crum.impersonate(self.dev), self.assertRaises(PermissionDenied):
            Product.objects.create(name="sneaky", description="x", prod_type=self.product_type, enable_simple_risk_acceptance=True)
        # switched on by someone else (no current user here): members still cannot use it
        Product.objects.filter(id=self.product.id).update(enable_simple_risk_acceptance=True)
        self.client.force_login(self.dev)
        self.assertTemplateUsed(self.client.post(reverse("simple_risk_accept_finding", args=(self.f1.id,))), "403.html")
        response = self.api.patch(reverse("finding-detail", args=(self.f1.id,)), {"risk_accepted": True, "active": False}, format="json")
        self.assertContains(response, "Only the security team accepts risk", status_code=403)
        self.assert_f1_untouched()
        # an edit that leaves the flag on is fine
        with crum.impersonate(self.dev):
            Product.objects.get(id=self.product.id).save()

    def test_members_can_still_expire(self):
        self.client.force_login(self.dev)
        response = self.client.post(reverse("expire_risk_acceptance", args=(self.engagements[0].id, self.acceptance.id)))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Risk_Acceptance.objects.get(id=self.acceptance.id).is_expired)
        f2 = self.refreshed(self.f2)[0]
        self.assertTrue(f2.active)
        self.assertFalse(f2.risk_accepted)

    def test_findings_members_save_as_risk_accepted_are_saved_open(self):
        # scan import and re-import take risk accepted from the report, copies keep it
        with crum.impersonate(self.dev):
            imported = Finding.objects.create(title="from report", test=self.f1.test, severity="High", numerical_severity="S1",
                                              reporter=self.dev, active=False, risk_accepted=True)
            f1 = self.refreshed(self.f1)[0]
            f1.risk_accepted, f1.active = True, False  # the re-importer on a report item marked accepted
            f1.save_no_options()
            copy = self.refreshed(self.f2)[0].copy()
            f2 = self.refreshed(self.f2)[0]
            f2.title = "Edited"
            f2.save_no_options()
        for finding in self.refreshed(imported, copy):
            self.assertEqual((finding.active, finding.risk_accepted), (True, False))
        self.assert_f1_untouched()
        f2 = self.refreshed(self.f2)[0]
        self.assertEqual((f2.active, f2.risk_accepted, f2.title), (False, True, "Edited"))
        # approvers and the expiry job (no user) keep the flag
        with crum.impersonate(self.sec):
            by_sec = Finding.objects.create(title="sec", test=self.f1.test, severity="High", numerical_severity="S1",
                                            reporter=self.sec, active=False, risk_accepted=True)
        self.assertTrue(self.refreshed(by_sec)[0].risk_accepted)

    def test_copying_an_engagement_with_risk_acceptances_is_refused_before_anything_is_copied(self):
        engagements = Engagement.objects.count()
        with crum.impersonate(self.dev), self.assertRaises(PermissionDenied):
            self.engagements[0].copy()
        self.assertEqual(Engagement.objects.count(), engagements)
        with crum.impersonate(self.dev):
            self.engagements[1].copy()  # no risk acceptance: copied
        self.assertEqual(Engagement.objects.count(), engagements + 1)

    def test_the_celery_expiry_job_still_works(self):
        self.system_settings(risk_acceptance_notify_before_expiration=0)
        Risk_Acceptance.objects.filter(id=self.acceptance.id).update(expiration_date=timezone.now() - timedelta(days=1))
        ra_helper.expiration_handler()
        self.assertTrue(Risk_Acceptance.objects.get(id=self.acceptance.id).is_expired)
        self.assertTrue(self.refreshed(self.f2)[0].active)

    def test_approvers_and_the_company_service_pass(self):
        request = self.request([self.f1, self.f3])
        self.client.force_login(self.sec)
        self.client.post(reverse("company_risk_decide"), {"request": request.id, "action": "approve"})
        self.assertEqual(RiskAcceptanceRequest.objects.get(id=request.id).status, "approved")
        self.assertTrue(self.refreshed(self.f1)[0].risk_accepted)
        response = self.client.post(reverse("add_risk_acceptance", args=(self.engagements[1].id,)), {
            "name": "by the security team", "accepted_findings": [], "recommendation": "A", "decision": "A", "owner": self.sec.id})
        self.assertEqual(response.status_code, 200)  # not the 403 page: for an approver the form renders again (no finding chosen)

    def test_upstream_add_links_are_hidden_for_members_only(self):
        page = reverse("view_finding", args=(self.f1.id,))
        self.client.force_login(self.dev)
        self.assertContains(self.client.get(page), 'id="company-ra-restricted"')
        self.client.force_login(self.sec)
        self.assertNotContains(self.client.get(page), 'id="company-ra-restricted"')
        with self.settings(COMPANY_RA_APPROVAL_REQUIRED=False):
            self.client.force_login(self.dev)
            self.assertNotContains(self.client.get(page), 'id="company-ra-restricted"')

    def test_url_permissions_follow_the_setting(self):
        for name in signals.GUARDED_URLS:
            self.assertIn(signals.APPROVER_CHECK, URL_PERMISSIONS[name])
        self.assertNotIn(signals.APPROVER_CHECK, URL_PERMISSIONS["expire_risk_acceptance"])
        self.assertTrue(m2m_changed.has_listeners(Risk_Acceptance.accepted_findings.through))
        with self.settings(COMPANY_RA_APPROVAL_REQUIRED=False):
            for name in signals.GUARDED_URLS:
                self.assertNotIn(signals.APPROVER_CHECK, URL_PERMISSIONS[name])
            self.assertFalse(m2m_changed.has_listeners(Risk_Acceptance.accepted_findings.through))
        self.assertTrue(m2m_changed.has_listeners(Risk_Acceptance.accepted_findings.through))

    def test_guards_cost_no_query_without_a_signed_in_user(self):
        acceptance = Risk_Acceptance.objects.get(id=self.acceptance.id)
        product = Product.objects.get(id=self.product.id)
        product.enable_simple_risk_acceptance = True
        finding = Finding.objects.only("id").get(id=self.f1.id)
        with self.assertNumQueries(0):
            signals.risk_acceptance_pre_save(Risk_Acceptance, acceptance)
            signals.product_pre_save(Product, product)
            signals.finding_pre_save(Finding, finding)
            signals.accepted_findings_changed(Risk_Acceptance.accepted_findings.through, action="pre_add")


@versioned_fixtures
@override_settings(COMPANY_RA_APPROVAL_REQUIRED=False, OS_MESSAGE_ENABLED=False)
class TestEnforcementOff(RiskData):

    """Setting off: upstream behaviour, and the guards cost no query even for a signed-in member."""

    def test_members_accept_risk_as_upstream_lets_them(self):
        self.client.force_login(self.dev)
        response = self.client.post(reverse("add_risk_acceptance", args=(self.engagements[0].id,)), {
            "name": "self service", "accepted_findings": [self.f1.id], "recommendation": "A", "decision": "A", "owner": self.dev.id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.refreshed(self.f1)[0].risk_accepted)
        acceptance = self.new_acceptances().get()
        api = APIClient()
        api.force_authenticate(user=self.dev)
        response = api.patch(reverse("risk_acceptance-detail", args=(acceptance.id,)), {"reactivate_expired": False}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        for name in signals.GUARDED_URLS:
            self.assertNotIn(signals.APPROVER_CHECK, URL_PERMISSIONS[name])

    def test_guards_cost_no_query(self):
        acceptance = Risk_Acceptance.objects.create(name="ra", owner=self.dev)
        product = Product.objects.get(id=self.product.id)
        product.enable_simple_risk_acceptance = True
        finding = Finding.objects.only("id").get(id=self.f1.id)  # risk_accepted deferred: reading it would load it
        with crum.impersonate(self.dev), self.assertNumQueries(0):
            signals.risk_acceptance_pre_save(Risk_Acceptance, acceptance)
            signals.product_pre_save(Product, product)
            signals.finding_pre_save(Finding, finding)
            signals.accepted_findings_changed(Risk_Acceptance.accepted_findings.through, action="pre_add")
            signals.guard(lambda _engagement: None, when=signals.has_risk_acceptances)(self.engagements[0])  # `when` not run
        # no M2M listener while off: any listener turns off Django's fast add (one more query per accepted finding)
        self.assertFalse(m2m_changed.has_listeners(Risk_Acceptance.accepted_findings.through))
        with self.assertNumQueries(1):
            acceptance.accepted_findings.add(self.f1)
