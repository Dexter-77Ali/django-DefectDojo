from django.apps import AppConfig


class CompanyConfig(AppConfig):

    """Company customizations: templates, static files, context processor (see CLAUDE.md)."""

    name = "dojo.company"
    label = "company"
    verbose_name = "Company customizations"
