"""
company: Phase 5 polish. "Mark fixed" (dojo.company.views.mark_fixed, DD_COMPANY_MARK_FIXED), Extend and Let expire in
the risk acceptance inbox's Expiring tab (company_risk_expiring), the EN / Arabic switch (company_set_language), the
company translation catalog and the sidebar auto-open in company.js. No test asserts Arabic text: CI images are built
from upstream's Dockerfiles, without the company msgfmt step, so the catalog is not compiled there.
"""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

import dojo
from dojo.company import risk_requests
from dojo.models import Dojo_User, Finding, Note_Type, Product, Risk_Acceptance, UserContactInfo
from dojo.utils import grade_debounce_cache_key
from unittests.company.test_risk_requests import RiskData
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

CATALOG = Path(dojo.__file__).parent / "company" / "locale" / "ar" / "LC_MESSAGES" / "django.po"
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")


def read_catalog(path):
    """[(msgid, msgid_plural, [msgstr, ...], flags)] of a .po file; enough of the format for the company catalog."""
    entries, entry, key, flags = [], None, None, ""
    for raw in [*path.read_text(encoding="utf-8").splitlines(), ""]:
        line = raw.strip()
        if line.startswith("#,"):
            flags = line
        elif line.startswith("msgid "):
            entry = {"msgid": "", "msgid_plural": "", "msgstr": {}, "flags": flags}
            entries.append(entry)
            key, flags = "msgid", ""
        elif line.startswith(("msgid_plural ", "msgstr")):
            key = line.split(" ", 1)[0]
        if line.startswith(("msgid", "msgstr", '"')):
            text = line[line.index('"') + 1:-1].replace('\\"', '"').replace("\\n", "\n")
            if key.startswith("msgstr"):
                entry["msgstr"][key] = entry["msgstr"].get(key, "") + text
            else:
                entry[key] += text
    return entries


