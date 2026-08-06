"""Google News RSS search — keyword discovery, no API key (spec BF-01).

Queries ``news.google.com/rss/search?q=<keyword>`` and parses the resulting RSS
with feedparser. Google News item links are opaque ``news.google.com`` redirect
shells, not publisher URLs, so :func:`collect_googlenews` resolves them to the
true publisher URL (see :mod:`sentinel.collect.resolve`) *before* handing them to
processing — otherwise full-text extraction is blocked by robots.txt and both
dedup layers would key off the redirect URL.

Manual smoke test:
    python -m sentinel.collect.googlenews
"""

from __future__ import annotations

from urllib.parse import quote_plus

import feedparser

from ..config import load_settings
from ..logging_conf import get_logger, setup_logging
from .base import graceful, make_article
from .resolve import resolve_articles

logger = get_logger("collect.googlenews")

# hl/gl/ceid pin the edition's language + region. The defaults below reproduce the
# original hardcoded English/US values; a request profile can override them via
# sentinel.request.locale (which resolves {B} language x {C} geographic zone).
SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"

DEFAULT_LOCALE_PARAMS: dict[str, str] = {"hl": "en-US", "gl": "US", "ceid": "US:en"}


def build_search_url(query: str, locale_params: dict[str, str] | None = None) -> str:
    """Build a Google News RSS search URL, optionally for a specific locale."""
    params = locale_params or DEFAULT_LOCALE_PARAMS
    return SEARCH_URL.format(query=quote_plus(query), **params)


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
def collect_googlenews(queries: list[str], *, resolve: bool = True,
                       allow_network: bool = False,
                       locale_params: dict[str, str] | None = None) -> list[dict]:
    """Run each discovery query against Google News RSS. Failures are isolated.

    Item links are ``news.google.com`` redirect shells. When ``resolve`` is true
    (default) they are rewritten in place to the publisher URL *where that is
    possible offline*, so downstream full-text fetch and dedup can key off the real
    article.

    ``allow_network`` defaults to **False** deliberately. Measured against live
    Google News output (Aug 2026): the offline decode resolves 0/8 links and the
    HTTP-follow fallback 0/5 — Google now issues internal-id payloads and answers
    the redirect with a JS interstitial that contains no publisher URL. Enabling
    the network path therefore costs ~0.5 s per article (hundreds of articles per
    run) for no measurable gain. Kept as an opt-in switch in case Google's
    behaviour changes. See :mod:`sentinel.collect.resolve`.
    """
    articles: list[dict] = []
    for query in queries:
        try:
            url = build_search_url(query, locale_params)
            articles.extend(parse_googlenews(url, query=query))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google News query %r failed: %s", query, exc)
    if resolve:
        resolve_articles(articles, allow_network=allow_network)
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
