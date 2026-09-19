"""
company: notification routing on top of the upstream NotificationManager.

Wired from dojo/settings/local_settings.py:
    NOTIFICATION_MANAGER = "dojo.company.notifications.CompanyNotificationManager"

Added behaviour: escalation copies. For every event listed in
COMPANY_ESCALATION_EVENTS one e-mail per event goes to the addresses in
COMPANY_ESCALATION_EMAILS, independent of any user's notification settings
and of the per-user mail toggle. Everything else is upstream behaviour.
Wording changes go in dojo/company/templates/notifications/<channel>/<event>.tpl
(the company template directory is searched first).
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from dojo.notifications.helper import NotificationManager

logger = logging.getLogger(__name__)


class CompanyNotificationManager(NotificationManager):

    """Upstream manager plus one escalation copy per event for the configured events."""

    def create_notification(self, event: str | None = None, **kwargs: dict) -> None:
        super().create_notification(event=event, **kwargs)
        self._send_escalation_copy(event, kwargs)

    def _send_escalation_copy(self, event: str | None, kwargs: dict) -> None:
        recipients = list(getattr(settings, "COMPANY_ESCALATION_EMAILS", []))
        events = set(getattr(settings, "COMPANY_ESCALATION_EVENTS", []))
        if not recipients or event not in events:
            return
        title = str(kwargs.get("title") or "")
        subject = f"[{settings.COMPANY_NAME}] {event}: {title}".rstrip(": ")
        text = str(kwargs.get("description") or title or event)
        try:
            html = self._create_notification_message(event, None, "mail", dict(kwargs))
        except Exception:
            logger.exception("company escalation: mail template for %s failed, sending the plain body", event)
            html = None
        message = EmailMultiAlternatives(subject, text, self.system_settings.email_from, recipients)
        if html:
            message.attach_alternative(html, "text/html")
        try:
            message.send(fail_silently=False)
        except Exception:
            logger.exception("company escalation mail for %s could not be sent", event)
