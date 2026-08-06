"""Overlay a request profile onto the global settings.

Every pipeline stage already receives one duck-typed ``settings`` object, so a
per-request variant can be substituted **without changing a single stage
signature**. That is what this module produces.

Semantics: a profile carries *intent*, and only the keys it actually sets are
overridden. Everything else keeps its ``config.yaml`` value, so

    apply_profile(settings, None) is settings

and a profile that omits a key behaves exactly as the unparameterized pipeline.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..config import Settings
from ..logging_conf import get_logger
from .profile import RequestProfile

logger = get_logger("request.overlay")


def apply_profile(settings: Settings, profile: RequestProfile | None) -> Settings:
    """Return a copy of ``settings`` with ``profile``'s overrides applied.

    Args:
        settings: The validated global settings from ``config.yaml``.
        profile: The request profile, or ``None`` to return ``settings`` untouched.

    Returns:
        A new frozen :class:`~sentinel.config.Settings` carrying the profile in its
        ``profile`` field, so downstream code can read {A}..{F} when it needs them.
    """
    if profile is None:
        return settings

    app = dataclasses.replace(
        settings.app,
        report_language=profile.language,
        weeks_history=settings.app.weeks_history,
    )

    report = settings.report
    if profile.recipients or profile.subject_prefix:
        report = dataclasses.replace(
            settings.report,
            recipients=profile.recipients or settings.report.recipients,
            subject_prefix=profile.subject_prefix or settings.report.subject_prefix,
        )

    overrides: dict[str, Any] = {"app": app, "report": report, "profile": profile}
    if profile.discovery_queries is not None:
        overrides["discovery_queries"] = profile.discovery_queries
    if profile.relevance_keywords is not None:
        overrides["relevance_keywords"] = profile.relevance_keywords
    if profile.relevance_exclude is not None:
        overrides["relevance_exclude"] = profile.relevance_exclude
    if profile.topics is not None:
        overrides["topics"] = profile.topics
    if profile.feeds is not None:
        overrides["feeds"] = profile.feeds

    effective = dataclasses.replace(settings, **overrides)
    logger.info(
        "Applied profile %s — overrides: %s",
        profile.slug,
        ", ".join(k for k in overrides if k != "profile") or "(language only)",
    )
    return effective


def profile_of(settings: Any) -> RequestProfile | None:
    """Read the profile off a settings object, tolerating plain/mocked settings."""
    return getattr(settings, "profile", None)


def theme_of(settings: Any, default: str = "AI SaaS B2B") -> str:
    """The request's theme {A}, or the historical default for an unparameterized run."""
    profile = profile_of(settings)
    return profile.theme if profile else default


def assert_trend_safe(settings: Any) -> bool:
    """Whether this run may write to the shared ``trends`` table.

    The ``trends`` table is keyed ``UNIQUE(topic, week)`` **globally**, so two
    requests with different taxonomies would overwrite each other's counts. Until
    trends are namespaced per request, a profile that overrides ``topics`` is
    refused write access (it still gets its report; only trend persistence is
    skipped).
    """
    profile = profile_of(settings)
    if profile is not None and profile.has_custom_taxonomy:
        logger.warning(
            "Profile %s overrides the canonical topic list; skipping trends persistence "
            "(the trends table is keyed UNIQUE(topic, week) globally and is not yet "
            "namespaced per request).",
            profile.slug,
        )
        return False
    return True
