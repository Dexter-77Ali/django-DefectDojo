"""
company: SLA policy through the upstream hooks.

Wired from dojo/settings/local_settings.py:
    FINDING_SLA_PERIOD_METHOD = "dojo.company.sla.finding_sla_period"
    FINDING_SLA_EXPIRATION_CALCULATION_METHOD = "dojo.company.sla.update_sla_expiration_dates"

Policy: the product's SLA_Configuration days for the finding's severity,
scaled by the product's business criticality (company field
``company:criticality``). The factors are configuration so one build serves
several companies: ``COMPANY_SLA_FACTORS`` (from ``DD_COMPANY_SLA_FACTORS``
JSON), placeholder default critical 0.5, high 0.75, medium 1.0, low 1.5.
Never below one day. A missing or unknown criticality keeps the upstream
period unchanged.
"""

from django.conf import settings

from dojo.company.fields import PREFIX, get_product_field
from dojo.sla_config.helpers import update_sla_expiration_dates_sla_config_sync

CRITICALITY_KEY = f"{PREFIX}criticality"
DEFAULT_FACTORS = {"critical": 0.5, "high": 0.75, "medium": 1.0, "low": 1.5}
_CACHE_ATTR = "_company_criticality"


def factors() -> dict[str, float]:
    """Criticality to multiplier, from settings with the placeholder defaults as fallback."""
    configured = getattr(settings, "COMPANY_SLA_FACTORS", None) or DEFAULT_FACTORS
    return {str(k).lower(): float(v) for k, v in configured.items()}


def _upstream_period(finding):
    """The default upstream rule (Finding.get_sla_period without the hook)."""
    sla_configuration = finding.get_sla_configuration()
    severity = finding.severity.lower()
    return (
        getattr(sla_configuration, severity, None),
        getattr(sla_configuration, f"enforce_{severity}", None),
    )


def product_criticality(product) -> str | None:
    """Criticality of a product, cached on the instance for the life of the object."""
    # ponytail: one query per product instance; importers reuse the instance across
    # the findings of a test, bulk paths elsewhere would need a real per-request cache.
    if not hasattr(product, _CACHE_ATTR):
        setattr(product, _CACHE_ATTR, get_product_field(product, CRITICALITY_KEY))
    return getattr(product, _CACHE_ATTR)


def finding_sla_period(finding):
    """Return (days, enforce) for the finding, scaled by product criticality."""
    days, enforce = _upstream_period(finding)
    if days is None or not enforce:
        return days, enforce
    factor = factors().get(product_criticality(finding.test.engagement.product) or "", 1.0)
    return max(1, round(days * factor)), enforce


def update_sla_expiration_dates(sla_config, products, severities=None):
    """Bulk recalculation after an SLA configuration edit; upstream's loop already calls the period hook per finding."""
    return update_sla_expiration_dates_sla_config_sync(sla_config, products, severities=severities)
