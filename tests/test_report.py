"""Tests for report assembly + rendering. Pure template rendering from fixture
data — no LLM call, no DB. generate_report's orchestration is tested with
mocked repos + a fake LLM client.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sentinel.analyze.deep_analysis import CitedItem, CompetitiveEntry, DeepAnalysis
from sentinel.analyze.trends import TrendStatus
from sentinel.config import load_settings
from sentinel.report.builder import build_report_context, generate_report, render_report_html

WEEK = "2026-W28"

ARTICLES = [
    {"url": "https://techcrunch.com/gpt5", "title": "OpenAI ships GPT-5", "source": "TechCrunch",
     "summary": "OpenAI released GPT-5."},
    {"url": "https://venturebeat.com/mistral", "title": "Mistral raises funding", "source": "VentureBeat",
     "summary": "Mistral raised a round."},
]

DEEP_ANALYSIS = DeepAnalysis(
    executive_summary=[CitedItem("OpenAI shipped GPT-5.", ["https://techcrunch.com/gpt5"])],
    competitive_watch=[
        CompetitiveEntry("OpenAI", "Shipped GPT-5.", ["https://techcrunch.com/gpt5"]),
    ],
    opportunities=[CitedItem("Evaluate GPT-5 internally.", ["https://techcrunch.com/gpt5"])],
)

TREND_STATUSES = [
    TrendStatus("AI agents", WEEK, 12, "ACCELERATING", [3, 4, 5, 6], {"OpenAI": 7}),
    TrendStatus("funding and investment", WEEK, 4, "NEW", [0, 0, 0, 0], {"Mistral AI": 4}),
    TrendStatus("pricing change", WEEK, 2, "ONGOING", [2, 2, 2, 2], {"Anthropic": 2}),
]


def _settings():
    return load_settings(load_env=False)


# --------------------------------------------------------------------------- #
# build_report_context (pure)
# --------------------------------------------------------------------------- #
def test_build_report_context_resolves_citations_to_titles():
    ctx = build_report_context(
        WEEK, _settings(), ARTICLES, TREND_STATUSES, DEEP_ANALYSIS, generated_at="2026-07-06T00:00:00+00:00"
    )
    assert ctx["week"] == WEEK
    assert ctx["executive_summary"][0]["sources"][0]["title"] == "OpenAI ships GPT-5"
    assert ctx["competitive_watch"][0]["actor"] == "OpenAI"
    assert len(ctx["sources_consulted"]) == 2


def test_build_report_context_buckets_trends_by_status():
    ctx = build_report_context(WEEK, _settings(), ARTICLES, TREND_STATUSES, DEEP_ANALYSIS, generated_at="x")
    assert [t["topic"] for t in ctx["trends"]["accelerating"]] == ["AI agents"]
    assert [t["topic"] for t in ctx["trends"]["new"]] == ["funding and investment"]
    assert [t["topic"] for t in ctx["trends"]["ongoing"]] == ["pricing change"]


def test_build_report_context_handles_none_deep_analysis_gracefully():
    ctx = build_report_context(WEEK, _settings(), ARTICLES, TREND_STATUSES, None, generated_at="x")
    assert ctx["executive_summary"] == []
    assert ctx["competitive_watch"] == []
    assert ctx["opportunities"] == []
    assert len(ctx["sources_consulted"]) == 2  # sources section is independent of the LLM


def test_build_report_context_caps_sources_with_overflow():
    from sentinel.report.builder import MAX_SOURCES_LISTED

    many = [
        {"url": f"https://x/{i}", "title": f"Article {i}", "source": "RSS", "summary": "s"}
        for i in range(MAX_SOURCES_LISTED + 25)
    ]
    ctx = build_report_context(WEEK, _settings(), many, [], None, generated_at="x")
    assert len(ctx["sources_consulted"]) == MAX_SOURCES_LISTED
    assert ctx["sources_overflow"] == 25


# --------------------------------------------------------------------------- #
# render_report_html (fixture data, no LLM call)
# --------------------------------------------------------------------------- #
def test_render_report_html_contains_all_five_sections():
    ctx = build_report_context(
        WEEK, _settings(), ARTICLES, TREND_STATUSES, DEEP_ANALYSIS, generated_at="2026-07-06T00:00:00+00:00"
    )
    html = render_report_html(ctx)

    assert "1. Executive Summary" in html
    assert "2. Competitive Watch" in html
    assert "3. Trends Detected" in html
    assert "4. Opportunities for Welyne" in html
    assert "5. Sources Consulted" in html

    assert "OpenAI shipped GPT-5." in html
    assert "AI agents" in html and "12 article" in html
    assert "Evaluate GPT-5 internally." in html
    assert 'href="https://techcrunch.com/gpt5"' in html
    assert WEEK in html


def test_render_report_html_escapes_untrusted_text():
    malicious = DeepAnalysis(
        executive_summary=[CitedItem("<script>alert(1)</script>", ["https://techcrunch.com/gpt5"])]
    )
    ctx = build_report_context(WEEK, _settings(), ARTICLES, [], malicious, generated_at="x")
    html = render_report_html(ctx)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_report_html_empty_sections_show_fallback_text():
    ctx = build_report_context(WEEK, _settings(), [], [], None, generated_at="x")
    html = render_report_html(ctx)
    assert "No executive summary available" in html
    assert "No sources recorded" in html


# --------------------------------------------------------------------------- #
# generate_report orchestration (mocked repos + fake LLM client)
# --------------------------------------------------------------------------- #
def test_generate_report_writes_local_file_and_persists(tmp_path):
    article_repo = MagicMock()
    article_repo.list_by_week.return_value = ARTICLES
    trend_repo = MagicMock()
    trend_repo.get_by_week.return_value = [
        {"topic": "AI agents", "week": WEEK, "article_count": 12, "actors": {"OpenAI": 7}}
    ]
    report_repo = MagicMock()

    fake_client = MagicMock()
    fake_client.generate.return_value = (
        '{"executive_summary": [{"text": "ok", "sources": [1]}], '
        '"competitive_watch": [], "opportunities": []}'
    )

    path = generate_report(
        _settings(), article_repo, trend_repo, report_repo,
        week=WEEK, client=fake_client, output_dir=tmp_path,
        generated_at="2026-07-06T00:00:00+00:00",
    )

    assert path == tmp_path / f"{WEEK}.html"
    assert path.exists()
    html = path.read_text(encoding="utf-8")
    assert "ok" in html
    report_repo.store.assert_called_once()
    stored_week, stored_html = report_repo.store.call_args[0]
    assert stored_week == WEEK
    assert stored_html == html


def test_generate_report_lists_only_analyzed_articles(tmp_path):
    # 2 analyzed (with summary) + 3 collected-but-unanalyzed -> only the 2 appear
    mixed = ARTICLES + [
        {"url": f"https://x/{i}", "title": f"Unanalyzed {i}", "source": "RSS"} for i in range(3)
    ]
    article_repo = MagicMock()
    article_repo.list_by_week.return_value = mixed
    trend_repo = MagicMock()
    trend_repo.get_by_week.return_value = []
    report_repo = MagicMock()
    fake_client = MagicMock()
    fake_client.generate.return_value = (
        '{"executive_summary": [], "competitive_watch": [], "opportunities": []}'
    )

    path = generate_report(
        _settings(), article_repo, trend_repo, report_repo,
        week=WEEK, client=fake_client, output_dir=tmp_path, generated_at="x",
    )
    html = path.read_text(encoding="utf-8")
    assert "Unanalyzed" not in html          # collected-but-unanalyzed excluded
    assert "OpenAI ships GPT-5" in html       # analyzed article listed as a source


def test_generate_report_survives_db_persist_failure(tmp_path):
    article_repo = MagicMock()
    article_repo.list_by_week.return_value = []
    trend_repo = MagicMock()
    trend_repo.get_by_week.return_value = []
    report_repo = MagicMock()
    report_repo.store.side_effect = RuntimeError("DB unreachable")

    path = generate_report(
        _settings(), article_repo, trend_repo, report_repo,
        week=WEEK, client=MagicMock(), output_dir=tmp_path,
        generated_at="x",
    )
    assert path.exists()  # local copy still written despite DB failure
