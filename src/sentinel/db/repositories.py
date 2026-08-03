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

    def list_by_week(self, start_iso: str, end_iso: str) -> list[Row]:
        """Return articles collected within ``[start_iso, end_iso)`` (a week's span).

        Callers derive the ISO datetime bounds (e.g. from
        :func:`sentinel.analyze.trends.iso_week_of`'s Monday-of-week helper) since
        the ``articles`` table has no ``week`` column of its own.
        """
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .gte("collected_at", start_iso)
            .lt("collected_at", end_iso)
        )
        return self.db.execute(query).data or []

    def set_content(self, url: str, content: str) -> Row | None:
        """Cache fetched full-text ``content`` for an article, so it isn't re-fetched."""
        query = self.db.table(self.TABLE).update({"content": content}).eq("url", url)
        return _first(self.db.execute(query).data)

    def mark_analyzed(self, url: str, *, summary: str, topics: list[str]) -> Row | None:
        """Persist the LLM summary + canonical topics and set ``processed = true``."""
        query = (
            self.db.table(self.TABLE)
            .update({"summary": summary, "topics": topics, "processed": True})
            .eq("url", url)
        )
        return _first(self.db.execute(query).data)


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

    def store(
        self,
        week: str,
        content_html: str,
        *,
        report_type: str = "weekly",
        request_slug: str | None = None,
        language: str | None = None,
        period_kind: str | None = None,
        period_key: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        title: str | None = None,
        word_count: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> Row | None:
        """Archive a generated report. Returns the stored row.

        Everything past ``content_html`` is keyword-only and optional: called with
        two positional arguments it behaves exactly as before (a weekly report).
        The extra fields let parameterized/typed reports coexist in one table.
        """
        row: Row = {"week": week, "content_html": content_html, "report_type": report_type}
        optional = {
            "request_slug": request_slug,
            "language": language,
            "period_kind": period_kind,
            "period_key": period_key,
            "period_start": period_start,
            "period_end": period_end,
            "title": title,
            "word_count": word_count,
            "params": params,
        }
        row.update({k: v for k, v in optional.items() if v is not None})
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

    def list_by_type(self, report_type: str, *, limit: int = 10) -> list[Row]:
        """Return the most recent reports of a given type, newest first."""
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .eq("report_type", report_type)
            .order("generated_at", desc=True)
            .limit(limit)
        )
        return self.db.execute(query).data or []

    def get_latest_for_request(self, request_slug: str, period_key: str) -> Row | None:
        """Return the latest report for a request + period, or ``None``."""
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .eq("request_slug", request_slug)
            .eq("period_key", period_key)
            .order("generated_at", desc=True)
            .limit(1)
        )
        return _first(self.db.execute(query).data)


class ReportSourceRepository:
    """CRUD for the ``report_sources`` table (the citation ledger).

    Every source a generated report actually cites gets a row here, tagged with its
    evidence tier, making the anti-hallucination guarantee auditable after the fact.
    """

    TABLE = "report_sources"

    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    def add_many(self, report_id: int, sources: list[Row]) -> list[Row]:
        """Bulk-insert citation rows for a report. Returns the inserted rows."""
        if not sources:
            return []
        rows = [{**s, "report_id": report_id} for s in sources]
        inserted = self.db.execute(self.db.table(self.TABLE).insert(rows)).data or []
        logger.info("Recorded %d cited source(s) for report %s.", len(inserted), report_id)
        return inserted

    def list_for_report(self, report_id: int) -> list[Row]:
        """Return every cited source for a report."""
        query = self.db.table(self.TABLE).select("*").eq("report_id", report_id)
        return self.db.execute(query).data or []
