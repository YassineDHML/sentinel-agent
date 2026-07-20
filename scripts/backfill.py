"""Backfill historical trend data (spec BF-04 bootstrap).

The NEW-vs-ACCELERATING distinction only means something once the `trends` table
holds several weeks of history. This script seeds that history so the **first
live report already has something to compare against**.

For each of the past N ISO weeks it:
  1. runs a **date-bounded Google News search** per discovery query
     (`<query> after:<mon> before:<next-mon>`) — regular RSS feeds only expose a
     recent rolling window, so dated news search is how we reach the past;
  2. filters + dedups the results (reusing the pipeline's own `process_articles`);
  3. **classifies** each article into canonical tags via the LLM — classification
     ONLY: no summaries, no full-text fetch, no deep analysis, to keep quota tiny;
  4. upserts the per-topic counts into `trends` for that week.

It writes ONLY to `trends`, never to `articles`, so it seeds history without
polluting the current-week article flow. All heavy lifting reuses existing,
tested modules — this script just orchestrates them over past weeks.

Usage:
    python scripts/backfill.py --weeks 4
    python scripts/backfill.py --weeks 6 --limit-per-week 40
    python scripts/backfill.py --weeks 4 --dry-run          # classify + count, no DB writes

Requires an LLM key (GEMINI_API_KEY, or GROQ_API_KEY as fallback). Live mode also
requires SUPABASE_URL / SUPABASE_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys

from sentinel.analyze.llm import LLMClient
from sentinel.analyze.trends import (
    count_topics,
    current_iso_week,
    prior_weeks,
    record_week_trends,
    week_bounds_iso,
)
from sentinel.collect.googlenews import collect_googlenews
from sentinel.config import ConfigError, load_settings, require_secrets
from sentinel.logging_conf import get_logger, setup_logging
from sentinel.process import process_articles

logger = get_logger("backfill")

DEFAULT_WEEKS = 4
DEFAULT_LIMIT_PER_WEEK = 40   # cap classified articles/week to bound LLM quota


def build_week_queries(base_queries: list[str], week: str) -> list[str]:
    """Turn each discovery query into a Google-News query bounded to one ISO week.

    Uses Google News' ``after:`` / ``before:`` date operators over the week's
    Monday → next-Monday span (from :func:`week_bounds_iso`).
    """
    start_iso, end_iso = week_bounds_iso(week)
    after, before = start_iso[:10], end_iso[:10]  # YYYY-MM-DD
    return [f"{q} after:{after} before:{before}" for q in base_queries]


def run_backfill(
    settings,
    weeks: int,
    *,
    end_week: str | None = None,
    limit_per_week: int = DEFAULT_LIMIT_PER_WEEK,
    dry_run: bool = False,
    collect_fn=collect_googlenews,
    client: LLMClient | None = None,
    trend_repo=None,
) -> dict[str, dict]:
    """Backfill the ``weeks`` ISO weeks before ``end_week`` (default: current week).

    Externals (collector, LLM client, trend repo) are injectable for testing.
    Returns ``{week: {topic: TopicCount}}`` for reporting.
    """
    base = end_week or current_iso_week()
    week_list = list(reversed(prior_weeks(base, weeks)))  # oldest → newest
    client = client or LLMClient.from_settings(settings)
    persist = trend_repo is not None and not dry_run

    results: dict[str, dict] = {}
    for week in week_list:
        queries = build_week_queries(settings.discovery_queries, week)
        raw = collect_fn(queries)
        processed = process_articles(raw, settings, repo=None)  # filter + dedup, no persist
        articles = processed.articles[:limit_per_week]

        if articles:
            topics_map = client.classify_batch(articles, settings.topics)
            for a in articles:
                a["topics"] = topics_map.get(a["url"], [])

        if persist:
            counts = record_week_trends(articles, trend_repo, week=week)
        else:
            counts = count_topics(articles)
        results[week] = counts
        logger.info(
            "week=%s collected=%d classified=%d topics=%d%s",
            week, len(raw), len(articles), len(counts), "" if persist else " [not persisted]",
        )
    return results


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python scripts/backfill.py")
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                        help=f"how many past weeks to backfill (default {DEFAULT_WEEKS})")
    parser.add_argument("--limit-per-week", type=int, default=DEFAULT_LIMIT_PER_WEEK,
                        help=f"max articles classified per week (default {DEFAULT_LIMIT_PER_WEEK})")
    parser.add_argument("--end-week", default=None,
                        help="ISO week to count back from (default: current week)")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify + count but don't write to the trends table")
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Config invalid: %s", exc)
        return 1

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY")):
        logger.error("Backfill needs an LLM key (GEMINI_API_KEY or GROQ_API_KEY) for classification.")
        return 1

    trend_repo = None
    if not args.dry_run:
        try:
            require_secrets(["SUPABASE_URL", "SUPABASE_KEY"])
            from sentinel.db import SupabaseDB, TrendRepository

            trend_repo = TrendRepository(SupabaseDB.connect())
        except Exception as exc:  # noqa: BLE001
            logger.error("FATAL: cannot connect to Supabase for backfill: %s", exc)
            return 1

    results = run_backfill(
        settings, args.weeks, end_week=args.end_week,
        limit_per_week=args.limit_per_week, dry_run=args.dry_run, trend_repo=trend_repo,
    )

    print(f"\n=== Backfill summary ({'dry-run' if args.dry_run else 'written to trends'}) ===")
    for week in sorted(results):
        counts = results[week]
        top = sorted(counts.values(), key=lambda c: c.article_count, reverse=True)[:5]
        top_str = ", ".join(f"{c.topic}({c.article_count})" for c in top) or "(none)"
        print(f"  {week}: {len(counts)} topics — top: {top_str}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
