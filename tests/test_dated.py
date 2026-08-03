"""Tests for date-bounded discovery queries. Pure string building — no network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.collect.dated import build_dated_queries, build_period_queries
from sentinel.period import MONTH, Period


def test_appends_after_and_before_operators():
    out = build_dated_queries(["OpenAI", "AI agents"], "2026-07-01", "2026-08-01")
    assert out == [
        "OpenAI after:2026-07-01 before:2026-08-01",
        "AI agents after:2026-07-01 before:2026-08-01",
    ]


def test_accepts_datetime_and_date_inputs():
    start = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc)
    (q,) = build_dated_queries(["x"], start, end)
    assert q == "x after:2026-07-01 before:2026-08-01"


def test_accepts_iso_strings_with_z():
    (q,) = build_dated_queries(["x"], "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z")
    assert q == "x after:2026-07-01 before:2026-08-01"


def test_rejects_inverted_range():
    with pytest.raises(ValueError):
        build_dated_queries(["x"], "2026-08-01", "2026-07-01")


def test_empty_query_list_is_noop():
    assert build_dated_queries([], "2026-07-01", "2026-08-01") == []


def test_build_period_queries_uses_period_span():
    period = Period.from_key(MONTH, "2026-M07")
    (q,) = build_period_queries(["Mistral"], period)
    assert q == "Mistral after:2026-07-01 before:2026-08-01"


def test_backfill_week_helper_still_works_via_shared_builder():
    """scripts/backfill.py delegates here; its ISO-week behaviour must be unchanged."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "backfill.py"
    spec = importlib.util.spec_from_file_location("backfill_dated", script)
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    out = backfill.build_week_queries(["OpenAI"], "2026-W28")
    assert out == ["OpenAI after:2026-07-06 before:2026-07-13"]
