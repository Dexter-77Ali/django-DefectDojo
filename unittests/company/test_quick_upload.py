"""
company: one-page scan upload (dojo.company.views.quick_upload), its entry points and the scanner
allowlist command (manage.py company_scan_types).
"""
import re
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from dojo.company.forms import NO_COMPONENT
from dojo.models import Dojo_User, Engagement, Finding, Product, Product_Type, Test, Test_Type
from dojo.tools.factory import PARSERS
from dojo.utils import grade_debounce_cache_key
from unittests.dojo_test_case import DojoTestCase, get_unit_tests_scans_path, versioned_fixtures

ZAP = "ZAP Scan"
FULL = get_unit_tests_scans_path("zap") / "0_zap_sample.xml"
# 0_zap_sample minus one alert plus a new one: re-importing it after FULL closes the missing alert
CHANGED = get_unit_tests_scans_path("zap") / "1_zap_sample_0_and_new_absent.xml"
TABS = re.compile(r"<!-- start of tabs -->(.*?)<!-- end of tabs -->", re.DOTALL)


def report(path, name=None):
    return SimpleUploadedFile(name or path.name, path.read_bytes())


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestQuickUpload(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        product_type = Product_Type.objects.create(name="Company upload")
        self.product = Product.objects.create(name="Company upload app", prod_type=product_type, description="x")
        # Background grading leaves a 30 s debounce marker in the cache, keyed by product id. Fixture loading
        # resets the id sequence, so the next test class's first product reuses this id and would skip its
        # grading (upstream's pinned query counts in test_tag_inheritance_perf notice).
        self.addCleanup(cache.delete, grade_debounce_cache_key(self.product.id))
        self.member = Dojo_User.objects.create(username="company-uploader")
        self.product.authorized_users.add(self.member)
        self.client.force_login(self.member)
        self.url = reverse("company_quick_upload")

    def upload(self, path, *, url=None, name=None, **fields):
        data = {"product": self.product.id, "scan_type": ZAP, "close_old_findings": "on", **fields}
        if url is not None:  # ?test=<id>: product and scanner come from the test
            data.pop("product")
            data.pop("scan_type")
        data["file"] = report(path, name)
        return self.client.post(url or self.url, data, follow=True)

    def engagement(self):
        return Engagement.objects.filter(product=self.product, name=settings.COMPANY_UPLOAD_ENGAGEMENT_NAME).first()

    def closed(self, test):
        return Finding.objects.filter(test=test, is_mitigated=True).count()

    def test_first_upload_creates_engagement_and_test_then_reimports_and_closes(self):
        response = self.upload(FULL)
        engagement = self.engagement()
        self.assertIsNotNone(engagement)
        test = engagement.test_set.get()
        self.assertRedirects(response, reverse("view_test", args=(test.id,)))
        self.assertContains(response, "Upload complete: 4 new, 0 closed, 0 reactivated, 0 unchanged.")
        self.assertEqual(self.closed(test), 0)

        response = self.upload(CHANGED)

        self.assertRedirects(response, reverse("view_test", args=(test.id,)))
        self.assertEqual(engagement.test_set.count(), 1)
        self.assertEqual(Engagement.objects.filter(product=self.product).count(), 1)
        self.assertEqual(self.closed(test), 1)
        self.assertContains(response, "1 closed")

    def test_new_component_creates_new_test(self):
        self.upload(FULL)
        response = self.upload(FULL, component="api")

        tests = self.engagement().test_set.order_by("id")
        self.assertEqual([t.title for t in tests], [None, "api"])
        self.assertRedirects(response, reverse("view_test", args=(tests[1].id,)))
        self.assertEqual(self.closed(tests[0]), 0)

    def test_blank_component_with_two_titled_tests_closes_nothing(self):
        self.upload(FULL, component="api")
        self.upload(FULL, component="web")

        response = self.upload(CHANGED)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick the component this report belongs to")
        self.assertContains(response, '<option value="api">api</option>')
        self.assertContains(response, '<option value="web">web</option>')
        self.assertEqual(self.engagement().test_set.count(), 2)
        self.assertFalse(Finding.objects.filter(test__engagement=self.engagement(), is_mitigated=True).exists())

        response = self.upload(CHANGED, component="web")  # naming the component re-imports into it and closes
        web = self.engagement().test_set.get(title="web")
        self.assertRedirects(response, reverse("view_test", args=(web.id,)))
        self.assertEqual(self.closed(web), 1)
        self.assertEqual(self.closed(self.engagement().test_set.get(title="api")), 0)

    def test_no_component_reaches_the_untitled_test_next_to_titled_ones(self):
        self.upload(FULL)
        self.upload(FULL, component="api")
        untitled = self.engagement().test_set.get(title=None)

        page = self.client.get(self.url, {"product": self.product.id, "scan_type": ZAP})
        self.assertContains(page, f'<option value="{NO_COMPONENT}">No component</option>')
        self.assertContains(page, "x-data=\"{ pick: '' }\"")  # two tests: the uploader picks

        response = self.upload(CHANGED, component=NO_COMPONENT)

        self.assertRedirects(response, reverse("view_test", args=(untitled.id,)))
        self.assertEqual(self.closed(untitled), 1)
        self.assertEqual(self.closed(self.engagement().test_set.get(title="api")), 0)
        self.assertEqual(self.engagement().test_set.count(), 2)

    def test_single_titled_test_is_pre_picked(self):
        self.upload(FULL, component="api")

        page = self.client.get(self.url, {"product": self.product.id})

        self.assertContains(page, f'<option value="{ZAP}" selected>')
        self.assertContains(page, "x-data=\"{ pick: 'api' }\"")
        self.assertNotContains(page, f'value="{NO_COMPONENT}"')

    def test_report_without_findings_warns_that_everything_closed(self):
        self.upload(FULL)

        response = self.upload(get_unit_tests_scans_path("zap") / "empty_2.9.0.xml")

        self.assertContains(response, "0 new, 4 closed")
        self.assertContains(response, "every open finding of this test was closed")

    def test_malformed_ids_are_404(self):
        for query in ({"product": "²"}, {"test": "²"}, {"product": "x"}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url, query).status_code, 404)

    def test_test_mode_reupload_closes_missing_findings(self):
        self.upload(FULL)
        test = self.engagement().test_set.get()
        url = f"{self.url}?test={test.id}"

        page = self.client.get(url)
        self.assertContains(page, f'href="{reverse("re_import_scan_results", args=(test.id,))}"')
        self.assertNotContains(page, 'name="product"')

        response = self.upload(CHANGED, url=url)

        self.assertRedirects(response, reverse("view_test", args=(test.id,)))
        self.assertEqual(self.closed(test), 1)
        self.assertEqual(Test.objects.filter(engagement__product=self.product).count(), 1)

    def test_outsider_and_forged_post_get_403(self):
        self.upload(FULL)
        test = self.engagement().test_set.get()
        outsider = Dojo_User.objects.create(username="company-outsider")
        self.client.force_login(outsider)

        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "You cannot upload to any product yet")
        self.assertNotContains(page, f'<option value="{self.product.id}"')
        # PermissionDenied renders upstream's handler403 (403.html; upstream answers it with status 400)
        for response in (
            self.client.get(self.url, {"product": self.product.id}),
            self.client.get(self.url, {"test": test.id}),
            self.upload(FULL, component="forged"),
            self.upload(FULL, url=f"{self.url}?test={test.id}"),
        ):
            self.assertTemplateUsed(response, "403.html")
            self.assertNotIn(response.status_code, (200, 302))
        self.assertEqual(Test.objects.filter(engagement__product=self.product).count(), 1)
        self.assertEqual(Finding.objects.filter(test=test).count(), 4)

    def test_creating_the_upload_engagement_needs_add_permission(self):
        with patch("dojo.authorization.authorization.user_has_permission",
                   side_effect=lambda _user, _obj, permission: permission != "add"):
            response = self.upload(FULL)
            self.assertTemplateUsed(response, "403.html")
            self.assertIsNone(self.engagement())
        self.upload(FULL)
        with patch("dojo.authorization.authorization.user_has_permission",
                   side_effect=lambda _user, _obj, permission: permission != "add"):
            self.upload(CHANGED)  # the engagement exists: import permission is enough
        self.assertEqual(self.closed(self.engagement().test_set.get()), 1)

    def test_bad_file_keeps_fields_and_leaves_no_empty_test_or_engagement(self):
        broken = get_unit_tests_scans_path("zap") / "empty_2.9.0.xml"
        garbage = SimpleUploadedFile("report.xml", b"this is not a ZAP report")
        response = self.client.post(self.url, {"product": self.product.id, "scan_type": ZAP, "component": "api",
                                               "close_old_findings": "on", "file": garbage})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The report could not be imported")
        self.assertContains(response, 'value="api"')
        self.assertContains(response, f'<option value="{self.product.id}" selected>')
        self.assertNotContains(response, "Get Pro")
        # the empty test goes; the engagement stays (its "Engagement created" notification already went out)
        self.assertEqual(Engagement.objects.filter(product=self.product).count(), 1)
        self.assertFalse(Test.objects.filter(engagement__product=self.product).exists())

        self.upload(FULL)
        response = self.client.post(self.url, {"product": self.product.id, "scan_type": ZAP, "component": "api",
                                               "file": SimpleUploadedFile("report.txt", b"<broken")})
        self.assertContains(response, "Details: Internal error: Wrong file format, please use xml.")  # no list repr
        self.assertEqual(self.engagement().test_set.count(), 1)

        response = self.upload(broken, name="report.exe")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "File extension")
        self.assertEqual(self.engagement().test_set.count(), 1)

    def test_get_defaults(self):
        page = self.client.get(self.url)
        self.assertContains(page, f'<option value="{self.product.id}" selected>')  # the member's only product
        self.assertNotContains(page, "Used before")
        self.upload(FULL)

        page = self.client.get(self.url, {"product": self.product.id})
        self.assertContains(page, "Used before")
        self.assertContains(page, f'<option value="{ZAP}" selected>')
        self.assertContains(page, f'href="{reverse("import_scan_results_prod", args=(self.product.id,))}"')
        self.assertContains(page, 'class="company-dropzone"')
        self.assertNotContains(page, "company-component-pick")  # one untitled test: the component stays optional

    def test_sidebar_link(self):
        sidebar = self.client.get(reverse("dashboard")).content.decode()
        self.assertLess(sidebar.index('data-testid="company-nav-my-work"'), sidebar.index('data-testid="company-nav-upload"'))
        self.assertIn(f'href="{self.url}" data-testid="company-nav-upload"', sidebar)

    def test_tab_bar_links_only_with_import_permission(self):
        self.upload(FULL)
        test = self.engagement().test_set.get()
        product_link = f'href="{self.url}?product={self.product.id}"'
        pages = {
            reverse("view_product", args=(self.product.id,)): product_link,
            reverse("product_open_findings", args=(self.product.id,)): product_link,
            reverse("view_engagement", args=(test.engagement_id,)): product_link,
            reverse("view_test", args=(test.id,)): f'href="{self.url}?test={test.id}"',
        }
        for page, link in pages.items():
            with self.subTest(page=page):
                tabs = TABS.search(self.client.get(page).content.decode()).group(1)
                self.assertIn(f'<a class="btn btn-success btn-sm" {link}', tabs)
                self.assertNotIn("btn-primary", tabs)
                quick = tabs[tabs.index("company-quick-actions"):]
                self.assertNotIn("<button", quick)
                with patch("dojo.authorization.template_filters.user_has_permission",
                           side_effect=lambda _user, _obj, permission: permission != "import"):
                    tabs = TABS.search(self.client.get(page).content.decode()).group(1)
                self.assertNotIn("company-quick-actions", tabs)
        # not on the upload page itself, nor on upstream's import, re-import and edit pages
        for page in (self.url, reverse("import_scan_results_prod", args=(self.product.id,)),
                     reverse("re_import_scan_results", args=(test.id,)), reverse("edit_product", args=(self.product.id,))):
            with self.subTest(page=page):
                self.assertNotIn("company-quick-actions", TABS.search(self.client.get(page).content.decode()).group(1))


