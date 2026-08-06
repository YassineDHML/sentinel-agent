"""Deep competitive/opportunity analysis — Gemini-ONLY (spec BF-03).

Unlike summarize/classify (token-light, Groq-eligible), this task ingests the
full week's analyzed articles **and** the compressed trend digest in one
context-heavy call, so it stays on Gemini alone — no Groq fallback. If Gemini is
unavailable, the caller (``report/builder.py``) degrades gracefully by omitting
this section rather than routing it to Groq's 12k TPM budget.

**Anti-hallucination is enforced in code, not just in the prompt**: every
executive-summary item, competitive-watch entry, and opportunity must cite at
least one source URL that is actually present in the provided articles.
Citations that don't match a known URL are dropped; items left with zero valid
citations are dropped entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging_conf import get_logger
from .llm import GeminiProvider, LLMClient
from .parsing import extract_json_object

logger = get_logger("analyze.deep_analysis")

# Report language is configurable (spec: sources stay English, the report
# narrative is written for the reader). Extend as new languages are supported.
_LANGUAGE_NAMES = {"en": "English", "fr": "French"}

DEEP_ANALYSIS_INSTRUCTIONS = """\
You are a strategic-intelligence analyst producing a weekly report on {theme} \
for the CEO of {company}. Write the report content in {language}. \
Source article titles/quotes may remain in their original English.

You are given this week's articles (numbered, with title/summary/actor/topics) \
and a compressed trend digest (topic counts vs prior weeks).

Produce:
1. "executive_summary": 3 to 5 of the most important facts of the week.
2. "competitive_watch": one entry per monitored actor that had notable activity
   this week, summarizing what they did.
3. "opportunities": actionable opportunities or implications for {company}, derived
   strictly from the article content and trends provided.

HARD RULE — anti-hallucination: every item in every section MUST include a
"sources" array of one or more article numbers (as given in brackets, e.g. [3])
that it is grounded in. Do NOT state anything that isn't supported by the given
articles or trend digest. If you cannot support a statement with a source
number from this batch, do not include it.

Return ONLY a JSON object of this exact shape:
{{
  "executive_summary": [{{"text": "...", "sources": [1, 2]}}, ...],
  "competitive_watch": [{{"actor": "OpenAI", "highlights": "...", "sources": [1]}}, ...],
  "opportunities": [{{"text": "...", "sources": [3]}}, ...]
}}"""


@dataclass
class CitedItem:
    text: str
    sources: list[str] = field(default_factory=list)  # article URLs


@dataclass
class CompetitiveEntry:
    actor: str
    highlights: str
    sources: list[str] = field(default_factory=list)


@dataclass
class DeepAnalysis:
    executive_summary: list[CitedItem] = field(default_factory=list)
    competitive_watch: list[CompetitiveEntry] = field(default_factory=list)
    opportunities: list[CitedItem] = field(default_factory=list)


def _language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code, code)


DEFAULT_THEME = "the AI SaaS B2B sector"
DEFAULT_COMPANY = "Welyne"


def build_deep_analysis_prompt(
    articles: list[dict],
    trend_digest: str,
    report_language: str = "en",
    *,
    theme: str = DEFAULT_THEME,
    company: str = DEFAULT_COMPANY,
) -> str:
    """Build the single-call deep-analysis prompt for a week's articles.

    ``theme`` and ``company`` default to the historical values so an
    unparameterized run produces the original prompt.
    """
    lines = [
        DEEP_ANALYSIS_INSTRUCTIONS.format(
            language=_language_name(report_language), theme=theme, company=company
        )
    ]
    lines.append("\nTREND DIGEST:\n" + trend_digest)
    lines.append("\nARTICLES:")
    for i, article in enumerate(articles, 1):
        body = article.get("summary") or article.get("snippet") or "(no summary available)"
        lines.append(
            f"[{i}] TITLE: {article.get('title') or '(no title)'}\n"
            f"    ACTOR: {article.get('actor') or 'n/a'}\n"
            f"    TOPICS: {', '.join(article.get('topics') or []) or 'n/a'}\n"
            f"    SUMMARY: {body}"
        )
    return "\n".join(lines)


def _resolve_sources(indices: Any, index_to_url: dict[int, str]) -> list[str]:
    """Map the LLM's article-number citations to real URLs; drop unknown ones."""
    if not isinstance(indices, list):
        return []
    resolved: list[str] = []
    for idx in indices:
        try:
            n = int(idx)
        except (TypeError, ValueError):
            continue
        url = index_to_url.get(n)
        if url and url not in resolved:
            resolved.append(url)
    return resolved


