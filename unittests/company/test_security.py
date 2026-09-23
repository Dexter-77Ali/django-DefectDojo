"""company: security checks for the company layer (input handling, escaping, configuration hardening)."""

from django.core import mail
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import override_settings
from django.urls import reverse

from dojo.company import fields
from dojo.company.apps import validate_company_configuration
from dojo.models import Product
from dojo.notifications.helper import create_notification
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

ESCALATION = ["soc@example.com"]


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False, COMPANY_NAME='<script>alert("x")</script>Acme')
class TestTemplateEscaping(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.client.force_login(self.get_test_admin())

    def test_company_name_is_escaped_in_layout(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, "&lt;script&gt;")

    def test_company_name_is_escaped_in_reports(self):
        product = Product.objects.order_by("id").first()
        response = self.client.get(reverse("product_report", args=(product.id,)), {"_generate": "1", "report_type": "HTML"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<script>alert("x")</script>')


class TestConfigurationHardening(DojoTestCase):

    @override_settings(COMPANY_PALETTE={"800": "#000;}</style><script>x</script>"})
    def test_palette_rejects_css_injection(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_company_configuration()

    @override_settings(COMPANY_PALETTE={"800": "#10413e"})
    def test_palette_accepts_plain_hex(self):
        validate_company_configuration()

    def test_field_values_are_bounded(self):
        with self.assertRaises(ValidationError):
            fields.registry()["company:owner-team"].validate("x" * (fields.MAX_VALUE_LENGTH + 1))
        with self.assertRaises(ValidationError):
            fields.CompanyField("company:tier", "Tier", ("gold",)).validate("critical'; DROP TABLE dojo_product; --")


@versioned_fixtures
@override_settings(
    COMPANY_NAME="Acme",
    COMPANY_ESCALATION_EMAILS=ESCALATION,
    COMPANY_ESCALATION_EVENTS=["sla_breach"],
    OS_MESSAGE_ENABLED=False,
)
class TestEscalationMailSafety(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_header_injection_in_title_does_not_add_recipients(self):
        # Django refuses newlines in headers; the manager must log and continue, never send a forged header.
        create_notification(event="sla_breach", title="Breach\r\nBcc: attacker@example.com", description="d", url="/")
        escalations = [m for m in mail.outbox if m.to == ESCALATION]
        self.assertEqual(escalations, [])
        self.assertFalse(any("attacker@example.com" in (m.bcc + m.cc + m.to) for m in mail.outbox))

    def test_clean_title_still_escalates(self):
        create_notification(event="sla_breach", title="Breach", description="d", url="/")
        self.assertEqual(len([m for m in mail.outbox if m.to == ESCALATION]), 1)


class TestProductionSettingsShape(DojoTestCase):

    def test_ldap_backend_is_first_and_local_last_when_enabled(self):
        # configuration builder puts the directory first and keeps the break-glass local backend
        from dojo.company import ldap as company_ldap  # noqa: PLC0415 -- lazy, module has no model imports

        target = {"AUTHENTICATION_BACKENDS": ("django.contrib.auth.backends.ModelBackend",)}
        try:
            company_ldap.configure(target, _Env({
                "DD_COMPANY_LDAP_SERVER_URI": "ldaps://dc.example.com:636",
                "DD_COMPANY_LDAP_USER_BASE": "ou=users,dc=example,dc=com",
                "DD_COMPANY_LDAP_GROUP_BASE": "ou=groups,dc=example,dc=com",
            }))
        except ModuleNotFoundError:
            self.skipTest("django-auth-ldap only in the company image")
        self.assertEqual(target["AUTHENTICATION_BACKENDS"][0], company_ldap.BACKEND)
        self.assertEqual(target["AUTHENTICATION_BACKENDS"][-1], "django.contrib.auth.backends.ModelBackend")
        self.assertTrue(target["AUTH_LDAP_SERVER_URI"].startswith("ldaps://"))


class _Env:

    def __init__(self, values):
        self.values = values

    def __call__(self, name, default=None, **_):
        if name in self.values:
            return self.values[name]
        if default is None:
            raise KeyError(name)
        return default

    def bool(self, name, *, default=False):
        return self.values.get(name, default)
