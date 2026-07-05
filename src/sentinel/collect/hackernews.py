"""Hacker News collection via the Algolia HN Search API (spec BF-01).

Official free API, no key, no scraping. Endpoint:
    https://hn.algolia.com/api/v1/search?query=<q>&tags=story

Manual smoke test:
    python -m sentinel.collect.hackernews
"""

from __future__ import annotations

from typing import Any

from ..config import load_settings
from ..logging_conf import get_logger, setup_logging
from .base import graceful, http_get_json, make_article

logger = get_logger("collect.hackernews")

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"


def parse_hn(payload: dict, *, query: str | None = None, actor: str | None = None) -> list[dict]:
    """Normalize an Algolia HN search payload (``{"hits": [...]}``) into articles."""
    articles: list[dict] = []
    for hit in payload.get("hits", []):
        object_id = hit.get("objectID")
        # Ask/Show HN posts have no external URL — fall back to the HN discussion.
        url = hit.get("url") or (ITEM_URL.format(id=object_id) if object_id else None)
        article = make_article(
            url,
            source="Hacker News",
            title=hit.get("title") or hit.get("story_title"),
            actor=actor,
            published_at=hit.get("created_at"),  # ISO 8601 already
            snippet=hit.get("story_text"),
        )
        if article is not None:
            articles.append(article)
    logger.debug("HN query %r: %d article(s).", query, len(articles))
    return articles


def search_hn(query: str, *, hits_per_page: int = 20, tags: str = "story") -> list[dict]:
    """Run one HN search query and return normalized articles."""
    payload = http_get_json(
        SEARCH_URL, params={"query": query, "tags": tags, "hitsPerPage": hits_per_page}
    )
    return parse_hn(payload, query=query)


@graceful("hackernews")
def collect_hackernews(queries: list[str], *, hits_per_page: int = 20) -> list[dict]:
    """Search HN for each discovery query. Per-query failures are isolated."""
    articles: list[dict] = []
    for query in queries:
        try:
            articles.extend(search_hn(query, hits_per_page=hits_per_page))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HN query %r failed: %s", query, exc)
    logger.info("Hacker News: collected %d article(s) for %d query(ies).", len(articles), len(queries))
    return articles


def _main() -> int:
    setup_logging()
    settings = load_settings()
    articles = collect_hackernews(settings.discovery_queries)
    print(f"\nHacker News collected {len(articles)} article(s). First few:\n")
    for a in articles[:5]:
        print(f"  - {a['title']}\n    {a['url']}  ({a['published_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
