"""company: product fields on DojoMeta (Phase 4c-1)."""

from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from dojo.company import fields
from dojo.models import DojoMeta, Product
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

# the code defaults plus one field with choices, whatever profile the container carries
FIELDS = {**fields.DEFAULT_FIELDS, "tier": {"label": "Tier", "choices": ["gold", "silver", "bronze"]}}


@versioned_fixtures
@override_settings(COMPANY_FIELDS=FIELDS)
class TestCompanyFields(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.product = Product.objects.order_by("id").first()

    def test_set_get_roundtrip_and_upsert(self):
        fields.set_product_field(self.product, "company:owner-team", "  AppSec  ")
        self.assertEqual(fields.get_product_field(self.product, "company:owner-team"), "AppSec")
        fields.set_product_field(self.product, "company:owner-team", "Platform")
        self.assertEqual(DojoMeta.objects.filter(product=self.product, name="company:owner-team").count(), 1)
        self.assertEqual(fields.get_product_fields(self.product), {"company:owner-team": "Platform"})

    def test_choices_are_normalised_and_enforced(self):
        row = fields.set_product_field(self.product, "company:tier", "GOLD")
        self.assertEqual(row.value, "gold")
        with self.assertRaises(ValidationError):
            fields.set_product_field(self.product, "company:tier", "urgent")

    def test_unknown_key_and_empty_value_are_rejected(self):
        with self.assertRaises(ValidationError):
            fields.set_product_field(self.product, "company:nope", "x")
        with self.assertRaises(ValidationError):
            fields.set_product_field(self.product, "company:owner-team", "   ")
        self.assertIsNone(fields.get_product_field(self.product, "company:owner-team"))
        self.assertEqual(fields.get_product_field(self.product, "company:owner-team", "unset"), "unset")

    def test_products_with_field_and_clear(self):
        fields.set_product_field(self.product, "company:business-unit", "Payments")
        self.assertIn(self.product, fields.products_with_field("company:business-unit"))
        self.assertIn(self.product, fields.products_with_field("company:business-unit", "payments"))
        self.assertNotIn(self.product, fields.products_with_field("company:business-unit", "Retail"))
        self.assertEqual(fields.clear_product_field(self.product, "company:business-unit"), 1)
        self.assertEqual(fields.clear_product_field(self.product, "company:business-unit"), 0)

    def test_check_all_reports_drifted_rows(self):
        # rows written through the API or UI bypass validation; check_all must catch them
        DojoMeta.objects.create(product=self.product, name="company:tier", value="urgent")
        problems = fields.check_all()
        self.assertEqual([(p, k, v) for p, k, v, _ in problems], [(self.product.id, "company:tier", "urgent")])

    def test_management_command(self):
        out = StringIO()
        call_command("company_fields", "list", stdout=out)
        self.assertIn("company:tier: Tier (gold, silver, bronze)", out.getvalue())

        out = StringIO()
        call_command("company_fields", "set", str(self.product.id), "company:tier", "Silver", stdout=out)
        self.assertEqual(fields.get_product_field(self.product, "company:tier"), "silver")

        out = StringIO()
        call_command("company_fields", "get", str(self.product.id), stdout=out)
        self.assertIn("company:tier=silver", out.getvalue())

        with self.assertRaises(CommandError):
            call_command("company_fields", "set", str(self.product.id), "company:tier", "urgent")
        with self.assertRaises(CommandError):
            call_command("company_fields", "get", "999999")

        out = StringIO()
        call_command("company_fields", "check", stdout=out)
        self.assertIn("0 invalid row(s)", out.getvalue())
