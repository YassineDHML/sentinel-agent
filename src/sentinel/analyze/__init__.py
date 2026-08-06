"""Analysis layer: LLM summarize + classify (spec BF-03).

:func:`analyze_articles` runs the two token-light tasks over a list of in-memory
article dicts, attaches ``summary`` + ``topics`` to each, and (when a repository
is given) persists them back to the articles table with ``processed = true``.

Heavier historical trend analysis (Gemini-only) is a later phase and lives
elsewhere; this module is deliberately limited to the batchable light tasks so
the Groq fallback is legitimate.
"""

from __future__ import annotations

from typing import Any

from ..logging_conf import get_logger
from .classify import parse_classify_response
from .deep_analysis import DeepAnalysis, run_deep_analysis
from .llm import GeminiProvider, GroqProvider, LLMClient, LLMError
from .prompts import language_name
from .summarize import parse_summary_response

logger = get_logger("analyze")

__all__ = [
    "LLMClient",
    "GeminiProvider",
    "GroqProvider",
    "LLMError",
    "analyze_articles",
    "parse_summary_response",
    "parse_classify_response",
    "DeepAnalysis",
    "run_deep_analysis",
]


def analyze_articles(
    articles: list[dict],
    settings: Any,
    *,
    client: LLMClient | None = None,
    repo: Any | None = None,
) -> list[dict]:
    """Summarize + classify ``articles``; persist results if ``repo`` is given.

    Args:
        articles: In-memory normalized article dicts (title + snippet/content).
        settings: Config providing model names, batch size, and canonical topics.
        client: Optional pre-built :class:`LLMClient` (tests inject a mocked one).
        repo: Optional ``ArticleRepository``. Articles that got a summary are
            updated with ``summary`` + ``topics`` and marked ``processed = true``.

    Returns:
        The articles with ``summary`` (str | None) and ``topics`` (list[str]) set.
    """
    if not articles:
        return []
    client = client or LLMClient.from_settings(settings)

    # {A} theme and {B} language come from the active request profile, if any.
    # Without a profile these stay None and the prompts keep their original text.
    profile = getattr(settings, "profile", None)
    theme = getattr(profile, "theme", None)
    language = language_name(getattr(settings.app, "report_language", None)) if profile else None

    summaries = client.summarize_batch(articles, theme=theme, language=language)
    topics_map = client.classify_batch(articles, settings.topics, theme=theme)

    analyzed: list[dict] = []
    persisted = 0
    for article in articles:
        url = article["url"]
        enriched = dict(article)
        enriched["summary"] = summaries.get(url)
        enriched["topics"] = topics_map.get(url, [])
        analyzed.append(enriched)

        if repo is not None and enriched["summary"] is not None:
            try:
                repo.mark_analyzed(url, summary=enriched["summary"], topics=enriched["topics"])
                persisted += 1
            except Exception as exc:  # noqa: BLE001 - persistence failure shouldn't abort analysis
                logger.error("Failed to persist analysis for %s: %s", url, exc)

    if repo is not None:
        logger.info("Persisted analysis for %d/%d article(s) (processed=true).", persisted, len(articles))
    return analyzed
