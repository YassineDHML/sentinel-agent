"""Send (or dry-run) a report via email and/or Slack.

    # dry-run: render/log, send nothing
    python -m sentinel.deliver --dry-run

    # real test send to yourself (overrides config recipients):
    python -m sentinel.deliver --to you@gmail.com --html output/2026-W28.demo.html

    # also post to Slack (if SLACK_WEBHOOK_URL is set):
    python -m sentinel.deliver --to you@gmail.com --slack

If --html is omitted, a demo report is rendered on the fly so you can test
delivery without a DB. Env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (app password,
2FA required); optionally SLACK_WEBHOOK_URL.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from .email import send_report
from .slack import notify

logger = get_logger("deliver.cli")

DEFAULT_WEEK = "2026-W28"


def _demo_html(settings) -> str:
    from ..report.__main__ import _demo_context
    from ..report.builder import render_report_html

    context, _ = _demo_context(settings)
    return render_report_html(context)


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.deliver")
    parser.add_argument("--html", default=None, help="path to a report HTML file (default: demo)")
    parser.add_argument("--to", action="append", default=None,
                        help="recipient override (repeatable); default = config recipients")
    parser.add_argument("--week", default=DEFAULT_WEEK, help="ISO week for the subject line")
    parser.add_argument("--slack", action="store_true", help="also post a Slack notification")
    parser.add_argument("--dry-run", action="store_true", help="render/log but send nothing")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8")
    else:
        html = _demo_html(settings)
        logger.info("No --html given; using a rendered demo report (%d bytes).", len(html))

    if not args.dry_run:
        require_secrets(["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"])

    sent = send_report(html, settings, week=args.week, dry_run=args.dry_run, recipients=args.to)
    logger.info("Email delivery result: %s", "sent/dry-run" if sent else "skipped (no recipients)")

    if args.slack:
        notify(settings, args.week, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
