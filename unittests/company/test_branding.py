"""
company: branding customization (Phase 4a).

Covers the mechanism, not the artwork: the company app is installed through
dojo/settings/local_settings.py, its template directory is searched first,
the company base.html overrides the sidebar logo, the support tab, the
stylesheet include and the footer, and the context-processor wrapper hides
the Pro upsell link and the "Get Pro" banners. Runs inside the uwsgi container like every other test:
    bash ./run-unittest.sh -t unittests.company.test_branding
"""

from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import RequestFactory, override_settings
from django.urls import reverse

from dojo.company.context_processors import globalize_vars
from dojo.product_announcements import ErrorPageProductAnnouncement, LongRunningRequestProductAnnouncement
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


class TestCompanySettings(DojoTestCase):

    def test_company_app_installed(self):
        self.assertIn("dojo.company", settings.INSTALLED_APPS)
        self.assertEqual(apps.get_app_config("company").name, "dojo.company")

    def test_company_templates_searched_first(self):
        first = str(settings.TEMPLATES[0]["DIRS"][0]).replace("\\", "/")
        self.assertTrue(first.endswith("dojo/company/templates"), first)

    def test_context_processor_replaced(self):
        processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertIn("dojo.company.context_processors.globalize_vars", processors)
        self.assertNotIn("dojo.context_processors.globalize_vars", processors)

    def test_company_static_files_are_findable(self):
        for path in ("dojo/company/company.css", "dojo/company/icon.png", "dojo/company/logo.png"):
            self.assertIsNotNone(finders.find(path), path)


class TestCompanyContextProcessor(DojoTestCase):

    @override_settings(OS_MESSAGE_ENABLED=False, COMPANY_NAME="Acme")
    def test_hides_upsell_and_exposes_company_name(self):
        context = globalize_vars(RequestFactory().get("/"))
        self.assertFalse(context["SHOW_PLG_LINK"])
        self.assertEqual(context["COMPANY_NAME"], "Acme")
        # upstream values still flow through the wrapper
        self.assertIn("CLASSIC_AUTH_ENABLED", context)

    @override_settings(OS_MESSAGE_ENABLED=False)
    def test_drops_pro_upsell_banners_and_keeps_the_others(self):
        request = RequestFactory().get("/")
        request.session = {}
        ErrorPageProductAnnouncement(request=request)  # what upstream queues after an error page
        request.session["_product_banners"].append({"source": "company-test", "message": "Maintenance tonight"})
        context = globalize_vars(request)
        self.assertEqual([b["message"] for b in context["additional_banners"]], ["Maintenance tonight"])
        self.assertNotIn("_product_banners", request.session)  # consumed, not left for the next page

    @override_settings(OS_MESSAGE_ENABLED=False)
    def test_no_banner_area_when_only_upsell_was_queued(self):
        request = RequestFactory().get("/")
        request.session = {}
        LongRunningRequestProductAnnouncement(request=request, duration=60)
        self.assertEqual(len(request.session["_product_banners"]), 1)
        self.assertNotIn("additional_banners", globalize_vars(request))


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestCompanyLayout(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.client.force_login(self.get_test_admin())

    def test_dashboard_uses_company_layout(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        # sidebar logo and stylesheet come from the company static directory
        self.assertContains(response, "dojo/company/icon.png")
        self.assertContains(response, "dojo/company/company.css")
        self.assertContains(response, settings.COMPANY_NAME)
        # footer keeps the license notices the BSD license requires
        self.assertContains(response, "3-Clause BSD License")
        self.assertContains(response, "Dependencies Notice")
        # upstream upsell surfaces are gone
        self.assertNotContains(response, "Try Pro for Free")
        self.assertNotContains(response, f'href="{reverse("support")}"')

    def test_login_page_still_renders(self):
        self.client.logout()
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        # the login card is upstream's; a profile's login-logo.png replaces dojo/img/logo.svg (docker/company/apply-profile.sh)
        self.assertContains(response, "dojo/img/logo.svg")
