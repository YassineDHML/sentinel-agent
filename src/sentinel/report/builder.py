"""Report assembly + rendering (spec BF-05).

Five sections, per spec: executive summary, competitive watch, detected trends
(new/accelerating, with historical context), opportunities (cited), and sources
consulted. The narrative sections (1, 2, 4) come from the Gemini-only deep
analysis (:mod:`sentinel.analyze.deep_analysis`); trends (3) and sources (5) are
computed deterministically from stored data — no LLM involved, so they can't
drift or hallucinate.

:func:`generate_report` is the full orchestrator (DB + LLM + template + persist +
local file). :func:`build_report_context` / :func:`render_report_html` are pure
and used directly by the fixture-based unit test — no DB, no LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analyze.deep_analysis import DeepAnalysis, run_deep_analysis
from ..analyze.trends import (
    DEFAULT_WEEKS_BACK,
    build_trend_digest,
    compute_trend_statuses,
    current_iso_week,
    week_bounds_iso,
)
from ..logging_conf import get_logger
from .i18n import headings

logger = get_logger("report.builder")

# The organisation the report is written for. Kept here (rather than inline in a
# prompt/template) so it is configurable and appears in exactly one place.
DEFAULT_COMPANY = "Welyne"


def company_name(settings: Any) -> str:
    """The organisation the report addresses, from config if set."""
    return getattr(settings.app, "company", None) or DEFAULT_COMPANY

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"
TEMPLATE_NAME = "report.html"

# Safety cap on the "Sources consulted" list so the report stays email-sized even
# if a very large week is analyzed. Overflow is reported as a count, not dropped
# silently.
MAX_SOURCES_LISTED = 100

WEEKLY_REPORT_TYPE = "weekly"


def report_output_path(
    period_key: str,
    *,
    report_type: str = WEEKLY_REPORT_TYPE,
    request_slug: str | None = None,
    suffix: str = "",
    output_dir: Path | str | None = None,
) -> Path:
    """Local filesystem path for a rendered report.

    The weekly watch keeps its historical ``output/<week>.html`` name exactly, so
    existing files, tests and habits are undisturbed. Any other report type gets a
    qualified name so types/requests can't overwrite each other.

    Args:
        period_key: e.g. ``"2026-W29"``, ``"2026-M07"``.
        report_type: ``"weekly"`` (default), ``"research"``, ``"competitor"``, ...
        request_slug: Parameterized-request identifier, when applicable.
        suffix: Extra marker before the extension, e.g. ``"dryrun"``.
        output_dir: Defaults to ``<repo>/output``.
    """
    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    if report_type == WEEKLY_REPORT_TYPE:
        # Historical naming, preserved exactly: <week>.html / <week>.dryrun.html
        stem = f"{period_key}.{suffix}" if suffix else period_key
    else:
        qualifiers = [period_key, report_type]
        if request_slug:
            qualifiers.append(request_slug)
        if suffix:
            qualifiers.append(suffix)
        stem = "__".join(qualifiers)
    return out_dir / f"{stem}.html"


def _get_env(template_dir: Path | None = None) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default=True),
    )


def context_words(context: dict[str, Any]) -> list[str]:
    """Words of narrative text in a report context (for the stored word count).

    Counts only LLM-authored narrative — not headings, links or the sources list —
    so the number means "how much analysis did this report actually contain".
    """
    chunks: list[str] = []
    for item in context.get("executive_summary") or []:
        chunks.append(str(item.get("text", "")))
    for entry in context.get("competitive_watch") or []:
        chunks.append(str(entry.get("highlights", "")))
    for item in context.get("opportunities") or []:
        chunks.append(str(item.get("text", "")))
    return " ".join(chunks).split()


def profile_params(profile: Any | None) -> dict[str, Any] | None:
    """Serialize a request profile's {A}..{F} for the ``reports.params`` column."""
    if profile is None:
        return None
    return {
        "slug": getattr(profile, "slug", None),
        "theme": getattr(profile, "theme", None),
        "language": getattr(profile, "language", None),
        "geo_zone": getattr(profile, "geo_zone", None),
        "sector": getattr(profile, "sector", None),
        "objective": getattr(profile, "objective", None),
        "horizon_past_months": getattr(profile, "horizon_past_months", None),
        "horizon_future_years": getattr(profile, "horizon_future_years", None),
    }


def _title_lookup(articles: list[dict]) -> dict[str, dict[str, Any]]:
    """Map url -> {title, source} for resolving citations to displayable links."""
    return {a["url"]: {"title": a.get("title"), "source": a.get("source")} for a in articles if a.get("url")}


def _cited_for_template(sources: list[str], lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"url": url, **lookup.get(url, {})} for url in sources]


