"""company: SLA policy hooks (Phase 4c-2)."""

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from dojo.company import sla
from dojo.models import Finding, Product
from dojo.utils import get_custom_method
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


@versioned_fixtures
@override_settings(COMPANY_SLA_FACTORS=None)  # test the code defaults, whatever profile the container carries
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

    def _period(self, criticality):
        self.product.business_criticality = criticality  # in memory: the hook reads the loaded product row
        return self.finding.get_sla_period()  # routes through the hook when configured

    def test_hooks_are_configured_and_importable(self):
        self.assertEqual(settings.FINDING_SLA_PERIOD_METHOD, "dojo.company.sla.finding_sla_period")
        self.assertIs(get_custom_method("FINDING_SLA_PERIOD_METHOD"), sla.finding_sla_period)
        self.assertIs(get_custom_method("FINDING_SLA_EXPIRATION_CALCULATION_METHOD"), sla.update_sla_expiration_dates)

    def test_no_criticality_keeps_upstream_period(self):
        self.assertEqual(self._period(None), (10, True))
        self.assertEqual(self._period(Product.NONE_CRITICALITY), (10, True))

    def test_criticality_scales_the_period(self):
        expected = {
            Product.VERY_HIGH_CRITICALITY: 5,
            Product.HIGH_CRITICALITY: 8,
            Product.MEDIUM_CRITICALITY: 10,
            Product.LOW_CRITICALITY: 15,
            Product.VERY_LOW_CRITICALITY: 20,
        }
        for criticality, days in expected.items():
            self.assertEqual(self._period(criticality), (days, True), criticality)

    def test_unenforced_severity_is_untouched(self):
        self._configure(enforce=False)
        self.assertEqual(self._period(Product.VERY_HIGH_CRITICALITY), (10, False))

    def test_never_below_one_day(self):
        self._configure(days=1)
        self.assertEqual(self._period(Product.VERY_HIGH_CRITICALITY), (1, True))

    def test_hook_adds_no_query(self):
        # upstream pins the query count around Finding.save() (unittests/test_tag_inheritance_perf.py);
        # the hook may only read what the default rule already loads
        def queries(func):
            finding = Finding.objects.get(pk=self.finding.pk)  # fresh instance, nothing cached
            with CaptureQueriesContext(connection) as ctx:
                days, enforce = func(finding)
            self.assertTrue(days is not None and enforce, "fixture must exercise the scaling path")
            return len(ctx.captured_queries)

        self.assertEqual(queries(sla.finding_sla_period), queries(sla._upstream_period))