@versioned_fixtures
class TestScanTypeAllowlist(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def run_command(self):
        out = StringIO()
        call_command("company_scan_types", stdout=out, stderr=StringIO())
        return out.getvalue()

    def active(self, name):
        # a parser without a Test_Type row counts as active (get_parser creates it on first use)
        return Test_Type.objects.filter(name=name).values_list("active", flat=True).first() is not False

    def test_allowlist(self):
        engagement = Engagement.objects.order_by("id").first()
        now = timezone.now()
        Test.objects.create(engagement=engagement, test_type=Test_Type.objects.get_or_create(name="Bandit Scan")[0],
                            scan_type="Bandit Scan", target_start=now, target_end=now)
        manual = Test_Type.objects.get_or_create(name="Pen Test")[0]
        self.assertNotIn("Pen Test", PARSERS)
        unused = "Trivy Scan"
        self.assertFalse(Test.objects.filter(test_type__name=unused).exists())

        with override_settings(COMPANY_SCAN_TYPES=[]):
            self.assertIn("nothing changed", self.run_command())
        self.assertTrue(self.active(unused))

        with override_settings(COMPANY_SCAN_TYPES=[ZAP, " Semgrep JSON Report"]):
            self.assertIn(f"deactivated: {unused}", self.run_command())
            self.assertTrue(self.active(ZAP))
            self.assertTrue(self.active("Semgrep JSON Report"))
            self.assertFalse(self.active(unused))
            self.assertTrue(self.active("Bandit Scan"))  # has tests: stays filterable
            manual.refresh_from_db()
            self.assertTrue(manual.active)
            self.assertIn("0 scan types changed", self.run_command())

            member = Dojo_User.objects.create(username="company-allowlist")
            engagement.product.authorized_users.add(member)
            self.client.force_login(member)
            page = self.client.get(reverse("company_quick_upload"))
            self.assertContains(page, f'<option value="{ZAP}">')
            self.assertNotContains(page, f'<option value="{unused}">')

        Test_Type.objects.filter(name=ZAP).update(active=False)
        with override_settings(COMPANY_SCAN_TYPES=[ZAP]):
            self.assertRegex(self.run_command(), rf"(?m)^activated: {ZAP}$")
        self.assertTrue(self.active(ZAP))
