"""Sentinel pipeline — the single weekly entrypoint (spec §4, BF-01..BF-07).

Runs the full flow, in order:

    collect -> process (filter/dedup/store) -> fulltext -> analyze (summarize/
    classify) -> trends -> report (deep analysis + render + archive) -> deliver

Graceful degradation at every stage (spec BF-07):
* a failing **source** is skipped (logged, counted, run continues);
* if **Gemini** is unavailable but Groq is, light tasks (summaries/classification)
  run on Groq and the Gemini-only deep analysis is skipped — the report keeps its
  deterministic sections (trends, sources) and the degradation is logged;
* if **no LLM** is available, analysis is skipped entirely (collection/dedup still
  run, nothing is marked processed);
* a **delivery** failure is logged loudly and fails the run's exit code in live
  mode (so CI shows red) without losing the report — it's already archived in the
  DB and written to ``output/``.

Every stage logs one structured line: ``stage=<name> count=<n> elapsed=<s>``.

Usage:
    python -m sentinel.pipeline --dry-run [--limit N]     # no DB writes, no email
    python -m sentinel.pipeline [--week 2026-W28] [--since 2026-07-01] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .analyze import analyze_articles
from .analyze.llm import GeminiProvider, GroqProvider, LLMClient
from .analyze.trends import (
    DEFAULT_WEEKS_BACK,
    build_trend_digest,
    compute_trend_statuses,
    current_iso_week,
    record_week_trends,
    week_bounds_iso,
)
from .collect.fulltext import fetch_fulltext
from .collect.gnews import collect_gnews
from .collect.googlenews import collect_googlenews
from .collect.hackernews import collect_hackernews
from .collect.producthunt import collect_producthunt
from .collect.rss import collect_rss
from .config import ConfigError, load_settings, require_secrets
from .deliver.email import send_report
from .deliver.slack import notify
from .logging_conf import get_logger, setup_logging
from .process import process_articles
from .request import apply_profile, assert_trend_safe, load_profile, profile_of, resolve_locale
from .request.locale import zone_is_global

logger = get_logger("pipeline")

DEFAULT_ANALYZE_LIMIT = 25  # articles per run; keeps LLM batches within free-tier RPM


# --------------------------------------------------------------------------- #
# Result / bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class StageResult:
    name: str
    count: int = 0
    elapsed: float = 0.0
    note: str = ""


@dataclass
class PipelineResult:
    stages: list[StageResult] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    report_path: Path | None = None
    ok: bool = True

    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages]


class _Timer:
    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self._t0
        return False


def _log_stage(result: PipelineResult, name: str, count: int, elapsed: float, note: str = "") -> None:
    result.stages.append(StageResult(name, count, elapsed, note))
    suffix = f" note={note}" if note else ""
    logger.info("stage=%s count=%d elapsed=%.1fs%s", name, count, elapsed, suffix)


# --------------------------------------------------------------------------- #
# In-memory repos (dry-run only: same interface, no network)
# --------------------------------------------------------------------------- #
class _MemoryTrendRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert(self, topic, week, article_count, actors=None):
        self.rows[(topic, week)] = {
            "topic": topic, "week": week, "article_count": article_count, "actors": actors or {}
        }

    def get_by_week(self, week):
        return [r for (t, w), r in self.rows.items() if w == week]


# --------------------------------------------------------------------------- #
# LLM tiering (spec BF-03)
# --------------------------------------------------------------------------- #
def _build_llm(settings: Any, result: PipelineResult) -> tuple[LLMClient | None, bool]:
    """Return ``(light_task_client, deep_analysis_available)`` per the tiered policy.

    Gemini present            -> Gemini primary (+ Groq fallback), deep analysis ON.
    Only Groq present         -> Groq-only light tasks, deep analysis OFF (12k TPM
                                 is too small for historical context — never send it).
    Neither                   -> no analysis at all.
    """
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_groq = bool(os.environ.get("GROQ_API_KEY"))

    if has_gemini:
        fallback = GroqProvider(settings.llm.groq_model) if has_groq else None
        return LLMClient(GeminiProvider(settings.llm.gemini_model), fallback,
                         batch_size=settings.llm.batch_size), True
    if has_groq:
        result.degradations.append("gemini unavailable: Groq-only light tasks, deep analysis skipped")
        logger.warning("DEGRADED: no Gemini key; summaries/classification on Groq only, "
                       "deep analysis (Gemini-only) skipped.")
        return LLMClient(GroqProvider(settings.llm.groq_model), None,
                         batch_size=settings.llm.batch_size), False
    result.degradations.append("no LLM keys: analysis skipped entirely")
    logger.warning("DEGRADED: no LLM keys present; skipping analysis, trends will use history only.")
    return None, False


def _since_filter(articles: list[dict], since: str | None) -> list[dict]:
    """Keep articles published on/after ``since`` (ISO date). Unparseable dates are kept."""
    if not since:
        return articles
    cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    kept = []
    for a in articles:
        raw = a.get("published_at")
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
        except ValueError:
            when = None
        if when is None or when >= cutoff:
            kept.append(a)
    return kept


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(
    settings: Any,
    *,
    dry_run: bool = False,
    week: str | None = None,
    since: str | None = None,
    analyze_limit: int = DEFAULT_ANALYZE_LIMIT,
    collectors: dict[str, Callable[[], list[dict]]] | None = None,
    llm_client: LLMClient | None = None,
    deep_available: bool | None = None,
    article_repo: Any = None,
    trend_repo: Any = None,
    report_repo: Any = None,
    emailer: Callable[..., bool] = send_report,
    slacker: Callable[..., bool] = notify,
) -> PipelineResult:
    """Run the full weekly flow. All externals are injectable for testing.

    In live mode the three repos must be provided (the CLI wires them from
    Supabase); in dry-run they're ignored and nothing is persisted or sent.
    """
    result = PipelineResult()
    week = week or current_iso_week()
    logger.info("Sentinel pipeline starting: week=%s dry_run=%s since=%s", week, dry_run, since)

    # --- LLM tiering ------------------------------------------------------- #
    if llm_client is None and deep_available is None:
        llm_client, deep_ok = _build_llm(settings, result)
    else:
        deep_ok = bool(deep_available)

    # --- 1. collect -------------------------------------------------------- #
    # A request profile ({B} language x {C} zone) selects the source locale; without
    # one these resolve to the historical English/US parameters.
    profile = profile_of(settings)
    locale = profile.locale if profile is not None else resolve_locale()
    gn_params = locale.googlenews_params()
    gnews_kwargs = locale.gnews_params(
        include_country=profile is not None and not zone_is_global(profile.geo_zone)
    )
    sources = collectors or {
        "rss": lambda: collect_rss(settings.feeds),
        "hackernews": lambda: collect_hackernews(settings.discovery_queries),
        "producthunt": lambda: collect_producthunt(),
        "googlenews": lambda: collect_googlenews(
            settings.discovery_queries, locale_params=gn_params),
        "gnews": lambda: collect_gnews(settings.discovery_queries, **gnews_kwargs),
    }
    raw: list[dict] = []
    with _Timer() as t:
        for name, collector in sources.items():
            if not settings.sources.get(name, True):
                logger.info("Source '%s' disabled in config; skipping.", name)
                continue
            try:
                got = collector()
                raw.extend(got)
                logger.info("source=%s collected=%d", name, len(got))
            except Exception as exc:  # noqa: BLE001 - one dead source never kills the run
                result.degradations.append(f"source '{name}' failed: {exc}")
                logger.warning("DEGRADED: source '%s' failed (%s); continuing without it.", name, exc)
    if since:
        before = len(raw)
        raw = _since_filter(raw, since)
        logger.info("--since %s: %d -> %d article(s).", since, before, len(raw))
    _log_stage(result, "collect", len(raw), t.elapsed)

    # --- 2. process (filter / dedup / store) ------------------------------- #
    with _Timer() as t:
        processed = process_articles(raw, settings, repo=None if dry_run else article_repo)
    _log_stage(result, "process", len(processed.articles), t.elapsed,
               note=f"filtered={processed.after_filter} deduped={processed.after_title_dedup}"
                    + ("" if dry_run else f" new={processed.persisted_new}"))

    # Articles to analyze this run: THIS week's un-summarized articles, newest first,
    # capped at analyze_limit. Scoping to the week keeps analyze and report aligned —
    # the report covers this week's articles, so we must analyze this week's articles
    # (not arbitrary old unprocessed leftovers, which would leave the report empty).
    if dry_run:
        to_analyze = processed.articles[:analyze_limit]
    else:
        week_start, week_end = week_bounds_iso(week)
        this_week = [a for a in article_repo.list_by_week(week_start, week_end)
                     if not a.get("summary")]
        this_week.sort(key=lambda a: a.get("collected_at") or "", reverse=True)
        to_analyze = this_week[:analyze_limit]

    # --- 3. fulltext (only useful if an LLM will read it) ------------------ #
    with _Timer() as t:
        fetched = 0
        if llm_client is not None:
            for a in to_analyze:
                if a.get("content"):
                    continue
                text = fetch_fulltext(a["url"])
                if text:
                    a["content"] = text
                    fetched += 1
                    if not dry_run:
                        try:
                            article_repo.set_content(a["url"], text)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("content cache write failed for %s: %s", a["url"], exc)
    _log_stage(result, "fulltext", fetched, t.elapsed,
               note="skipped (no LLM)" if llm_client is None else "")

    # --- 4. analyze (summaries + canonical topics) ------------------------- #
    with _Timer() as t:
        if llm_client is not None:
            analyzed = analyze_articles(to_analyze, settings, client=llm_client,
                                        repo=None if dry_run else article_repo)
            analyzed = [a for a in analyzed if a.get("summary")]
        else:
            analyzed = []
    _log_stage(result, "analyze", len(analyzed), t.elapsed,
               note="skipped (no LLM)" if llm_client is None else "")

    # --- 5. trends (record this week + statuses vs history) ---------------- #
    with _Timer() as t:
        # A profile with a custom taxonomy must not write to the shared trends table
        # (keyed UNIQUE(topic, week) globally); it still gets statuses in-memory.
        trend_safe = assert_trend_safe(settings)
        t_repo = _MemoryTrendRepo() if (dry_run or not trend_safe) else trend_repo
        record_week_trends(analyzed, t_repo, week=week)
        weeks_back = getattr(settings.app, "weeks_history", None) or DEFAULT_WEEKS_BACK
        statuses = compute_trend_statuses(t_repo, week=week, weeks_back=weeks_back)
        digest = build_trend_digest(statuses, week=week)
    _log_stage(result, "trends", len(statuses), t.elapsed,
               note="dry-run: vs empty history" if dry_run
               else ("" if trend_safe else "not persisted (custom taxonomy)"))

    # --- 6. report (deep analysis + render + archive) ----------------------- #
    with _Timer() as t:
        generated_at = datetime.now(timezone.utc).isoformat()
        from .report.builder import (
            DEFAULT_OUTPUT_DIR,
            build_report_context,
            generate_report,
            render_report_html,
            report_output_path,
        )
        from .analyze.deep_analysis import run_deep_analysis

        deep = None
        if deep_ok and llm_client is not None and analyzed:
            deep = run_deep_analysis(analyzed, digest, settings,
                                     client=LLMClient(llm_client.primary, fallback=None))
            if deep is None:
                result.degradations.append("deep analysis failed: report omits narrative sections")
                logger.warning("DEGRADED: deep analysis unavailable; report keeps trends+sources only.")

        # A profiled run must not overwrite the weekly watch's output file, so its
        # report type/slug qualify the filename; unprofiled runs keep <week>.html.
        report_type = getattr(profile, "report_type", None) or "weekly"
        if report_type == "weekly_watch":
            report_type = "weekly"
        slug = getattr(profile, "slug", None)

        if dry_run:
            context = build_report_context(week, settings, analyzed, statuses, deep,
                                           generated_at=generated_at)
            html = render_report_html(context)
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            report_path = report_output_path(week, report_type=report_type,
                                             request_slug=slug, suffix="dryrun")
            report_path.write_text(html, encoding="utf-8")
        else:
            # deep analysis was already computed above (or intentionally skipped);
            # compute_deep=False prevents a second heavy Gemini call.
            report_path = generate_report(settings, article_repo, trend_repo, report_repo,
                                          week=week, deep_analysis=deep, compute_deep=False,
                                          generated_at=generated_at,
                                          report_type=report_type, request_slug=slug)
            html = report_path.read_text(encoding="utf-8")
    result.report_path = report_path
    _log_stage(result, "report", 1, t.elapsed, note=str(report_path))

    # --- 7. deliver --------------------------------------------------------- #
    with _Timer() as t:
        delivered = 0
        try:
            if emailer(html, settings, week=week, dry_run=dry_run):
                delivered += 1
        except Exception as exc:  # noqa: BLE001 - loud, and fails the exit code in live mode
            result.degradations.append(f"EMAIL DELIVERY FAILED: {exc}")
            logger.error("EMAIL DELIVERY FAILED (report is archived in DB and at %s): %s",
                         report_path, exc)
            if not dry_run:
                result.ok = False
        try:
            highlights = [i.text for i in deep.executive_summary] if deep else None
            if slacker(settings, week, highlights=highlights, dry_run=dry_run):
                delivered += 1
        except Exception as exc:  # noqa: BLE001 - Slack is optional, never fatal
            result.degradations.append(f"slack delivery failed: {exc}")
            logger.error("Slack delivery failed (optional, continuing): %s", exc)
    _log_stage(result, "deliver", delivered, t.elapsed)

    # --- summary ------------------------------------------------------------ #
    if result.degradations:
        logger.warning("Pipeline finished DEGRADED (%d issue(s)): %s",
                       len(result.degradations), " | ".join(result.degradations))
    else:
        logger.info("Pipeline finished clean: %s", " -> ".join(result.stage_names()))
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect/analyze but no DB writes, no email/Slack")
    parser.add_argument("--week", default=None, help="ISO week override, e.g. 2026-W28")
    parser.add_argument("--since", default=None, help="drop articles published before this date (reruns)")
    parser.add_argument("--limit", type=int, default=DEFAULT_ANALYZE_LIMIT,
                        help=f"max articles to analyze this run (default {DEFAULT_ANALYZE_LIMIT})")
    parser.add_argument("--profile", default=None,
                        help="request profile YAML (e.g. requests/ai_healthcare_fr.yaml) "
                             "supplying the {A}..{F} parameters")
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
        if args.profile:
            settings = apply_profile(settings, load_profile(args.profile))
    except ConfigError as exc:
        logger.error("Config invalid: %s", exc)
        return 1

    article_repo = trend_repo = report_repo = None
    if not args.dry_run:
        try:
            require_secrets(["SUPABASE_URL", "SUPABASE_KEY", "GEMINI_API_KEY"])
            from .db import ArticleRepository, ReportRepository, SupabaseDB, TrendRepository

            db = SupabaseDB.connect()
            article_repo, trend_repo, report_repo = (
                ArticleRepository(db), TrendRepository(db), ReportRepository(db)
            )
        except Exception as exc:  # noqa: BLE001 - fatal: live mode needs the DB
            logger.error("FATAL: cannot initialize live mode: %s", exc)
            return 1

    result = run_pipeline(
        settings,
        dry_run=args.dry_run,
        week=args.week,
        since=args.since,
        analyze_limit=args.limit,
        article_repo=article_repo,
        trend_repo=trend_repo,
        report_repo=report_repo,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
