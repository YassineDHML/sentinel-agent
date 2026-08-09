"""Email delivery via Gmail SMTP (spec BF-06).

Sends the HTML report over Gmail's SMTP-over-SSL endpoint using ``smtplib`` (stdlib).
Requires a Gmail **app password** (account must have 2FA enabled) — never the real
account password. Credentials come from the environment; recipients + subject
prefix come from config.

``--dry-run`` (via ``send_report(..., dry_run=True)``) renders/logs but does not
open a connection or send.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from ..config import get_secret
from ..logging_conf import get_logger

logger = get_logger("deliver.email")

SMTP_HOST = "smtp.gmail.com"
SMTP_SSL_PORT = 465

REQUIRED_SECRETS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


def build_message(
    html: str,
    *,
    subject: str,
    sender: str,
    recipients: list[str],
    sender_name: str | None = None,
) -> EmailMessage:
    """Build a multipart HTML email (with a minimal plain-text fallback part)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = ", ".join(recipients)
    # Plain-text fallback for clients that won't render HTML.
    msg.set_content("This report is best viewed in an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    return msg


def _subject(settings: Any, week: str) -> str:
    prefix = settings.report.subject_prefix
    return f"{prefix} — {week}"


def send_report(
    html: str,
    settings: Any,
    *,
    week: str,
    dry_run: bool = False,
    recipients: list[str] | None = None,
    subject: str | None = None,
    smtp_factory: Any | None = None,
) -> bool:
    """Send the report HTML to the configured recipients via Gmail SMTP.

    Args:
        html: The rendered report HTML.
        settings: Config providing recipients + subject prefix.
        week: ISO week, included in the subject line.
        dry_run: If True, log what would be sent and return without connecting.
        recipients: Override the config recipient list (e.g. for a test send).
        subject: Use this subject verbatim instead of ``"<prefix> — <week>"``.
            Scheduled requests supply their own so two requests delivered the same
            week don't arrive with identical subject lines. ``None`` keeps the
            historical weekly subject exactly.
        smtp_factory: Testing seam — a callable returning a context-manager SMTP
            client. Defaults to ``smtplib.SMTP_SSL(host, port)``.

    Returns:
        True if a message was sent (or would be, in dry-run); False if there were
        no recipients.
    """
    to = recipients if recipients is not None else settings.report.recipients
    subject = subject or _subject(settings, week)

    if not to:
        logger.warning("Email: no recipients configured; skipping send.")
        return False

    if dry_run:
        logger.info("[dry-run] Email NOT sent. Subject=%r To=%s (%d bytes HTML)",
                    subject, ", ".join(to), len(html))
        return True

    sender = get_secret("GMAIL_ADDRESS")
    password = get_secret("GMAIL_APP_PASSWORD")
    message = build_message(html, subject=subject, sender=sender, recipients=to,
                            sender_name=settings.app.name)

    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_SSL_PORT))
    with factory() as server:
        server.login(sender, password)
        server.send_message(message)
    logger.info("Email sent: subject=%r to=%s", subject, ", ".join(to))
    return True
