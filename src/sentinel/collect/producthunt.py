"""Product Hunt collection via the official GraphQL API v2 (spec BF-01).

Requires a developer token (``PRODUCTHUNT_TOKEN``). If the token is absent the
collector degrades gracefully (logs + returns []), so the pipeline still runs.

FLAG (verify against current PH API): endpoint URL, the Bearer-token auth scheme,
and the exact GraphQL schema fields are kept here behind this thin wrapper; if PH
changes them, this is the only file to touch.

Manual smoke test (needs PRODUCTHUNT_TOKEN in .env):
    python -m sentinel.collect.producthunt
"""

from __future__ import annotations

from typing import Any

from ..config import get_secret
from ..logging_conf import get_logger, setup_logging
from .base import graceful, http_post_json, make_article

logger = get_logger("collect.producthunt")

API_URL = "https://api.producthunt.com/v2/api/graphql"

# Newest posts with the fields we need. `first` caps the page size.
POSTS_QUERY = """
query RecentPosts($first: Int!) {
  posts(order: NEWEST, first: $first) {
    edges {
      node { id name tagline url website createdAt }
    }
  }
}
"""


def parse_producthunt(payload: dict) -> list[dict]:
    """Normalize a Product Hunt GraphQL response into article dicts."""
    edges = (
        payload.get("data", {})
        .get("posts", {})
        .get("edges", [])
        if isinstance(payload.get("data"), dict)
        else []
    )
    articles: list[dict] = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        article = make_article(
            node.get("website") or node.get("url"),
            source="Product Hunt",
            title=node.get("name"),
            published_at=node.get("createdAt"),
            snippet=node.get("tagline"),
        )
        if article is not None:
            articles.append(article)
    logger.debug("Product Hunt: %d article(s).", len(articles))
    return articles


@graceful("producthunt")
def collect_producthunt(*, token: str | None = None, first: int = 20) -> list[dict]:
    """Fetch newest Product Hunt posts. Returns [] if no token is configured."""
    token = token or get_secret("PRODUCTHUNT_TOKEN", required=False)
    if not token:
        logger.info("Product Hunt: no PRODUCTHUNT_TOKEN set; skipping.")
        return []
    headers = {"Authorization": f"Bearer {token}"}
    payload = http_post_json(
        API_URL, {"query": POSTS_QUERY, "variables": {"first": first}}, headers=headers
    )
    if payload.get("errors"):
        logger.warning("Product Hunt API returned errors: %s", payload["errors"])
    return parse_producthunt(payload)


def _main() -> int:
    setup_logging()
    articles = collect_producthunt()
    print(f"\nProduct Hunt collected {len(articles)} article(s). First few:\n")
    for a in articles[:5]:
        print(f"  - {a['title']}: {a['snippet']}\n    {a['url']}  ({a['published_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