@versioned_fixtures
@override_settings(COMPANY_MARK_FIXED=True, COMPANY_RA_APPROVAL_REQUIRED=False, OS_MESSAGE_ENABLED=False)
class TestMarkFixed(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        first = Finding.objects.filter(active=True).order_by("id").first()
        self.product = first.test.engagement.product
        self.findings = list(Finding.objects.filter(test__engagement__product=self.product, active=True).order_by("id")[:2])
        self.foreign = Finding.objects.exclude(test__engagement__product=self.product).order_by("id").first()
        self.addCleanup(cache.delete, grade_debounce_cache_key(self.product.id))  # closing saves findings (see test_quick_upload)
        self.dev = Dojo_User.objects.create(username="fixer", first_name="Fix", last_name="Er")
        self.product.authorized_users.add(self.dev)
        self.page = reverse("view_finding", args=(self.findings[0].id,))

    def post(self, findings, **data):
        if len(findings) == 1 and not data.pop("bulk", False):
            url = reverse("company_mark_fixed", args=(findings[0].id,))
            return self.client.post(url, {"return_url": self.page, **data})
        ids = [finding.id for finding in findings]
        return self.client.post(reverse("company_mark_fixed_bulk"), {"finding_to_update": ids, "return_url": "/finding/open", **data})

    def assert_open(self, *findings):
        for finding in findings:
            self.assertTrue(Finding.objects.get(id=finding.id).active, finding)

    def test_mark_fixed_from_the_finding_page_keeps_the_assignment(self):
        finding = self.findings[0]
        finding.tags.add("team:wallet", "assignee:fixer")
        self.client.force_login(self.dev)
        html = self.client.get(self.page).content.decode()
        self.assertIn('<button type="submit" class="btn btn-default btn-sm" data-testid="company-mark-fixed">', html)
        self.assertIn(f'action="{reverse("company_mark_fixed", args=(finding.id,))}"', html)
        # in the instance language whatever the user's: other people read the note
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        response = self.post([finding])

        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        closed = Finding.objects.get(id=finding.id)
        self.assertEqual((closed.active, closed.is_mitigated, closed.mitigated_by_id), (False, True, self.dev.id))
        self.assertIsNotNone(closed.mitigated)
        self.assertEqual(closed.notes.order_by("-id").first().entry, "Marked fixed")
        self.assertLessEqual({"team:wallet", "assignee:fixer"}, {tag.name for tag in closed.tags.all()})
        # closed: no button any more, and a second POST changes nothing
        self.assertNotIn('data-testid="company-mark-fixed"', self.client.get(self.page).content.decode())
        notes = closed.notes.count()
        self.assertRedirects(self.post([finding]), self.page, fetch_redirect_response=False)
        self.assertEqual(Finding.objects.get(id=finding.id).notes.count(), notes)

    def test_bulk_needs_edit_on_every_finding(self):
        self.client.force_login(self.dev)
        foreign_state = (self.foreign.active, self.foreign.is_mitigated)
        response = self.post([*self.findings, self.foreign])
        self.assertTemplateUsed(response, "403.html")
        self.assert_open(*self.findings)
        foreign = Finding.objects.get(id=self.foreign.id)
        self.assertEqual((foreign.active, foreign.is_mitigated), foreign_state)
        self.assertEqual(self.client.get(reverse("company_mark_fixed_bulk")).status_code, 405)
        self.assertEqual(self.post(self.findings, return_url="https://evil.example/").status_code, 400)
        self.assert_open(*self.findings)

        response = self.post(self.findings)

        self.assertRedirects(response, "/finding/open", fetch_redirect_response=False)
        for finding in self.findings:
            self.assertFalse(Finding.objects.get(id=finding.id).active)

    def test_bulk_menu_form(self):
        self.client.force_login(self.get_test_admin())
        html = self.client.get(reverse("open_findings")).content.decode()
        self.assertIn(f'action="{reverse("company_mark_fixed_bulk")}" class="company-bulk-assign" data-testid="company-bulk-fixed-form"', html)
        self.assertIn('<button type="submit" class="btn btn-default btn-sm" data-testid="company-bulk-fixed">', html)
        self.assertIn('onsubmit="return window.confirm(', html)

    def test_mandatory_note_types_fall_back_to_the_upstream_close_page(self):
        Note_Type.objects.create(name="Fix evidence", description="x", is_mandatory=True, is_active=True)
        self.client.force_login(self.dev)
        response = self.post([self.findings[0]])
        self.assertRedirects(response, reverse("close_finding", args=(self.findings[0].id,)), fetch_redirect_response=False)
        response = self.post(self.findings)
        self.assertRedirects(response, "/finding/open", fetch_redirect_response=False)
        self.assert_open(*self.findings)

    @override_settings(COMPANY_MARK_FIXED=False)
    def test_switched_off(self):
        self.client.force_login(self.get_test_admin())
        self.assertNotIn("company-mark-fixed", self.client.get(self.page).content.decode())
        self.assertNotIn("company-bulk-fixed", self.client.get(reverse("open_findings")).content.decode())
        self.assertTemplateUsed(self.post([self.findings[0]]), "403.html")
        self.assertTemplateUsed(self.post(self.findings), "403.html")
        self.assert_open(*self.findings)


@versioned_fixtures
@override_settings(COMPANY_RA_APPROVAL_REQUIRED=True, OS_MESSAGE_ENABLED=False)
class TestExpiringActions(RiskData):

    """Approval required (the ZainCash setting): approved request of dev, acceptance a1 (f1, f2) and a2 (f3), 30 days."""

    def setUp(self):
        super().setUp()
        risk_requests.approve(self.request(days=30), self.sec)  # no current user here: the guards let it through
        self.a1 = Risk_Acceptance.objects.get(engagement=self.engagements[0])
        self.url = reverse("company_risk_expiring")
        self.tab = reverse("company_risk_inbox") + "?tab=expiring"

    def post(self, user, **data):
        self.client.force_login(user)
        return self.client.post(self.url, {"acceptance": self.a1.id, **data})

    def test_tab_offers_extend_to_approvers_and_let_expire_to_members(self):
        self.client.force_login(self.sec)
        html = self.client.get(self.tab).content.decode()
        self.assertIn('data-testid="company-risk-extend"', html)
        self.assertIn('data-testid="company-risk-expire"', html)
        self.assertIn('<option value="90">', html)
        self.client.force_login(self.dev)
        html = self.client.get(self.tab).content.decode()
        self.assertNotIn('data-testid="company-risk-extend"', html)
        self.assertIn('data-testid="company-risk-expire"', html)

    def test_extend_as_approver(self):
        before = self.a1.expiration_date
        Risk_Acceptance.objects.filter(id=self.a1.id).update(expiration_date_warned=timezone.now())

        response = self.post(self.sec, action="extend", days="90")

        self.assertRedirects(response, self.tab, fetch_redirect_response=False)
        after = Risk_Acceptance.objects.get(id=self.a1.id)
        self.assertLess(abs(after.expiration_date - (before + timedelta(days=90))), timedelta(minutes=1))
        self.assertIsNone(after.expiration_date_warned)  # the heads-up goes out again before the new date
        self.assertIsNone(after.expiration_date_handled)
        note = after.notes.order_by("-id").first().entry
        self.assertIn("extended this risk acceptance", note)
        self.assertIn("by 90 days, until", note)
        for finding in self.refreshed(self.f1, self.f2):
            self.assertTrue(finding.risk_accepted)

    def test_extend_is_refused_for_members_and_unknown_durations(self):
        before = self.a1.expiration_date
        self.assertTemplateUsed(self.post(self.dev, action="extend", days="90"), "403.html")
        self.assertEqual(self.post(self.sec, action="extend", days="7").status_code, 400)
        self.assertEqual(self.post(self.sec, action="extend", days="").status_code, 400)
        self.assertEqual(Risk_Acceptance.objects.get(id=self.a1.id).expiration_date, before)
        # one that never expires stays so (extending would time-box it)
        Risk_Acceptance.objects.filter(id=self.a1.id).update(expiration_date=None)
        self.assertEqual(self.post(self.sec, action="extend", days="30").status_code, 400)
        self.assertIsNone(Risk_Acceptance.objects.get(id=self.a1.id).expiration_date)

    def test_no_extend_once_the_product_switched_full_risk_acceptance_off(self):
        before = self.a1.expiration_date
        Product.objects.filter(id=self.product.id).update(enable_full_risk_acceptance=False)  # both acceptances
        self.client.force_login(self.sec)
        html = self.client.get(self.tab).content.decode()
        self.assertNotIn('data-testid="company-risk-extend"', html)
        self.assertIn('data-testid="company-risk-expire"', html)
        self.assertTemplateUsed(self.post(self.sec, action="extend", days="30"), "403.html")
        self.assertEqual(Risk_Acceptance.objects.get(id=self.a1.id).expiration_date, before)

    def test_let_expire_as_member_with_a_note(self):
        response = self.post(self.dev, action="expire")
        self.assertRedirects(response, self.tab, fetch_redirect_response=False)
        after = Risk_Acceptance.objects.get(id=self.a1.id)
        self.assertTrue(after.is_expired)
        note = after.notes.order_by("-id").first()
        self.assertEqual(note.author, self.dev)
        self.assertIn("expired this risk acceptance (reason: let expire in the risk acceptance inbox)", note.entry)
        for finding in self.refreshed(self.f1, self.f2):
            self.assertTrue(finding.active)
            self.assertFalse(finding.risk_accepted)
        # done already: nothing more happens
        notes = after.notes.count()
        self.post(self.dev, action="expire")
        self.assertEqual(Risk_Acceptance.objects.get(id=self.a1.id).notes.count(), notes)

    def test_nobody_without_edit_on_the_engagement(self):
        before = self.a1.expiration_date
        for action in ("expire", "extend"):
            with self.subTest(action=action):
                self.assertTemplateUsed(self.post(self.far, action=action, days="30"), "403.html")
        after = Risk_Acceptance.objects.get(id=self.a1.id)
        self.assertEqual((after.expiration_date, after.expiration_date_handled), (before, None))
        self.client.force_login(self.sec)
        self.assertEqual(self.client.post(self.url, {"action": "nope", "acceptance": self.a1.id}).status_code, 400)
        self.assertEqual(self.client.get(self.url).status_code, 405)


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestLanguageSwitch(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.user = Dojo_User.objects.create(username="speaker")
        self.url = reverse("company_set_language")

    def test_user_menu_offers_english_and_arabic(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'<form method="post" action="{self.url}" class="company-language" data-testid="company-language-form">', html)
        for code in ("en", "ar"):
            self.assertRegex(html, rf'<button type="submit" name="language" value="{code}" [^>]*class="company-btn company-language-option"')
        self.assertEqual(html.count('name="language"'), 2)

    def test_saves_the_preference_and_sets_the_cookie(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"language": "ar", "return_url": "/finding/open"})
        self.assertRedirects(response, "/finding/open", fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "ar")
        self.assertEqual(UserContactInfo.objects.get(user=self.user).language, "ar")
        response = self.client.post(self.url, {"language": "en"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(UserContactInfo.objects.get(user=self.user).language, "en")
        # another browser: upstream's LanguagePreferenceMiddleware seeds the cookie from the saved preference
        self.client.post(self.url, {"language": "ar"})
        self.client.cookies.clear()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("dashboard")).cookies[settings.LANGUAGE_COOKIE_NAME].value, "ar")

    def test_a_page_rendered_from_a_post_switches_back_to_where_it_came_from(self):
        # the bulk Assign… page answers only POST, so switching there must not send the user back to it with a GET
        def language_form(response):
            html = response.content.decode()
            html = html[html.index('data-testid="company-language-form"'):]
            return html[:html.index("</form>")]

        self.client.force_login(self.get_test_admin())
        finding = Finding.objects.filter(active=True).order_by("id").first()
        response = self.client.post(reverse("company_assign"), {"finding_to_update": [finding.id], "return_url": "/finding/open"})
        self.assertIn('name="return_url" value="/finding/open"', language_form(response))
        response = self.client.get(reverse("open_findings") + "?page=1")
        self.assertIn(f'name="return_url" value="{reverse("open_findings")}?page=1"', language_form(response))

    def test_rejects_unknown_codes_foreign_return_urls_and_gets(self):
        self.client.force_login(self.user)
        for data in ({"language": "xx"}, {"language": ""}, {"language": "ar", "return_url": "https://evil.example/"}):
            with self.subTest(data=data):
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)
        self.assertFalse(UserContactInfo.objects.filter(user=self.user, language="ar").exists())
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"language": "ar"}).status_code, 302)  # to the login page
        self.assertFalse(UserContactInfo.objects.filter(user=self.user).exists())


