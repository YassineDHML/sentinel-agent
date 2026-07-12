"""Generate the weekly HTML report.

    python -m sentinel.report --demo             # synthetic data, no secrets, no DB
    python -m sentinel.report --live [--week W]  # real Supabase data + Gemini

Either mode writes output/<week>.html and prints its path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from ..config import load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from .builder import build_report_context, generate_report, render_report_html

logger = get_logger("report.cli")


def _demo_context(settings):
    """A hand-built context exercising every section, matching the fixtures in
    tests/test_report.py — lets you preview the template with zero live calls."""
    from ..analyze.deep_analysis import CitedItem, CompetitiveEntry, DeepAnalysis
    from ..analyze.trends import TrendStatus

    week = "2026-W28"
    articles = [
        {"url": "https://techcrunch.com/openai-gpt5", "title": "OpenAI ships GPT-5", "source": "TechCrunch"},
        {"url": "https://venturebeat.com/mistral-funding", "title": "Mistral AI raises Series C", "source": "VentureBeat"},
        {"url": "https://blog.langchain.dev/agents-v2", "title": "LangChain launches Agents v2", "source": "LangChain Blog"},
    ]
    deep = DeepAnalysis(
        executive_summary=[
            CitedItem("OpenAI released GPT-5, its new flagship model.", ["https://techcrunch.com/openai-gpt5"]),
            CitedItem("Mistral AI raised a Series C funding round.", ["https://venturebeat.com/mistral-funding"]),
        ],
        competitive_watch=[
            CompetitiveEntry("OpenAI", "Shipped GPT-5 with improved reasoning.", ["https://techcrunch.com/openai-gpt5"]),
            CompetitiveEntry("LangChain", "Released Agents v2 with better tool use.", ["https://blog.langchain.dev/agents-v2"]),
        ],
        opportunities=[
            CitedItem(
                "Evaluate GPT-5 for Welyne's internal tooling given its reasoning gains.",
                ["https://techcrunch.com/openai-gpt5"],
            ),
        ],
    )
    trend_statuses = [
        TrendStatus("AI agents", week, 12, "ACCELERATING", [3, 4, 5, 6], {"OpenAI": 7, "LangChain": 5}),
        TrendStatus("funding and investment", week, 4, "NEW", [0, 0, 0, 0], {"Mistral AI": 4}),
        TrendStatus("pricing change", week, 2, "ONGOING", [2, 2, 2, 2], {"Anthropic": 2}),
    ]
    generated_at = datetime.now(timezone.utc).isoformat()
    return build_report_context(week, settings, articles, trend_statuses, deep, generated_at=generated_at), week


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.report")
    parser.add_argument("--live", action="store_true", help="use real Supabase data + Gemini")
    parser.add_argument("--week", default=None, help="ISO week for --live, e.g. 2026-W28")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()

    if args.live:
        require_secrets(["SUPABASE_URL", "SUPABASE_KEY", "GEMINI_API_KEY"])
        from ..db import ArticleRepository, ReportRepository, SupabaseDB, TrendRepository

        db = SupabaseDB.connect()
        path = generate_report(
            settings,
            ArticleRepository(db),
            TrendRepository(db),
            ReportRepository(db),
            week=args.week,
        )
    else:
        context, week = _demo_context(settings)
        html = render_report_html(context)
        from pathlib import Path

        from .builder import DEFAULT_OUTPUT_DIR

        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = DEFAULT_OUTPUT_DIR / f"{week}.demo.html"
        path.write_text(html, encoding="utf-8")

    print(f"\nReport written to: {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
