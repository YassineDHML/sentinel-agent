"""Processing layer: relevance filter + deduplication (spec BF-02).

:func:`process_articles` wires the steps together into a clean, deduplicated list
of in-scope articles ready for analysis, and (when a repository is provided)
persists the new ones to the ``articles`` table with ``processed = false``.

Order of operations:
    1. relevance filter        (drop out-of-scope)
    2. exact URL dedup         (within the batch)
    3. title-similarity dedup  (cross-source near-duplicates)
    4. persist new + DB dedup  (idempotent upsert; existing URLs are skipped, so
                                only genuinely new articles are returned)

Manual demo (live collect, dry-run — no DB writes, no secrets):
    python -m sentinel.process
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..logging_conf import get_logger, setup_logging
from .dedup import dedup_by_title, dedup_by_url, normalize_title, title_similarity
from .filter import RelevanceFilter

logger = get_logger("process")

__all__ = [
    "RelevanceFilter",
    "dedup_by_url",
    "dedup_by_title",
    "normalize_title",
    "title_similarity",
    "process_articles",
    "ProcessResult",
]

# Columns on the articles table (schema.sql) that map directly from a normalized
# article dict. `snippet` is stored always (cheap fallback text); `content` is
# usually None at collection time and filled in lazily when full text is fetched.
_DB_COLUMNS = ("url", "title", "source", "actor", "published_at", "collected_at", "snippet", "content")


@dataclass
class ProcessResult:
    """Counts at each stage plus the clean article list ready for analysis."""

    collected: int
    after_filter: int
    after_url_dedup: int
    after_title_dedup: int
    persisted_new: int
    articles: list[dict]
    duplicate_groups: list[list[dict]] = field(default_factory=list)
    persisted: bool = False  # True if a DB repository was used

    def summary_lines(self) -> list[str]:
        new_label = (
            f"persisted new (processed=false) : {self.persisted_new}"
            if self.persisted
            else "persisted new                   : (dry-run, DB skipped)"
        )
        return [
            f"collected (input)               : {self.collected}",
            f"after relevance filter          : {self.after_filter}",
            f"after exact URL dedup           : {self.after_url_dedup}",
            f"after title-similarity dedup    : {self.after_title_dedup}",
            new_label,
            f"cross-source duplicate groups   : {len(self.duplicate_groups)}",
        ]


def _iso_or_none(value: Any) -> str | None:
    """Return ``value`` only if it's an ISO-8601 timestamp Postgres will accept."""
    if not value or not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None  # e.g. a raw RFC-822 date string -> let the DB default stand


def _to_db_row(article: dict) -> dict:
    """Project a normalized article dict onto the articles-table columns."""
    row = {col: article.get(col) for col in _DB_COLUMNS}
    row["published_at"] = _iso_or_none(row.get("published_at"))
    row["collected_at"] = _iso_or_none(row.get("collected_at"))
    row["processed"] = False
    return row


def process_articles(
    articles: list[dict],
    settings: Any,
    *,
    repo: Any | None = None,
    threshold: float | None = None,
) -> ProcessResult:
    """Filter + dedup ``articles``; persist new ones if ``repo`` is given.

    Args:
        articles: Raw normalized article dicts from the collection layer.
        settings: A :class:`~sentinel.config.Settings` (keywords/actors/threshold).
        repo: Optional ``ArticleRepository``. If provided, new articles are
            upserted (idempotent on URL) and only DB-new ones are returned —
            this is the exact-dedup-against-DB step. If ``None`` (dry-run), no DB
            access happens and all title-deduped articles are returned.
        threshold: Override the configured title-similarity threshold.

    Returns:
        A :class:`ProcessResult` with per-stage counts and the clean article list.
    """
    thr = threshold if threshold is not None else settings.process.title_similarity_threshold

    filtered = RelevanceFilter.from_settings(settings).filter(articles)
    url_deduped = dedup_by_url(filtered)
    title_deduped, duplicate_groups = dedup_by_title(url_deduped, thr)

    if repo is not None:
        inserted = repo.insert_many([_to_db_row(a) for a in title_deduped])
        new_urls = {r.get("url") for r in inserted}
        new_articles = [a for a in title_deduped if a["url"] in new_urls]
        logger.info(
            "Persisted %d new article(s); %d already in DB (skipped).",
            len(new_articles),
            len(title_deduped) - len(new_articles),
        )
        persisted_new = len(new_articles)
        persisted = True
    else:
        new_articles = title_deduped
        persisted_new = 0
        persisted = False

    return ProcessResult(
        collected=len(articles),
        after_filter=len(filtered),
        after_url_dedup=len(url_deduped),
        after_title_dedup=len(title_deduped),
        persisted_new=persisted_new,
        articles=new_articles,
        duplicate_groups=duplicate_groups,
        persisted=persisted,
    )


def _main() -> int:
    """Live demo: collect from key-free sources, run filter+dedup dry-run, print counts."""
    setup_logging()
    from ..config import load_settings
    from ..collect.googlenews import collect_googlenews
    from ..collect.hackernews import collect_hackernews
    from ..collect.rss import collect_rss

    settings = load_settings()
    raw: list[dict] = []
    raw += collect_rss(settings.feeds)
    raw += collect_hackernews(settings.discovery_queries)
    raw += collect_googlenews(settings.discovery_queries)

    result = process_articles(raw, settings, repo=None)  # dry-run

    print("\n=== Processing (before/after) ===")
    for line in result.summary_lines():
        print("  " + line)

    if result.duplicate_groups:
        print("\n  Example cross-source duplicate group(s):")
        for group in result.duplicate_groups[:3]:
            print(f"    - {len(group)} articles merged:")
            for a in group:
                print(f"        [{a.get('source')}] {a.get('title')}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
