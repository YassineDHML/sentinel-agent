"""GNews API — keyword DISCOVERY layer (spec BF-01).

Free-tier constraints (personal / non-commercial): 100 requests/day, 1 req/sec,
**10 articles per request, truncated snippets only**. Because of this, GNews is a
*discovery* source: it yields candidate URLs + snippets, never final content.
Full text is fetched separately (``fulltext.py``) for articles that pass the
relevance filter. ``content`` is deliberately left ``None`` here.

Requires ``GNEWS_API_KEY``; degrades gracefully (logs + []) if absent.

Manual smoke test (needs GNEWS_API_KEY in .env):
    python -m sentinel.collect.gnews
"""

from __future__ import annotations

import time

from ..config import get_secret, load_settings
from ..logging_conf import get_logger, setup_logging
from .base import graceful, http_get_json, make_article

logger = get_logger("collect.gnews")

SEARCH_URL = "https://gnews.io/api/v4/search"
MAX_PER_REQUEST = 10        # free-tier hard cap
REQUEST_INTERVAL = 1.1      # seconds between calls (free tier: 1 req/sec)


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so tests can patch it out."""
    time.sleep(seconds)


def parse_gnews(payload: dict, *, query: str | None = None) -> list[dict]:
    """Normalize a GNews search payload into discovery candidates (no content)."""
    articles: list[dict] = []
    for item in payload.get("articles", []):
        src = item.get("source") or {}
        outlet = src.get("name") if isinstance(src, dict) else None
        article = make_article(
            item.get("url"),
            source=f"GNews ({outlet})" if outlet else "GNews",
            title=item.get("title"),
            published_at=item.get("publishedAt"),
            snippet=item.get("description"),  # truncated snippet only
            content=None,                     # discovery layer — full text fetched later
        )
        if article is not None:
            articles.append(article)
    logger.debug("GNews query %r: %d candidate(s).", query, len(articles))
    return articles


def search_gnews(query: str, *, api_key: str, max_articles: int = MAX_PER_REQUEST,
                 lang: str = "en") -> list[dict]:
    """Run one GNews query. ``max_articles`` is clamped to the free-tier cap of 10."""
    payload = http_get_json(
        SEARCH_URL,
        params={
            "q": query,
            "max": min(max_articles, MAX_PER_REQUEST),
            "lang": lang,
            "apikey": api_key,
        },
    )
    return parse_gnews(payload, query=query)


@graceful("gnews")
def collect_gnews(queries: list[str], *, api_key: str | None = None,
                  max_articles: int = MAX_PER_REQUEST) -> list[dict]:
    """Discover candidates for each query, respecting the 1 req/sec rate limit.

    Returns [] (with a log) if no API key is configured.
    """
    api_key = api_key or get_secret("GNEWS_API_KEY", required=False)
    if not api_key:
        logger.info("GNews: no GNEWS_API_KEY set; skipping discovery.")
        return []

    articles: list[dict] = []
    for i, query in enumerate(queries):
        if i > 0:
            _sleep(REQUEST_INTERVAL)  # stay under 1 req/sec
        try:
            articles.extend(search_gnews(query, api_key=api_key, max_articles=max_articles))
        except Exception as exc:  # noqa: BLE001
            logger.warning("GNews query %r failed: %s", query, exc)
    logger.info("GNews: collected %d candidate(s) for %d query(ies).", len(articles), len(queries))
    return articles


def _main() -> int:
    setup_logging()
    settings = load_settings()
    articles = collect_gnews(settings.discovery_queries)
    print(f"\nGNews discovered {len(articles)} candidate(s). First few:\n")
    for a in articles[:5]:
        print(f"  - [{a['source']}] {a['title']}\n    {a['url']}  ({a['published_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
