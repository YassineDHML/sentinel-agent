"""Repositories: typed access to the articles / trends / reports tables.

Each repository wraps a :class:`~sentinel.db.client.SupabaseDB` and exposes a
small, intention-revealing interface. All DB round-trips go through
``db.execute(...)`` so retry/backoff applies uniformly.
"""

from __future__ import annotations

from typing import Any

from ..logging_conf import get_logger
from .client import SupabaseDB

logger = get_logger("db.repositories")

# An article/trend/report row as returned by PostgREST.
Row = dict[str, Any]


def _first(data: list[Row] | None) -> Row | None:
    return data[0] if data else None


class ArticleRepository:
    """CRUD for the ``articles`` table (permanent article memory)."""

    TABLE = "articles"

    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    def insert(self, article: Row) -> Row | None:
        """Insert one article, **idempotent on ``url``**.

        Uses UPSERT with ``ignore_duplicates`` on the ``url`` unique constraint:
        a brand-new URL is inserted; a URL already in the table is a no-op (the
        existing row — including its summary/processed flags — is left untouched).

        Returns:
            The inserted row, or ``None`` if the URL already existed.
        """
        query = self.db.table(self.TABLE).upsert(
            article, on_conflict="url", ignore_duplicates=True
        )
        return _first(self.db.execute(query).data)

    def insert_many(self, articles: list[Row]) -> list[Row]:
        """Bulk insert, idempotent on ``url``. Returns only the newly inserted rows."""
        if not articles:
            return []
        query = self.db.table(self.TABLE).upsert(
            articles, on_conflict="url", ignore_duplicates=True
        )
        inserted = self.db.execute(query).data or []
        logger.info("Inserted %d/%d new article(s).", len(inserted), len(articles))
        return inserted

    def get_by_url(self, url: str) -> Row | None:
        """Return the article with this URL, or ``None`` (used for dedup checks)."""
        query = self.db.table(self.TABLE).select("*").eq("url", url).limit(1)
        return _first(self.db.execute(query).data)

    def list_unprocessed(self, limit: int | None = None) -> list[Row]:
        """Return articles not yet processed (``processed = false``)."""
        query = self.db.table(self.TABLE).select("*").eq("processed", False)
        if limit is not None:
            query = query.limit(limit)
        return self.db.execute(query).data or []


class TrendRepository:
    """CRUD for the ``trends`` table (weekly topic frequencies)."""

    TABLE = "trends"

    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    def upsert(
        self,
        topic: str,
        week: str,
        article_count: int,
        actors: Any | None = None,
    ) -> Row | None:
        """Insert or update the row for ``(topic, week)``.

        Relies on the ``UNIQUE(topic, week)`` constraint in schema.sql: a repeat
        run of the same week updates ``article_count``/``actors`` rather than
        creating a duplicate row.
        """
        row: Row = {
            "topic": topic,
            "week": week,
            "article_count": article_count,
            "actors": actors if actors is not None else [],
        }
        query = self.db.table(self.TABLE).upsert(row, on_conflict="topic,week")
        return _first(self.db.execute(query).data)

    def upsert_many(self, rows: list[Row]) -> list[Row]:
        """Bulk upsert trend rows on ``(topic, week)``."""
        if not rows:
            return []
        query = self.db.table(self.TABLE).upsert(rows, on_conflict="topic,week")
        return self.db.execute(query).data or []

    def get_by_week(self, week: str) -> list[Row]:
        """Return all trend rows for a given ISO week (e.g. ``"2026-W24"``)."""
        query = self.db.table(self.TABLE).select("*").eq("week", week)
        return self.db.execute(query).data or []

    def get_by_topic(self, topic: str) -> list[Row]:
        """Return all weekly rows for one topic (history for acceleration checks)."""
        query = self.db.table(self.TABLE).select("*").eq("topic", topic)
        return self.db.execute(query).data or []


class ReportRepository:
    """CRUD for the ``reports`` table (generated report archive)."""

    TABLE = "reports"

    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    def store(self, week: str, content_html: str) -> Row | None:
        """Archive a generated report for a week. Returns the stored row."""
        row: Row = {"week": week, "content_html": content_html}
        return _first(self.db.execute(self.db.table(self.TABLE).insert(row)).data)

    def get_latest_by_week(self, week: str) -> Row | None:
        """Return the most recently generated report for a week, or ``None``."""
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .eq("week", week)
            .order("generated_at", desc=True)
            .limit(1)
        )
        return _first(self.db.execute(query).data)
