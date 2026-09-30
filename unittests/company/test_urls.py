"""company: root URLconf (dojo/company/urls.py, ROOT_URLCONF = "dojo.company.urls")."""

from django.conf import settings
from django.test import override_settings
from django.urls import resolve, reverse

import dojo.urls
from dojo.company import urls as company_urls
from dojo.models import Dojo_User, Product
from dojo.utils import get_system_setting
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


class TestCompanyUrlconf(DojoTestCase):

    def test_root_urlconf_is_the_company_one(self):
        self.assertEqual(settings.ROOT_URLCONF, "dojo.company.urls")

    def test_error_handlers_are_upstreams(self):
        for name in ("handler400", "handler403", "handler500"):
            self.assertEqual(getattr(company_urls, name), getattr(dojo.urls, name), name)

    def test_company_routes_first_then_every_upstream_route(self):
        self.assertIsInstance(company_urls.company_patterns, list)
        self.assertEqual(company_urls.urlpatterns[0].pattern.regex.pattern, f"^{get_system_setting('url_prefix')}company/")
        # compared by pattern text: unittests.api_v3 reloads dojo.urls, which rebuilds its pattern objects
        self.assertEqual(
            [str(p.pattern) for p in company_urls.urlpatterns[1:]],
            [str(p.pattern) for p in dojo.urls.urlpatterns],
        )

    def test_upstream_names_still_reverse_and_resolve(self):
        for name, args in (
            ("home", ()),
            ("dashboard", ()),
            ("login", ()),
            ("open_findings", ()),
            ("view_product", (1,)),
            ("view_finding", (1,)),
            ("api-root", ()),
        ):
            with self.subTest(name=name):
                self.assertEqual(resolve(reverse(name, args=args)).url_name, name)


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestCompanyErrorPages(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_permission_denied_renders_through_upstream_view_without_upsell(self):
        self.client.force_login(Dojo_User.objects.create(username="company-outsider"))
        product = Product.objects.order_by("id").first()
        response = self.client.get(reverse("view_product", args=(product.id,)))
        # dojo.views.custom_unauthorized_view renders 403.html (with status 400, an upstream quirk)
        self.assertTemplateUsed(response, "403.html")
        self.assertIn(response.status_code, {400, 403})
        # that view queues a "Pro comes with support" banner; the company context processor drops it
        self.assertNotContains(response, "Pro comes with support", status_code=response.status_code)
        self.assertNotContains(response, "cloud.defectdojo.com", status_code=response.status_code)
