"""company: per-company configuration through DD_COMPANY_* settings (multi-company build)."""

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import override_settings
from django.urls import reverse

from dojo.company import fields, sla
from dojo.company.apps import validate_company_configuration
from dojo.models import Product
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


@override_settings(COMPANY_FIELDS=None, COMPANY_SLA_FACTORS=None)  # the container may carry a company profile
class TestConfiguredFieldsAndFactors(DojoTestCase):

    def test_default_fields(self):
        keys = set(fields.registry())
        self.assertEqual(keys, {"company:owner-team", "company:business-unit"})

    @override_settings(COMPANY_FIELDS={"cost-centre": {"label": "Cost centre"}, "company:tier": {"label": "Tier", "choices": ["Gold", "silver"]}})
    def test_fields_from_settings_replace_defaults(self):
        registry = fields.registry()
        self.assertEqual(set(registry), {"company:cost-centre", "company:tier"})
        self.assertEqual(registry["company:tier"].choices, ("gold", "silver"))
        self.assertEqual(registry["company:tier"].validate("GOLD"), "gold")
        with self.assertRaises(ValidationError):
            fields._field("company:owner-team")

    @override_settings(COMPANY_SLA_FACTORS={"very high": 0.25, "High ": 2})
    def test_factors_from_settings(self):
        self.assertEqual(sla.factors(), {"very high": 0.25, "high": 2.0})

    def test_default_factors(self):
        self.assertEqual(sla.factors(), sla.DEFAULT_FACTORS)


class TestStartupValidation(DojoTestCase):

    @override_settings(COMPANY_PALETTE={"800": "#10413e"}, COMPANY_SLA_FACTORS={"critical": 0.5}, COMPANY_FIELDS={"x": {"label": "X"}})
    def test_valid_configuration_passes(self):
        validate_company_configuration()

    @override_settings(COMPANY_PALETTE={"850": "#10413e"})
    def test_bad_palette_shade(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_company_configuration()

    @override_settings(COMPANY_PALETTE={"800": "teal"})
    def test_bad_palette_colour(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_company_configuration()

    @override_settings(COMPANY_SLA_FACTORS={"critical": 0})
    def test_bad_factor(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_company_configuration()

    @override_settings(COMPANY_FIELDS={})
    def test_empty_fields(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_company_configuration()


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False, COMPANY_PALETTE={"800": "#112233", "900": "#0a0b0c"})
class TestRuntimePalette(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_palette_style_block_is_emitted(self):
        self.client.force_login(self.get_test_admin())
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'id="company-palette"')
        self.assertContains(response, "--color-dd-primary-800:#112233;")
        self.assertContains(response, "--color-dd-primary-900:#0a0b0c;")

    @override_settings(COMPANY_PALETTE={})
    def test_no_style_block_without_palette(self):
        self.client.force_login(self.get_test_admin())
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'id="company-palette"')
        Product.objects.exists()  # keep the fixture import meaningful
