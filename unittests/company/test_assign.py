"""
company: assign findings to a team and optionally a person (dojo.company.views.assign, dojo/company/teams.py),
the finding page form, the bulk "Assign…" button, the sidebar links, the "My work" tiles and manage.py company_teams.
"""
import re
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

import dojo
from dojo.company import ldap as company_ldap
from dojo.finding import helper as finding_helper
from dojo.models import Alerts, Dojo_User, Finding
from dojo.utils import grade_debounce_cache_key
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


def tag_names(finding):
    return {tag.name for tag in Finding.objects.get(id=finding.id).tags.all()}


def tile(html, testid):
    found = re.search(rf'data-testid="{testid}".*?stat-card-value">(\d+)<', html, re.DOTALL)
    return int(found.group(1)) if found else None


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestAssign(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        first = Finding.objects.filter(active=True).order_by("id").first()
        self.product = first.test.engagement.product
        self.findings = list(Finding.objects.filter(test__engagement__product=self.product, active=True).order_by("id")[:3])
        self.foreign = Finding.objects.exclude(test__engagement__product=self.product).order_by("id").first()
        # close and reopen save findings, which leaves the product-grading debounce marker (see test_quick_upload)
        self.addCleanup(cache.delete, grade_debounce_cache_key(self.product.id))
        self.team = Group.objects.create(name="team:wallet")
        self.lead = self.user("lead", member=True, team=True)
        self.dev = self.user("dev", member=True, team=True)
        self.outsider = self.user("outsider", team=True)  # in the team, but no access to the product
        self.url = reverse("company_assign")

    def user(self, username, *, member=False, team=False, **fields):
        user = Dojo_User.objects.create(username=username, first_name=username.title(), last_name="Tester", **fields)
        if member:
            self.product.authorized_users.add(user)
        if team:
            user.groups.add(self.team)
        return user

    def post(self, findings, **data):
        return self.client.post(self.url, {"finding_to_update": [f.id for f in findings], "return_url": "/finding/open", **data})

    def test_bulk_assign_sets_tags_notes_and_notifies_without_status_changes(self):
        status = [(f.active, f.verified, f.under_review) for f in self.findings]
        notes = [f.notes.count() for f in self.findings]
        self.client.force_login(self.lead)

        with patch("dojo.company.teams.create_notification") as notify:
            response = self.post(self.findings, team="wallet", person=self.dev.id)

        self.assertRedirects(response, "/finding/open", fetch_redirect_response=False)
        for finding, before, count in zip(self.findings, status, notes, strict=True):
            finding.refresh_from_db()
            self.assertLessEqual({"team:wallet", "assignee:dev"}, tag_names(finding))
            self.assertEqual((finding.active, finding.verified, finding.under_review), before)
            self.assertEqual(finding.notes.count(), count + 1)
            self.assertEqual(finding.notes.order_by("-id").first().entry, "Assigned to wallet / Dev Tester (dev) by Lead Tester (lead)")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "company_finding_assigned")
        self.assertEqual(notify.call_args.kwargs["recipients"], ["dev"])
        self.assertEqual(notify.call_args.kwargs["url"], reverse("open_findings") + "?tags=assignee%3Adev")

        # a new assignment replaces the old one: team only, so the assignee tag goes
        with patch("dojo.company.teams.create_notification") as notify:
            self.post(self.findings[:1], team="wallet")
        self.assertIn("team:wallet", tag_names(self.findings[0]))
        self.assertNotIn("assignee:dev", tag_names(self.findings[0]))
        self.assertIn("assignee:dev", tag_names(self.findings[1]))
        # every member who may open the finding, never the assigner, never a member without product access
        self.assertEqual(notify.call_args.kwargs["recipients"], ["dev"])

    def test_notification_goes_to_the_person_or_the_team(self):
        self.client.force_login(self.lead)
        self.post(self.findings[:2], team="wallet", person=self.dev.id)
        alert = Alerts.objects.get(user_id=self.dev)
        self.assertIn("assigned 2 findings to Dev Tester (dev)", alert.title)
        self.assertIn("tags=assignee%3Adev", alert.url)
        self.assertFalse(Alerts.objects.filter(user_id=self.lead).exists())

        Alerts.objects.all().delete()
        other = self.user("other", member=True, team=True)
        self.post(self.findings[:1], team="wallet")
        self.assertEqual(set(Alerts.objects.values_list("user_id__username", flat=True)), {"dev", "other"})
        self.assertIn("tags=team%3Awallet", Alerts.objects.get(user_id=other).url)

    def test_without_a_team_the_page_shows_the_choices(self):
        self.client.force_login(self.lead)
        response = self.post(self.findings)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "company/assign.html")
        for finding in self.findings:
            self.assertContains(response, f'name="finding_to_update" value="{finding.id}"')
        self.assertContains(response, '<option value="wallet">wallet</option>', html=True)
        self.assertContains(response, 'href="/finding/open"')  # Cancel
        self.assertFalse(any(tag.startswith("team:") for finding in self.findings for tag in tag_names(finding)))

    def test_one_finding_without_edit_permission_gives_403_and_changes_nothing(self):
        self.client.force_login(self.lead)
        with patch("dojo.company.teams.create_notification") as notify:
            response = self.post([*self.findings, self.foreign], team="wallet")
        self.assertTemplateUsed(response, "403.html")  # upstream's handler403 (it answers with status 400)
        for finding in [*self.findings, self.foreign]:
            self.assertFalse({"team:wallet"} & tag_names(finding))
        notify.assert_not_called()
        self.assertTemplateUsed(self.client.post(self.url, {"finding_to_update": ["1x"], "team": "wallet"}), "403.html")

    def test_cap_get_and_foreign_return_url(self):
        self.client.force_login(self.lead)
        response = self.client.post(self.url, {"finding_to_update": [str(n) for n in range(501)]})
        self.assertRedirects(response, reverse("open_findings"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = self.post(self.findings, team="wallet", return_url="https://evil.example/")
        self.assertEqual(response.status_code, 400)
        self.assertTemplateNotUsed(response, "403.html")
        self.assertNotIn("team:wallet", tag_names(self.findings[0]))

    def test_person_without_product_access_is_refused(self):
        self.client.force_login(self.lead)
        response = self.post(self.findings, team="wallet", person=self.outsider.id)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is not an active member of this team")
        self.assertNotIn("team:wallet", tag_names(self.findings[0]))
        response = self.post(self.findings, team="nope")
        self.assertContains(response, "Choose one of the teams.")

    def test_staff_requester_sees_only_members_of_the_product(self):
        staff = self.user("staffer", is_staff=True)
        staff_member = self.user("security", is_staff=True, team=True)  # staff can open every product
        Dojo_User.objects.create(username="gone", is_active=False).groups.add(self.team)
        self.client.force_login(staff)
        response = self.post(self.findings)
        candidates = {user.username for user in response.context["teams"]["wallet"]}
        self.assertEqual(candidates, {"lead", "dev", staff_member.username})

    def test_preprovisioned_directory_user_is_a_candidate(self):
        # what manage.py company_ldap_sync does for a directory user who never logged in
        dave = Dojo_User.objects.create(username="dave")
        dave.set_unusable_password()
        dave.save()
        company_ldap.sync_product_type_access(dave, {f"dojo-pt-{self.product.prod_type.name}"}, prefix="dojo-pt-")
        company_ldap.sync_team_membership(dave, {"dojo-team-Wallet"}, prefix="dojo-team-")
        self.client.force_login(self.lead)
        response = self.post(self.findings)
        self.assertIn("dave", {user.username for user in response.context["teams"]["wallet"]})

    def test_tags_survive_close_and_reopen(self):
        finding = self.findings[0]
        self.client.force_login(self.lead)
        self.post([finding], team="wallet", person=self.dev.id)
        finding.refresh_from_db()
        finding_helper.close_finding(finding=finding, user=self.lead, is_mitigated=True, mitigated=timezone.now(),
                                     mitigated_by=self.lead, false_p=False, out_of_scope=False, duplicate=False)
        self.assertFalse(Finding.objects.get(id=finding.id).active)
        self.assertLessEqual({"team:wallet", "assignee:dev"}, tag_names(finding))
        self.client.force_login(self.get_test_admin())
        self.client.post(reverse("reopen_finding", args=(finding.id,)))
        self.assertTrue(Finding.objects.get(id=finding.id).active)
        self.assertLessEqual({"team:wallet", "assignee:dev"}, tag_names(finding))
        # re-import keeping the tags: test_upstream_contract.test_reimport_keeps_existing_finding_tags

    def test_finding_page_shows_the_assignment_and_the_form(self):
        finding = self.findings[0]
        page = reverse("view_finding", args=(finding.id,))
        self.client.force_login(self.lead)
        response = self.client.get(page)
        self.assertContains(response, 'data-testid="company-assign-finding"')
        self.assertContains(response, "Not assigned")
        self.post([finding], team="wallet", person=self.dev.id, return_url=page)
        response = self.client.get(page)
        self.assertContains(response, "Assigned: wallet / dev")
        self.assertContains(response, f'<input type="hidden" name="return_url" value="{page}">')
        self.assertContains(response, '<button type="submit" class="btn btn-default btn-sm" data-testid="company-assign-save">')
        self.post([finding], team="wallet")
        self.assertContains(self.client.get(page), "Assigned: wallet / nobody")

    def test_bulk_button_only_for_findings_bulk_forms(self):
        # JS-free half of "the endpoint bulk menu gets no company button": company.js appends the button only when
        # form#bulk_change_form posts to a URL the template names; the finding page's endpoint form posts elsewhere.
        self.client.force_login(self.get_test_admin())
        html = self.client.get(reverse("open_findings")).content.decode()
        self.assertIn(f'data-findings-bulk-url="{reverse("finding_bulk_update_all")}"', html)
        self.assertIn('<button type="submit" class="btn btn-default btn-sm" data-testid="company-bulk-assign">', html)
        product_list = reverse("product_open_findings", args=(self.product.id,))
        product_bulk = reverse("finding_bulk_update_all_product", args=(self.product.id,))
        self.assertIn(f'data-findings-bulk-product-url="{product_bulk}"', self.client.get(product_list).content.decode())

        html = self.client.get(reverse("view_finding", args=(self.findings[0].id,))).content.decode()
        action = re.search(r'<form action="([^"]*)" method="post" id="bulk_change_form">', html).group(1)
        self.assertEqual(action, reverse("endpoints_status_bulk", args=(self.findings[0].id,)))
        named = set(re.findall(r'data-findings-bulk(?:-product)?-url="([^"]*)"', html)) - {""}
        self.assertTrue(named)
        self.assertNotIn(action, named)

        script = (Path(dojo.__file__).parent / "static" / "dojo" / "company" / "company.js").read_text(encoding="utf-8")
        for needle in ('"form#bulk_change_form"', 'getAttribute("action")', '"data-findings-bulk-url"',
                       '"data-findings-bulk-product-url"', "samePath(action, url)", "input[type=checkbox].select_one:checked"):
            self.assertIn(needle, script)
        self.assertIn('<script defer src="/static/dojo/company/company.js', html)

    def test_sidebar_links(self):
        self.client.force_login(self.lead)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'href="{reverse("open_findings")}?tags=assignee%3Alead" data-testid="company-nav-my-findings"', html)
        self.assertIn(f'href="{reverse("open_findings")}?tags=team%3Awallet" data-testid="company-nav-team-findings"', html)
        self.lead.groups.add(Group.objects.create(name="team:api"))
        html = self.client.get(reverse("dashboard")).content.decode()
        # one tags parameter holding both: the list's tagulous filter keeps only the last of repeated parameters
        self.assertIn("?tags=team%3Aapi%2C+team%3Awallet", html)
        nobody = Dojo_User.objects.create(username="carol")
        self.client.force_login(nobody)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-testid="company-nav-my-findings"', html)
        self.assertNotIn('data-testid="company-nav-team-findings"', html)

    def test_team_list_filters_exactly(self):
        self.client.force_login(self.lead)
        self.post(self.findings[:2], team="wallet")
        Group.objects.create(name="team:api")
        self.post(self.findings[2:], team="api")
        self.client.force_login(self.get_test_admin())
        url = reverse("open_findings") + "?tags=team%3Aapi%2C+team%3Awallet"
        listed = {f.id for f in self.client.get(url).context["findings"].object_list}
        self.assertEqual(listed, {f.id for f in self.findings})
        listed = {f.id for f in self.client.get(reverse("open_findings") + "?tags=team%3Aapi").context["findings"].object_list}
        self.assertEqual(listed, {self.findings[2].id})

    def test_dashboard_tiles_count_the_users_open_findings(self):
        self.client.force_login(self.lead)
        self.post(self.findings[:2], team="wallet", person=self.dev.id)
        self.post(self.findings[2:], team="wallet")
        Finding.objects.filter(id=self.findings[0].id).update(sla_expiration_date=timezone.now().date() - timedelta(days=1))
        closed = Finding.objects.filter(test__engagement__product=self.product, active=False).first()
        closed.tags.add("assignee:dev")

        self.client.force_login(self.dev)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(tile(html, "company-tile-mine"), 2)
        self.assertEqual(tile(html, "company-tile-team"), 3)
        self.assertEqual(tile(html, "company-tile-overdue"), 1)
        self.assertIn("?tags=assignee%3Adev&amp;outside_of_sla=1", html)
        self.assertIn("Active Engagements", html)  # upstream tiles stay

        self.client.force_login(self.outsider)  # same team, but may not view the product
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(tile(html, "company-tile-team"), 0)
        self.assertEqual(tile(html, "company-tile-mine"), 0)

        self.client.force_login(Dojo_User.objects.create(username="carol"))
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIsNone(tile(html, "company-tile-team"))
        self.assertEqual(tile(html, "company-tile-mine"), 0)


@versioned_fixtures
class TestCompanyTeamsCommand(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def run_command(self, *args):
        out = StringIO()
        call_command("company_teams", *args, stdout=out)
        return out.getvalue()

    def test_create_add_list_remove_delete(self):
        self.assertIn("created: team:wallet", self.run_command("create", "Wallet"))
        self.assertIn("already exists", self.run_command("create", "wallet"))
        self.assertIn("added: admin to team:wallet", self.run_command("add", "wallet", "ADMIN"))
        self.assertIn("team:wallet: admin", self.run_command("list"))
        self.assertTrue(self.get_test_admin().groups.filter(name="team:wallet").exists())
        self.run_command("remove", "wallet", "admin")
        self.assertFalse(self.get_test_admin().groups.filter(name="team:wallet").exists())
        Finding.objects.order_by("id").first().tags.add("team:wallet")
        self.assertIn("1 findings keep the tag team:wallet", self.run_command("delete", "wallet"))
        self.assertFalse(Group.objects.filter(name="team:wallet").exists())
        self.assertIn("no teams", self.run_command("list"))

    def test_rejects_bad_names_unknown_teams_and_users(self):
        for name in ("wallet ops", "team:x", "-x", "x" * 65, "wället"):
            with self.subTest(name=name), self.assertRaises(CommandError):
                self.run_command("create", name)
        with self.assertRaises(CommandError):
            self.run_command("add", "missing", "admin")
        self.run_command("create", "wallet")
        with self.assertRaises(CommandError):
            self.run_command("add", "wallet", "nobody")
