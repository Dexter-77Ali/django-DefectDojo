"""company: notification routing (Phase 4c-3)."""

from django.core import mail
from django.test import override_settings

from dojo.company.notifications import CompanyNotificationManager
from dojo.notifications.helper import create_notification, get_manager_class_instance
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

ESCALATION = ["security@example.com"]


@versioned_fixtures
@override_settings(
    COMPANY_NAME="Acme",
    COMPANY_ESCALATION_EMAILS=ESCALATION,
    COMPANY_ESCALATION_EVENTS=["sla_breach"],
    OS_MESSAGE_ENABLED=False,
)
class TestCompanyNotifications(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    @staticmethod
    def _escalations():
        return [m for m in mail.outbox if m.to == ESCALATION]

    def test_manager_is_the_company_subclass(self):
        self.assertIsInstance(get_manager_class_instance(), CompanyNotificationManager)

    def test_escalated_event_sends_exactly_one_copy(self):
        create_notification(event="sla_breach", title="Finding X breached its SLA", description="details here", url="/")
        escalations = self._escalations()
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0].subject, "[Acme] sla_breach: Finding X breached its SLA")
        self.assertIn("details here", escalations[0].body)

    def test_other_events_are_not_escalated(self):
        create_notification(event="product_added", title="New product", description="d", url="/")
        self.assertEqual(self._escalations(), [])

    @override_settings(COMPANY_ESCALATION_EMAILS=[])
    def test_no_recipients_means_no_copy(self):
        create_notification(event="sla_breach", title="Finding X breached its SLA", description="d", url="/")
        self.assertEqual(self._escalations(), [])
