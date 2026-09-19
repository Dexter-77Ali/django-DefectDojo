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
COMPANY_NAME = env("DD_COMPANY_NAME", default="Company")  # noqa: F821
