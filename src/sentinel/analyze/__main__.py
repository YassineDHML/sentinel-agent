"""Run LLM analysis on a handful of stored (unprocessed) articles.

    python -m sentinel.analyze [--limit N] [--no-fulltext] [--no-persist]

Pulls up to N articles with processed=false from Supabase, optionally enriches
each with fetched full text (the articles table stores only metadata + summary,
not body text), summarizes + classifies them, prints the results, and persists
summary/topics back with processed=true.

Requires SUPABASE_URL/SUPABASE_KEY and GEMINI_API_KEY (GROQ_API_KEY optional).
"""

from __future__ import annotations

import argparse

from ..config import load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from ..db import ArticleRepository, SupabaseDB
from . import analyze_articles

logger = get_logger("analyze.cli")


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.analyze")
    parser.add_argument("--limit", type=int, default=5, help="max stored articles to analyze")
    parser.add_argument("--no-fulltext", action="store_true", help="don't fetch article body text")
    parser.add_argument("--no-persist", action="store_true", help="don't write results back to DB")
    args = parser.parse_args()

    setup_logging()
    require_secrets(["SUPABASE_URL", "SUPABASE_KEY", "GEMINI_API_KEY"])
    settings = load_settings()

    db = SupabaseDB.connect()
    repo = ArticleRepository(db)
    articles = repo.list_unprocessed(limit=args.limit)
    if not articles:
        print(
            "No unprocessed articles in the DB. Seed a few first, e.g.:\n"
            "  python -c \"from sentinel.config import load_settings; "
            "from sentinel.collect.googlenews import collect_googlenews; "
            "from sentinel.process import process_articles; "
            "from sentinel.db import SupabaseDB, ArticleRepository; "
            "s=load_settings(); "
            "process_articles(collect_googlenews(['OpenAI'])[:10], s, "
            "repo=ArticleRepository(SupabaseDB.connect()))\""
        )
        return 0

    if not args.no_fulltext:
        from ..collect.fulltext import fetch_fulltext

        for a in articles:
            a["content"] = fetch_fulltext(a["url"])

    analyzed = analyze_articles(
        articles, settings, repo=None if args.no_persist else repo
    )

    print(f"\n=== Analyzed {len(analyzed)} article(s) ===")
    for a in analyzed:
        print(f"\n- {a.get('title')}\n  URL:    {a['url']}")
        print(f"  TOPICS: {a.get('topics')}")
        summary = a.get("summary") or "(no summary produced)"
        print("  SUMMARY:")
        for line in summary.splitlines() or [summary]:
            print(f"    {line}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
