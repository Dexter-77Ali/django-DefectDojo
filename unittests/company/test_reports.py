"""company: branded HTML reports (Phase 4c-4)."""

from django.test import override_settings
from django.urls import reverse

from dojo.models import Product
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


@versioned_fixtures
@override_settings(COMPANY_NAME="Acme", OS_MESSAGE_ENABLED=False)
class TestCompanyReports(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.client.force_login(self.get_test_admin())
        self.product = Product.objects.order_by("id").first()

    def test_product_report_carries_company_header(self):
        url = reverse("product_report", args=(self.product.id,))
        response = self.client.get(url, {"_generate": "1", "report_type": "HTML"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="company-report-header"')
        self.assertContains(response, "dojo/company/logo.png")
        self.assertContains(response, "dojo/company/company.css")
        self.assertContains(response, "Acme")
        # upstream report body still renders underneath the company header
        self.assertContains(response, self.product.name)

    def test_report_options_page_is_untouched(self):
        response = self.client.get(reverse("product_report", args=(self.product.id,)))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="company-report-header"')