class TestCatalogAndScript(DojoTestCase):

    def test_arabic_catalog_exists_parses_and_is_complete(self):
        self.assertEqual(Path(settings.LOCALE_PATHS[0]), CATALOG.parents[2])
        entries = read_catalog(CATALOG)
        header, messages = entries[0], entries[1:]
        self.assertEqual(header["msgid"], "")
        self.assertIn("Language: ar\n", header["msgstr"]["msgstr"])
        self.assertIn("Plural-Forms: nplurals=6;", header["msgstr"]["msgstr"])
        self.assertGreater(len(messages), 100)
        # the company catalog is searched first: an English text upstream translates too keeps upstream's translation
        upstream_po = Path(dojo.__file__).parent / "locale" / "ar" / "LC_MESSAGES" / "django.po"
        upstream = {entry["msgid"]: entry["msgstr"] for entry in read_catalog(upstream_po)[1:]}
        for entry in messages:
            if entry["msgid"] in upstream:
                self.assertEqual(entry["msgstr"], upstream[entry["msgid"]], entry["msgid"])
        for entry in messages:
            with self.subTest(msgid=entry["msgid"]):
                self.assertNotIn("fuzzy", entry["flags"])
                translations = list(entry["msgstr"].values())
                self.assertEqual(len(translations), 6 if entry["msgid_plural"] else 1)
                self.assertTrue(all(translations))
                # a placeholder the code does not pass would raise KeyError in Arabic only
                known = set(PLACEHOLDER.findall(entry["msgid"] + entry["msgid_plural"]))
                for text in translations:
                    self.assertLessEqual(set(PLACEHOLDER.findall(text)), known)

    def test_sidebar_auto_open_waits_for_alpine_and_never_throws(self):
        script = (Path(dojo.__file__).parent / "static" / "dojo" / "company" / "company.js").read_text(encoding="utf-8")
        # non-staff only: upstream selenium (a superuser) needs closed sections; the marker is the declutter stylesheet
        self.assertIn('if (!document.getElementById("company-declutter"))', script)
        declutter = Path(dojo.__file__).parent / "company" / "templates" / "company" / "_declutter.html"
        self.assertIn('<style id="company-declutter">', declutter.read_text(encoding="utf-8"))
        for needle in ('typeof window.Alpine === "undefined"', "window.Alpine.$data(section)", '"open" in data',
                       '"alpine:initialized"', '"#dd-sidebar div[x-data]"', "safely(openCurrentSection)"):
            self.assertIn(needle, script)