def parse_deep_analysis_response(text: str, articles: list[dict]) -> DeepAnalysis:
    """Parse the deep-analysis JSON, validating every citation against ``articles``.

    Any executive-summary/opportunity item or competitive-watch entry left with
    zero valid citations after resolution is dropped (hard anti-hallucination
    requirement: nothing ungrounded reaches the report).
    """
    index_to_url = {i: a["url"] for i, a in enumerate(articles, 1)}
    data = extract_json_object(text)

    summary: list[CitedItem] = []
    for item in data.get("executive_summary") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        sources = _resolve_sources(item.get("sources"), index_to_url)
        if sources:
            summary.append(CitedItem(text=str(item["text"]).strip(), sources=sources))
        else:
            logger.debug("Dropping uncited executive-summary item: %r", item.get("text"))

    watch: list[CompetitiveEntry] = []
    for item in data.get("competitive_watch") or []:
        if not isinstance(item, dict) or not item.get("actor") or not item.get("highlights"):
            continue
        sources = _resolve_sources(item.get("sources"), index_to_url)
        if sources:
            watch.append(
                CompetitiveEntry(
                    actor=str(item["actor"]).strip(),
                    highlights=str(item["highlights"]).strip(),
                    sources=sources,
                )
            )
        else:
            logger.debug("Dropping uncited competitive-watch entry: %r", item.get("actor"))

    opportunities: list[CitedItem] = []
    for item in data.get("opportunities") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        sources = _resolve_sources(item.get("sources"), index_to_url)
        if sources:
            opportunities.append(CitedItem(text=str(item["text"]).strip(), sources=sources))
        else:
            logger.debug("Dropping uncited opportunity: %r", item.get("text"))

    return DeepAnalysis(executive_summary=summary, competitive_watch=watch, opportunities=opportunities)


def run_deep_analysis(
    articles: list[dict],
    trend_digest: str,
    settings: Any,
    *,
    client: LLMClient | None = None,
) -> DeepAnalysis | None:
    """Run the Gemini-only deep analysis. Returns ``None`` if Gemini fails (no fallback).

    Args:
        articles: This week's analyzed articles (title/summary/actor/topics/url).
        trend_digest: The compressed digest from :func:`~sentinel.analyze.trends.build_trend_digest`.
        settings: Config providing the Gemini model name and report language.
        client: Optional pre-built client (tests inject a fake with no fallback).
    """
    if not articles:
        logger.info("Deep analysis: no articles for this week; skipping.")
        return None

    client = client or LLMClient(GeminiProvider(settings.llm.gemini_model), fallback=None)
    profile = getattr(settings, "profile", None)
    prompt = build_deep_analysis_prompt(
        articles,
        trend_digest,
        settings.app.report_language,
        theme=getattr(profile, "theme", None) or DEFAULT_THEME,
        company=getattr(settings.app, "company", None) or DEFAULT_COMPANY,
    )
    try:
        text = client.generate(prompt)
    except Exception as exc:  # noqa: BLE001 - Gemini-only: no fallback, degrade gracefully
        logger.error("Deep analysis failed (Gemini-only, no fallback): %s", exc)
        return None

    try:
        return parse_deep_analysis_response(text, articles)
    except ValueError as exc:
        logger.error("Deep analysis response unparseable: %s", exc)
        return None
