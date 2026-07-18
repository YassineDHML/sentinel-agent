"""Optional Slack delivery via an incoming webhook (spec BF-06).

Posts a short notification (not the full HTML — Slack webhooks take plain
text/blocks) to a channel. The webhook URL is a secret; if it isn't configured
the collector **no-ops cleanly** (logs + returns False) so Slack stays optional.

``dry_run`` logs the payload without posting.
"""

from __future__ import annotations

from typing import Any

from ..config import get_secret
from ..collect.base import DEFAULT_TIMEOUT, get_session
from ..logging_conf import get_logger

logger = get_logger("deliver.slack")


def build_payload(settings: Any, week: str, *, report_url: str | None = None,
                  highlights: list[str] | None = None) -> dict:
    """Build a Slack message payload summarizing the week's report."""
    lines = [f":newspaper: *{settings.app.name} — weekly report {week}*"]
    for h in (highlights or [])[:5]:
        lines.append(f"• {h}")
    if report_url:
        lines.append(f"<{report_url}|View full report>")
    return {"text": "\n".join(lines)}


def notify(
    settings: Any,
    week: str,
    *,
    report_url: str | None = None,
    highlights: list[str] | None = None,
    dry_run: bool = False,
    webhook_url: str | None = None,
) -> bool:
    """Post a report notification to Slack. No-op (returns False) if unconfigured.

    Args:
        settings: Config (app name).
        week: ISO week for the message.
        report_url: Optional link to the hosted/archived report.
        highlights: Optional short bullet highlights (e.g. top exec-summary items).
        dry_run: If True, log the payload but don't post.
        webhook_url: Override the ``SLACK_WEBHOOK_URL`` secret (mainly for tests).

    Returns:
        True if posted (or would be, in dry-run); False if no webhook is configured.
    """
    url = webhook_url or get_secret("SLACK_WEBHOOK_URL", required=False)
    if not url:
        logger.info("Slack: no SLACK_WEBHOOK_URL configured; skipping (optional).")
        return False

    payload = build_payload(settings, week, report_url=report_url, highlights=highlights)

    if dry_run:
        logger.info("[dry-run] Slack NOT posted. Payload=%r", payload)
        return True

    try:
        # Slack incoming webhooks reply with the plain text "ok" (NOT JSON), so we
        # check the HTTP status and must not try to parse the body as JSON.
        response = get_session().post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - Slack is optional; never break the run
        logger.error("Slack post failed (continuing): %s", exc)
        return False
    logger.info("Slack notification posted for week %s.", week)
    return True
