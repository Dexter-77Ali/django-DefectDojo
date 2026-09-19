"""company: SLA policy hooks (Phase 4c-2)."""

from django.conf import settings

from dojo.company import sla
from dojo.company.fields import set_product_field
from dojo.models import Finding
from dojo.utils import get_custom_method
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


@versioned_fixtures
class TestCompanySlaPolicy(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.finding = Finding.objects.order_by("id").first()
        self.product = self.finding.test.engagement.product
        self.severity = self.finding.severity.lower()
        # Pin the SLA configuration in memory (no save: saving triggers the bulk recalculation task).
        self._configure(days=10, enforce=True)

    def _configure(self, *, days=None, enforce=None):
        config = self.product.sla_configuration
        if days is not None:
            setattr(config, self.severity, days)
        if enforce is not None:
            setattr(config, f"enforce_{self.severity}", enforce)

    def _period(self):
        # drop the per-instance cache written by the previous call
        if hasattr(self.product, "_company_criticality"):
            delattr(self.product, "_company_criticality")
        return self.finding.get_sla_period()  # routes through the hook when configured

    def test_hooks_are_configured_and_importable(self):
        self.assertEqual(settings.FINDING_SLA_PERIOD_METHOD, "dojo.company.sla.finding_sla_period")
        self.assertIs(get_custom_method("FINDING_SLA_PERIOD_METHOD"), sla.finding_sla_period)
        self.assertIs(get_custom_method("FINDING_SLA_EXPIRATION_CALCULATION_METHOD"), sla.update_sla_expiration_dates)

    def test_no_criticality_keeps_upstream_period(self):
        self.assertEqual(self._period(), (10, True))

    def test_criticality_scales_the_period(self):
        expected = {"critical": 5, "high": 8, "medium": 10, "low": 15}
        for criticality, days in expected.items():
            set_product_field(self.product, "company:criticality", criticality)
            self.assertEqual(self._period(), (days, True), criticality)

    def test_unenforced_severity_is_untouched(self):
        self._configure(enforce=False)
        set_product_field(self.product, "company:criticality", "critical")
        self.assertEqual(self._period(), (10, False))

    def test_never_below_one_day(self):
        self._configure(days=1)
        set_product_field(self.product, "company:criticality", "critical")
        self.assertEqual(self._period(), (1, True))
