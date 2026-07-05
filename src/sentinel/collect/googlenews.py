"""Google News RSS search — keyword discovery, no API key (spec BF-01).

Queries ``news.google.com/rss/search?q=<keyword>`` and parses the resulting RSS
with feedparser. Google News item links are redirect URLs; that's fine — the
full-text fetcher resolves them later for articles that pass the relevance filter.

Manual smoke test:
    python -m sentinel.collect.googlenews
"""

from __future__ import annotations

from urllib.parse import quote_plus

import feedparser

from ..config import load_settings
from ..logging_conf import get_logger, setup_logging
from .base import graceful, make_article

logger = get_logger("collect.googlenews")

# hl/gl/ceid pin language + region to English so results match the MVP scope (§10).
SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def build_search_url(query: str) -> str:
    return SEARCH_URL.format(query=quote_plus(query))


def parse_googlenews(source: object, *, query: str | None = None) -> list[dict]:
    """Parse a Google News RSS document (URL or raw XML) into article dicts."""
    parsed = feedparser.parse(source)
    if parsed.bozo and not parsed.entries:
        logger.warning("Google News feed for %r unparseable: %s", query, parsed.get("bozo_exception"))
        return []

    articles: list[dict] = []
    for entry in parsed.entries:
        # Google News sets entry.source.title to the originating outlet.
        outlet = None
        src = entry.get("source")
        if isinstance(src, dict):
            outlet = src.get("title")
        article = make_article(
            entry.get("link"),
            source=f"Google News ({outlet})" if outlet else "Google News",
            title=entry.get("title"),
            published_at=entry.get("published_parsed") or entry.get("published"),
            snippet=entry.get("summary"),
        )
        if article is not None:
            articles.append(article)
    logger.debug("Google News query %r: %d article(s).", query, len(articles))
    return articles


@graceful("googlenews")
def collect_googlenews(queries: list[str]) -> list[dict]:
    """Run each discovery query against Google News RSS. Failures are isolated."""
    articles: list[dict] = []
    for query in queries:
        try:
            articles.extend(parse_googlenews(build_search_url(query), query=query))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google News query %r failed: %s", query, exc)
    logger.info("Google News: collected %d article(s) for %d query(ies).", len(articles), len(queries))
    return articles


def _main() -> int:
    setup_logging()
    settings = load_settings()
    articles = collect_googlenews(settings.discovery_queries)
    print(f"\nGoogle News collected {len(articles)} article(s). First few:\n")
    for a in articles[:5]:
        print(f"  - [{a['source']}] {a['title']}\n    {a['url']}  ({a['published_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
