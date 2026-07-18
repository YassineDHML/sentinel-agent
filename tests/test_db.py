"""Tests for the persistence layer. The Supabase client is fully mocked —
no live connection, no secrets, no `supabase` package required at runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sentinel.db import client as client_module
from sentinel.db.client import SupabaseDB
from sentinel.db.repositories import (
    ArticleRepository,
    ReportRepository,
    TrendRepository,
)


def _resp(data):
    """Mimic the SDK's APIResponse (has .data and .count)."""
    return SimpleNamespace(data=data, count=None)


def _db_with_mock_client():
    client = MagicMock(name="supabase_client")
    # base_delay=0 keeps any accidental retry instantaneous
    return SupabaseDB(client, attempts=3, base_delay=0.0), client


# --------------------------------------------------------------------------- #
# ArticleRepository
# --------------------------------------------------------------------------- #
def test_article_insert_is_idempotent_upsert_on_url():
    db, client = _db_with_mock_client()
    client.table.return_value.upsert.return_value.execute.return_value = _resp(
        [{"id": 1, "url": "https://a.com/x"}]
    )
    repo = ArticleRepository(db)

    row = repo.insert({"url": "https://a.com/x", "title": "X"})

    assert row == {"id": 1, "url": "https://a.com/x"}
    client.table.assert_called_with("articles")
    _, kwargs = client.table.return_value.upsert.call_args
    assert kwargs["on_conflict"] == "url"
    assert kwargs["ignore_duplicates"] is True


def test_article_insert_duplicate_url_returns_none():
    db, client = _db_with_mock_client()
    # ignore_duplicates => no row returned when the URL already exists
    client.table.return_value.upsert.return_value.execute.return_value = _resp([])
    repo = ArticleRepository(db)

    assert repo.insert({"url": "https://a.com/dup"}) is None


def test_article_get_by_url():
    db, client = _db_with_mock_client()
    (
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value
    ) = _resp([{"id": 5, "url": "https://a.com/y"}])
    repo = ArticleRepository(db)

    row = repo.get_by_url("https://a.com/y")

    assert row["id"] == 5
    client.table.return_value.select.return_value.eq.assert_called_with("url", "https://a.com/y")


def test_article_insert_many_reports_only_new_rows():
    db, client = _db_with_mock_client()
    client.table.return_value.upsert.return_value.execute.return_value = _resp(
        [{"id": 1}, {"id": 2}]
    )
    repo = ArticleRepository(db)

    inserted = repo.insert_many([{"url": "u1"}, {"url": "u2"}, {"url": "u3"}])
    assert len(inserted) == 2


def test_article_set_content_updates_by_url():
    db, client = _db_with_mock_client()
    client.table.return_value.update.return_value.eq.return_value.execute.return_value = _resp(
        [{"url": "https://a.com/x", "content": "full text"}]
    )
    repo = ArticleRepository(db)

    row = repo.set_content("https://a.com/x", "full text")
    assert row["content"] == "full text"
    args, _ = client.table.return_value.update.call_args
    assert args[0] == {"content": "full text"}
    client.table.return_value.update.return_value.eq.assert_called_with("url", "https://a.com/x")


def test_article_insert_many_empty_is_noop():
    db, client = _db_with_mock_client()
    repo = ArticleRepository(db)
    assert repo.insert_many([]) == []
    client.table.return_value.upsert.assert_not_called()


# --------------------------------------------------------------------------- #
# TrendRepository
# --------------------------------------------------------------------------- #
def test_trend_upsert_on_topic_week():
    db, client = _db_with_mock_client()
    client.table.return_value.upsert.return_value.execute.return_value = _resp(
        [{"id": 1, "topic": "AI agents", "week": "2026-W24", "article_count": 3}]
    )
    repo = TrendRepository(db)

    row = repo.upsert("AI agents", "2026-W24", 3, actors={"OpenAI": 2})

    assert row["article_count"] == 3
    client.table.assert_called_with("trends")
    args, kwargs = client.table.return_value.upsert.call_args
    assert kwargs["on_conflict"] == "topic,week"
    # the row payload carries the (topic, week) key and count/actors
    payload = args[0]
    assert payload["topic"] == "AI agents"
    assert payload["week"] == "2026-W24"
    assert payload["actors"] == {"OpenAI": 2}


def test_trend_get_by_week():
    db, client = _db_with_mock_client()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = _resp(
        [{"topic": "pricing change", "week": "2026-W24"}]
    )
    repo = TrendRepository(db)

    rows = repo.get_by_week("2026-W24")
    assert rows and rows[0]["topic"] == "pricing change"
    client.table.return_value.select.return_value.eq.assert_called_with("week", "2026-W24")


# --------------------------------------------------------------------------- #
# ReportRepository
# --------------------------------------------------------------------------- #
def test_report_store():
    db, client = _db_with_mock_client()
    client.table.return_value.insert.return_value.execute.return_value = _resp(
        [{"id": 9, "week": "2026-W24"}]
    )
    repo = ReportRepository(db)

    row = repo.store("2026-W24", "<html>hi</html>")
    assert row["id"] == 9
    client.table.assert_called_with("reports")
    args, _ = client.table.return_value.insert.call_args
    assert args[0] == {"week": "2026-W24", "content_html": "<html>hi</html>"}


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #
def test_execute_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    db, _ = _db_with_mock_client()  # attempts=3
    query = MagicMock()
    query.execute.side_effect = [RuntimeError("cold start"), RuntimeError("cold start"), _resp([])]

    result = db.execute(query)

    assert result.data == []
    assert query.execute.call_count == 3


def test_execute_raises_after_max_attempts(monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    db, _ = _db_with_mock_client()  # attempts=3
    query = MagicMock()
    query.execute.side_effect = RuntimeError("still paused")

    with pytest.raises(RuntimeError, match="still paused"):
        db.execute(query)
    assert query.execute.call_count == 3


# --------------------------------------------------------------------------- #
# Healthcheck / keep-alive
# --------------------------------------------------------------------------- #
def test_healthcheck_ok():
    db, client = _db_with_mock_client()
    client.rpc.return_value.execute.return_value = _resp(1)
    assert db.healthcheck() is True
    client.rpc.assert_called_with("ping", None)


def test_healthcheck_failure_returns_false(monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    db, client = _db_with_mock_client()
    client.rpc.return_value.execute.side_effect = RuntimeError("unreachable")
    assert db.healthcheck() is False


def test_keepalive_main_success(monkeypatch):
    from sentinel.db import keepalive

    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    fake_db = MagicMock()
    fake_db.healthcheck.return_value = True
    monkeypatch.setattr(keepalive.SupabaseDB, "connect", classmethod(lambda cls, *a, **k: fake_db))

    assert keepalive.main() == 0


def test_keepalive_main_missing_secrets_returns_nonzero(monkeypatch):
    from sentinel.db import keepalive

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert keepalive.main() == 1
