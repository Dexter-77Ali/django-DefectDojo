"""
company: SLA policy through the upstream hooks.

Wired from dojo/settings/local_settings.py:
    FINDING_SLA_PERIOD_METHOD = "dojo.company.sla.finding_sla_period"
    FINDING_SLA_EXPIRATION_CALCULATION_METHOD = "dojo.company.sla.update_sla_expiration_dates"

Policy: the product's SLA_Configuration days for the finding's severity,
scaled by the product's upstream *Business criticality* (the
``business_criticality`` column on Product, set on the product edit form:
very high, high, medium, low, very low, none). The factors are configuration
so one build serves several companies: ``COMPANY_SLA_FACTORS`` (from
``DD_COMPANY_SLA_FACTORS`` JSON), placeholder default very high 0.5, high 0.75,
medium 1.0, low 1.5, very low 2.0. Never below one day. A missing or unknown
criticality keeps the upstream period unchanged.

The column travels with the product row that the default rule already loads,
so the hook adds no query to Finding.save(): upstream pins query counts around
it (unittests/test_tag_inheritance_perf.py) and those baselines must keep
passing on the fork.
"""

from django.conf import settings

from dojo.sla_config.helpers import update_sla_expiration_dates_sla_config_sync

DEFAULT_FACTORS = {"very high": 0.5, "high": 0.75, "medium": 1.0, "low": 1.5, "very low": 2.0}


def factors() -> dict[str, float]:
    """Criticality to multiplier, from settings with the placeholder defaults as fallback."""
    configured = getattr(settings, "COMPANY_SLA_FACTORS", None) or DEFAULT_FACTORS
    return {str(k).strip().lower(): float(v) for k, v in configured.items()}


def _upstream_period(finding):
    """The default upstream rule (Finding.get_sla_period without the hook)."""
    sla_configuration = finding.get_sla_configuration()
    severity = finding.severity.lower()
    return (
        getattr(sla_configuration, severity, None),
        getattr(sla_configuration, f"enforce_{severity}", None),
    )


def finding_sla_period(finding):
    """Return (days, enforce) for the finding, scaled by the product's business criticality."""
    days, enforce = _upstream_period(finding)
    if days is None or not enforce:
        return days, enforce
    criticality = (finding.test.engagement.product.business_criticality or "").strip().lower()
    factor = factors().get(criticality, 1.0)
    return max(1, round(days * factor)), enforce


def update_sla_expiration_dates(sla_config, products, severities=None):
    """Bulk recalculation after an SLA configuration edit; upstream's loop already calls the period hook per finding."""
    return update_sla_expiration_dates_sla_config_sync(sla_config, products, severities=severities)
