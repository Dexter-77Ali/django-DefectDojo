"""
company: fork settings.

Loaded by dojo/settings/settings.py right after settings.dist.py in the
same namespace (django-split-settings), so every name defined there
(env, root, INSTALLED_APPS, TEMPLATES, ...) is available and mutated in
place. Never re-declare an upstream structure here; mutate it so upstream
additions keep flowing in on every release merge. The file is exec'd, not
imported: use absolute imports only.
"""

# --- company app --------------------------------------------------------------
INSTALLED_APPS += ("dojo.company",)  # noqa: F821 -- defined by settings.dist.py

# Company templates are searched before dojo/templates, so a company
# base.html can extend upstream's ("recursive extends") and override blocks.
_DOJO_TEMPLATE_DIRS.insert(0, root("dojo/company/templates"))  # noqa: F821

# Swap the upstream context processor for the company wrapper (same values,
# plus COMPANY_NAME, minus the Pro upsell link).
_context_processors = TEMPLATES[0]["OPTIONS"]["context_processors"]  # noqa: F821
_context_processors[_context_processors.index("dojo.context_processors.globalize_vars")] = (
    "dojo.company.context_processors.globalize_vars"
)

# --- company values (DD_* environment variables; defaults are placeholders) ---
# One build serves several companies: everything below is configuration.
COMPANY_NAME = env("DD_COMPANY_NAME", default="Company")  # noqa: F821
# {"50": "#e6f4f3", ..., "900": "#0a2b29"}: overrides the --color-dd-primary-N tokens at runtime (any subset).
COMPANY_PALETTE = env.json("DD_COMPANY_PALETTE", default={})  # noqa: F821
# {"critical": 0.5, "high": 0.75, "medium": 1.0, "low": 1.5}: SLA day multipliers per company:criticality.
COMPANY_SLA_FACTORS = env.json("DD_COMPANY_SLA_FACTORS", default=None)  # noqa: F821
# {"owner-team": {"label": "Owner team"}, "criticality": {"label": "...", "choices": [...]}}: replaces the default fields.
COMPANY_FIELDS = env.json("DD_COMPANY_FIELDS", default=None)  # noqa: F821

# --- upstream hooks (dotted paths read through dojo.utils.get_custom_method) -----
# Asserted importable by dojo.company.apps.CompanyConfig.ready(); a typo here
# would otherwise silently fall back to upstream behaviour.
FINDING_SLA_PERIOD_METHOD = "dojo.company.sla.finding_sla_period"
FINDING_SLA_EXPIRATION_CALCULATION_METHOD = "dojo.company.sla.update_sla_expiration_dates"

# --- LDAP / Active Directory login (see dojo/company/ldap.py) ---------------------
# Off by default; the company image (Dockerfile.company) carries django-auth-ldap.
COMPANY_LDAP_ENABLED = env.bool("DD_COMPANY_LDAP_ENABLED", default=False)  # noqa: F821
if COMPANY_LDAP_ENABLED:
    from dojo.company.ldap import configure as _configure_company_ldap

    _configure_company_ldap(globals(), env)  # noqa: F821

# --- notifications (see dojo/company/notifications.py) ----------------------------
NOTIFICATION_MANAGER = "dojo.company.notifications.CompanyNotificationManager"
COMPANY_ESCALATION_EMAILS = env.list("DD_COMPANY_ESCALATION_EMAILS", default=[])  # noqa: F821
COMPANY_ESCALATION_EVENTS = env.list(  # noqa: F821
    "DD_COMPANY_ESCALATION_EVENTS",
    default=["sla_breach", "sla_breach_combined", "risk_acceptance_expiration"],
)