def build_report_context(
    week: str,
    settings: Any,
    articles: list[dict],
    trend_statuses: list[Any],
    deep_analysis: DeepAnalysis | None,
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Assemble the template context from stored/computed data (pure, no I/O)."""
    lookup = _title_lookup(articles)
    deep_analysis = deep_analysis or DeepAnalysis()

    trends: dict[str, list[dict[str, Any]]] = {"accelerating": [], "new": [], "ongoing": []}
    for s in trend_statuses:
        bucket = trends.get(s.status.lower())
        if bucket is not None:
            bucket.append(
                {
                    "topic": s.topic,
                    "count": s.count,
                    "prior_counts": s.prior_counts,
                    "prior_avg": s.prior_avg,
                    "actors": s.actors,
                }
            )

    all_sources = [
        {"url": a["url"], "title": a.get("title"), "source": a.get("source")}
        for a in articles
        if a.get("url")
    ]
    sources_consulted = all_sources[:MAX_SOURCES_LISTED]
    sources_overflow = max(0, len(all_sources) - MAX_SOURCES_LISTED)

    profile = getattr(settings, "profile", None)
    language = settings.app.report_language
    return {
        "app_name": settings.app.name,
        "week": week,
        "generated_at": generated_at,
        "report_language": language,
        # Localized labels so the template carries no hardcoded language.
        "labels": headings(language, company=company_name(settings)),
        "theme": getattr(profile, "theme", None),
        "request_slug": getattr(profile, "slug", None),
        "executive_summary": [
            {"text": i.text, "sources": _cited_for_template(i.sources, lookup)}
            for i in deep_analysis.executive_summary
        ],
        "competitive_watch": [
            {"actor": e.actor, "highlights": e.highlights, "sources": _cited_for_template(e.sources, lookup)}
            for e in deep_analysis.competitive_watch
        ],
        "trends": trends,
        "opportunities": [
            {"text": i.text, "sources": _cited_for_template(i.sources, lookup)}
            for i in deep_analysis.opportunities
        ],
        "sources_consulted": sources_consulted,
        "sources_overflow": sources_overflow,
    }


def render_report_html(context: dict[str, Any], *, template_dir: Path | None = None) -> str:
    """Render the Jinja2 report template with ``context``. Pure — no I/O beyond the template file."""
    env = _get_env(template_dir)
    return env.get_template(TEMPLATE_NAME).render(**context)


def generate_report(
    settings: Any,
    article_repo: Any,
    trend_repo: Any,
    report_repo: Any,
    *,
    week: str | None = None,
    client: Any | None = None,
    deep_analysis: DeepAnalysis | None = None,
    compute_deep: bool = True,
    output_dir: Path | str | None = None,
    generated_at: str | None = None,
    report_type: str = WEEKLY_REPORT_TYPE,
    request_slug: str | None = None,
) -> Path:
    """Build, render, persist, and locally save the weekly report.

    Steps: fetch this week's articles from Supabase, compute trend statuses
    (reading prior weeks back from ``trends``), run the Gemini-only deep
    analysis (degrades to an empty section on failure — never fatal), render the
    HTML, store it in the ``reports`` table, and write a local copy to
    ``output/<week>.html`` for inspection.

    Returns:
        The local filesystem path of the written HTML file.
    """
    from datetime import datetime, timezone

    week = week or current_iso_week()
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    start_iso, end_iso = week_bounds_iso(week)

    collected = article_repo.list_by_week(start_iso, end_iso)
    # The report covers ANALYZED articles (those with an LLM summary) — spec BF-05
    # asks for "articles analysés". Unanalyzed rows (collected but not summarized)
    # are excluded from both the deep-analysis context and the sources list, so a
    # 1000-article collection week doesn't produce a 1000-link report.
    articles = [a for a in collected if a.get("summary")]
    logger.info(
        "Report %s: %d collected, %d analyzed (used for the report).",
        week, len(collected), len(articles),
    )

    # weeks_back comes from config (app.weeks_history) rather than the module
    # default, so the comparison window is configurable per request.
    weeks_back = getattr(settings.app, "weeks_history", None) or DEFAULT_WEEKS_BACK
    # Trends are namespaced per request. Scoping here rather than at the call site
    # means every caller (pipeline, `python -m sentinel.report --live`) reads the
    # right history without passing an extra argument.
    from ..analyze.trends import scoped_repo
    from ..request.overlay import trend_scope_of

    trend_statuses = compute_trend_statuses(
        scoped_repo(trend_repo, trend_scope_of(settings)), week=week, weeks_back=weeks_back)
    digest = build_trend_digest(trend_statuses, week=week)

    # The caller (e.g. the pipeline) may have already run the Gemini-only deep
    # analysis — pass it via deep_analysis / compute_deep=False to avoid a second
    # heavy LLM call. Left to its own devices, this function computes it.
    if deep_analysis is None and compute_deep:
        deep_analysis = run_deep_analysis(articles, digest, settings, client=client)
    if deep_analysis is None:
        logger.warning("Deep analysis unavailable this week; report will omit sections 1/2/4.")

    context = build_report_context(
        week, settings, articles, trend_statuses, deep_analysis, generated_at=generated_at
    )
    html = render_report_html(context)

    profile = getattr(settings, "profile", None)
    try:
        report_repo.store(
            week, html,
            report_type=report_type,
            request_slug=request_slug,
            language=settings.app.report_language,
            period_kind="week",
            period_key=week,
            period_start=start_iso,
            period_end=end_iso,
            word_count=len(context_words(context)),
            params=profile_params(profile),
        )
    except Exception as exc:  # noqa: BLE001 - the local file is the fallback record
        logger.error("Failed to persist report to DB (continuing, local copy still written): %s", exc)

    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_output_path(week, report_type=report_type,
                                  request_slug=request_slug, output_dir=out_dir)
    out_path.write_text(html, encoding="utf-8")
    logger.info("Report written to %s", out_path)
    return out_path
