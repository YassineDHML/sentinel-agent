"""Tests for the delivery layer. SMTP and the Slack webhook are fully mocked —
no real emails, no real HTTP.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sentinel.config import load_settings
from sentinel.deliver import email as email_mod
from sentinel.deliver import slack as slack_mod

HTML = "<html><body>report</body></html>"


def _settings():
    return load_settings(load_env=False)


class FakeSMTP:
    """Context-manager stand-in for smtplib.SMTP_SSL."""

    def __init__(self):
        self.logged_in = None
        self.sent = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


# --------------------------------------------------------------------------- #
# email
# --------------------------------------------------------------------------- #
def test_build_message_headers_and_html_part():
    msg = email_mod.build_message(
        HTML, subject="Sub", sender="me@gmail.com", recipients=["a@x.com", "b@x.com"],
        sender_name="Sentinel",
    )
    assert msg["Subject"] == "Sub"
    assert "me@gmail.com" in msg["From"] and "Sentinel" in msg["From"]
    assert msg["To"] == "a@x.com, b@x.com"
    # has an HTML alternative part
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert html_parts and "report" in html_parts[0].get_content()


def test_subject_includes_week():
    s = _settings()
    assert email_mod._subject(s, "2026-W28") == f"{s.report.subject_prefix} — 2026-W28"


def test_send_report_dry_run_does_not_connect():
    factory = MagicMock()
    ok = email_mod.send_report(HTML, _settings(), week="2026-W28", dry_run=True,
                               recipients=["me@x.com"], smtp_factory=factory)
    assert ok is True
    factory.assert_not_called()  # no SMTP connection in dry-run


def test_send_report_no_recipients_returns_false():
    ok = email_mod.send_report(HTML, _settings(), week="2026-W28", recipients=[])
    assert ok is False


def test_send_report_real_logs_in_and_sends(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass-1234")
    fake = FakeSMTP()

    ok = email_mod.send_report(
        HTML, _settings(), week="2026-W28", recipients=["dest@x.com"],
        smtp_factory=lambda: fake,
    )
    assert ok is True
    assert fake.logged_in == ("me@gmail.com", "app-pass-1234")
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["To"] == "dest@x.com"
    assert "2026-W28" in sent["Subject"]


# --------------------------------------------------------------------------- #
# slack
# --------------------------------------------------------------------------- #
def test_build_payload_contains_week_and_highlights():
    payload = slack_mod.build_payload(
        _settings(), "2026-W28", report_url="https://r/x", highlights=["a", "b"]
    )
    text = payload["text"]
    assert "2026-W28" in text
    assert "• a" in text and "• b" in text
    assert "https://r/x" in text


def test_notify_no_webhook_is_noop(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert slack_mod.notify(_settings(), "2026-W28") is False


def test_notify_dry_run_does_not_post(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(slack_mod, "http_post_json", called)
    ok = slack_mod.notify(_settings(), "2026-W28", dry_run=True, webhook_url="https://hook")
    assert ok is True
    called.assert_not_called()


def test_notify_posts_to_webhook(monkeypatch):
    captured = {}

    def fake_post(url, payload, **kw):
        captured["url"] = url
        captured["payload"] = payload
        return {}

    monkeypatch.setattr(slack_mod, "http_post_json", fake_post)
    ok = slack_mod.notify(_settings(), "2026-W28", webhook_url="https://hooks.slack.com/x",
                          highlights=["big news"])
    assert ok is True
    assert captured["url"] == "https://hooks.slack.com/x"
    assert "big news" in captured["payload"]["text"]


def test_notify_post_failure_returns_false(monkeypatch):
    def boom(url, payload, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(slack_mod, "http_post_json", boom)
    ok = slack_mod.notify(_settings(), "2026-W28", webhook_url="https://hook")
    assert ok is False  # optional channel: failure never raises
