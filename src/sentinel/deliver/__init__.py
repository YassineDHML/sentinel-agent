"""Delivery layer: email (Gmail SMTP) + optional Slack webhook (spec BF-06)."""

from .email import send_report
from .slack import notify

__all__ = ["send_report", "notify"]
