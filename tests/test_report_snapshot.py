"""Golden-file regression guard for the weekly report.

The weekly watch is in production. Every later phase (parameterization, new report
types, i18n headings) risks silently changing its rendered output. This test
renders a FIXED fixture context and compares byte-for-byte (line endings
normalized) against a committed golden file.

If it fails, either the change was unintended — or it was intended, in which case
regenerate deliberately:

    python -m tests.regen_report_golden       # see REGEN_HINT below
"""

from __future__ import annotations

from pathlib import Path

from sentinel.analyze.deep_analysis import CitedItem, CompetitiveEntry, DeepAnalysis
from sentinel.analyze.trends import TrendStatus
from sentinel.config import load_settings
from sentinel.report.builder import build_report_context, render_report_html

GOLDEN = Path(__file__).parent / "fixtures" / "report_golden.html"

REGEN_HINT = (
    "Weekly report output changed. If this was INTENTIONAL, regenerate the golden file:\n"
    "  python -c \"from tests.test_report_snapshot import write_golden; write_golden()\"\n"
    "and review the diff carefully before committing."
)

# A fixed, fully-populated context — never derived from live data.
WEEK = "2026-W29"
GENERATED_AT = "2026-07-20T06:00:00+00:00"

ARTICLES = [
    {
        "url": "https://techcrunch.com/openai-gpt5",
        "title": "OpenAI ships GPT-5",
        "source": "TechCrunch",
        "summary": "OpenAI released GPT-5.",
    },
    {
        "url": "https://venturebeat.com/mistral-series-c",
        "title": "Mistral AI raises Series C",
        "source": "VentureBeat",
        "summary": "Mistral closed a Series C round.",
    },
]

DEEP = DeepAnalysis(
    executive_summary=[
        CitedItem("OpenAI released GPT-5, its new flagship model.", ["https://techcrunch.com/openai-gpt5"]),
        CitedItem("Mistral AI raised a Series C round.", ["https://venturebeat.com/mistral-series-c"]),
    ],
    competitive_watch=[
        CompetitiveEntry("OpenAI", "Shipped GPT-5 with improved reasoning.", ["https://techcrunch.com/openai-gpt5"]),
    ],
    opportunities=[
        CitedItem("Evaluate GPT-5 for internal tooling.", ["https://techcrunch.com/openai-gpt5"]),
    ],
)

TRENDS = [
    TrendStatus("AI agents", WEEK, 12, "ACCELERATING", [3, 4, 5, 6], {"OpenAI": 7}),
    TrendStatus("funding and investment", WEEK, 4, "NEW", [0, 0, 0, 0], {"Mistral AI": 4}),
    TrendStatus("pricing change", WEEK, 2, "ONGOING", [2, 2, 2, 2], {"Anthropic": 2}),
]


def render_fixture_report() -> str:
    """Render the fixed fixture context through the real report pipeline."""
    settings = load_settings(load_env=False)
    context = build_report_context(
        WEEK, settings, ARTICLES, TRENDS, DEEP, generated_at=GENERATED_AT
    )
    return render_report_html(context)


def _normalize(html: str) -> str:
    """Normalize line endings so the test is stable across platforms (CRLF/LF)."""
    return html.replace("\r\n", "\n").replace("\r", "\n").strip()


def write_golden() -> Path:
    """(Re)generate the golden file. Run deliberately, then review the diff."""
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(_normalize(render_fixture_report()) + "\n", encoding="utf-8")
    return GOLDEN


def test_weekly_report_matches_golden():
    """The weekly report's rendered HTML must not change unintentionally."""
    assert GOLDEN.exists(), f"golden file missing; create it with write_golden(). {REGEN_HINT}"
    expected = _normalize(GOLDEN.read_text(encoding="utf-8"))
    actual = _normalize(render_fixture_report())
    assert actual == expected, REGEN_HINT


def test_golden_covers_all_five_sections():
    """Guard the guard: a golden file that lost a section would hide regressions."""
    html = _normalize(GOLDEN.read_text(encoding="utf-8"))
    for heading in (
        "1. Executive Summary",
        "2. Competitive Watch",
        "3. Trends Detected",
        "4. Opportunities",
        "5. Sources Consulted",
    ):
        assert heading in html, f"golden file is missing section {heading!r}"
