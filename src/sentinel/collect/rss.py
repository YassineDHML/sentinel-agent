"""RSS/Atom collection via feedparser (spec BF-01).

Reads the feed list from config (tech media + official actor blogs) and returns
normalized article dicts. feedparser handles both fetching-by-URL and parsing raw
content, so :func:`parse_feed` can be unit-tested directly on a fixture string.

Manual smoke test (hits the real feeds in config.yaml):
    python -m sentinel.collect.rss
"""

from __future__ import annotations

from typing import Any

import feedparser

from ..config import Feed, load_settings
from ..logging_conf import get_logger, setup_logging
from .base import graceful, make_article

logger = get_logger("collect.rss")


def parse_feed(source: Any, *, name: str, actor: str | None = None) -> list[dict]:
    """Parse one feed (a URL or raw XML string) into normalized articles.

    Args:
        source: A feed URL or a raw feed document (feedparser accepts both).
        name: Human-readable source name (e.g. "TechCrunch").
        actor: Optional actor attribution for a company's own blog.
    """
    parsed = feedparser.parse(source)
    if parsed.bozo and not parsed.entries:
        # Malformed and unusable — log the cause and yield nothing for this feed.
        logger.warning("Feed '%s' could not be parsed: %s", name, parsed.get("bozo_exception"))
        return []

    articles: list[dict] = []
    for entry in parsed.entries:
        article = make_article(
            entry.get("link"),
            source=name,
            title=entry.get("title"),
            actor=actor,
            published_at=entry.get("published_parsed") or entry.get("updated_parsed")
            or entry.get("published") or entry.get("updated"),
            snippet=entry.get("summary") or entry.get("description"),
        )
        if article is not None:
            articles.append(article)
    logger.debug("Feed '%s': %d article(s).", name, len(articles))
    return articles


@graceful("rss")
def collect_rss(feeds: list[Feed]) -> list[dict]:
    """Collect from every configured feed. Per-feed failures are isolated."""
    articles: list[dict] = []
    for feed in feeds:
        try:
            articles.extend(parse_feed(feed.url, name=feed.name, actor=feed.actor))
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the rest
            logger.warning("Feed '%s' (%s) failed: %s", feed.name, feed.url, exc)
    logger.info("RSS: collected %d article(s) from %d feed(s).", len(articles), len(feeds))
    return articles


def _main() -> int:
    setup_logging()
    settings = load_settings()
    articles = collect_rss(settings.feeds)
    print(f"\nRSS collected {len(articles)} article(s). First few:\n")
    for a in articles[:5]:
        print(f"  - [{a['source']}] {a['title']}\n    {a['url']}  ({a['published_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
