"""Tests for the reporting-period abstraction. Pure logic — no I/O."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.analyze.trends import iso_week_of, prior_weeks, week_bounds_iso
from sentinel.period import (
    ADHOC,
    MONTH,
    QUARTER,
    WEEK,
    Period,
    PeriodError,
    current_period,
    month_key,
    period_for_cadence,
    quarter_key,
    week_key,
)


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def test_key_formats():
    d = _dt(2026, 7, 20).date()
    assert week_key(d) == "2026-W30"
    assert month_key(d) == "2026-M07"
    assert quarter_key(d) == "2026-Q3"


def test_quarter_boundaries():
    assert quarter_key(_dt(2026, 1, 1).date()) == "2026-Q1"
    assert quarter_key(_dt(2026, 3, 31).date()) == "2026-Q1"
    assert quarter_key(_dt(2026, 4, 1).date()) == "2026-Q2"
    assert quarter_key(_dt(2026, 12, 31).date()) == "2026-Q4"


# --------------------------------------------------------------------------- #
# Compatibility with the existing week helpers (must not drift)
# --------------------------------------------------------------------------- #
def test_week_key_matches_legacy_iso_week_of():
    for d in (_dt(2026, 1, 1), _dt(2026, 7, 20), _dt(2025, 12, 29), _dt(2026, 12, 31)):
        assert week_key(d.date()) == iso_week_of(d.date())


def test_week_bounds_match_legacy_week_bounds_iso():
    for key in ("2026-W01", "2026-W29", "2025-W52"):
        p = Period.from_key(WEEK, key)
        assert p.bounds_iso == week_bounds_iso(key)


def test_prior_keys_match_legacy_prior_weeks():
    p = Period.from_key(WEEK, "2026-W28")
    assert p.prior_keys(4) == prior_weeks("2026-W28", 4)


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
def test_week_bounds_are_monday_to_monday_exclusive():
    from datetime import timedelta

    p = Period.from_key(WEEK, "2026-W29")
    assert p.start.weekday() == 0            # Monday
    assert (p.end - p.start).days == 7
    # end is exclusive, so the last INCLUDED date is the Sunday before it
    assert p.end_date == (p.start + timedelta(days=6)).date()


def test_month_key_must_use_M_prefix():
    """``2026-07`` is ambiguous with a week key; the M marker is required."""
    with pytest.raises(PeriodError):
        Period.from_key(MONTH, "2026-07")


def test_month_bounds_span_whole_month():
    p = Period.from_key(MONTH, "2026-M07")
    assert p.start == _dt(2026, 7, 1)
    assert p.end == _dt(2026, 8, 1)


def test_month_rolls_over_year():
    p = Period.from_key(MONTH, "2026-M12")
    assert p.start == _dt(2026, 12, 1)
    assert p.end == _dt(2027, 1, 1)


def test_quarter_bounds():
    p = Period.from_key(QUARTER, "2026-Q3")
    assert p.start == _dt(2026, 7, 1)
    assert p.end == _dt(2026, 10, 1)


def test_malformed_keys_raise():
    for kind, key in ((WEEK, "2026W29"), (MONTH, "2026-Mxx"), (QUARTER, "2026-Q5")):
        with pytest.raises(PeriodError):
            Period.from_key(kind, key)


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
def test_previous_week_crosses_year():
    assert Period.from_key(WEEK, "2026-W01").previous().key == "2025-W52"


def test_previous_month_and_quarter():
    assert Period.from_key(MONTH, "2026-M01").previous().key == "2025-M12"
    assert Period.from_key(QUARTER, "2026-Q1").previous().key == "2025-Q4"


def test_prior_keys_ordering_is_recent_first():
    assert Period.from_key(MONTH, "2026-M06").prior_keys(3) == ["2026-M05", "2026-M04", "2026-M03"]


def test_containing_picks_the_right_period():
    assert Period.containing(WEEK, _dt(2026, 7, 20)).key == "2026-W30"
    assert Period.containing(MONTH, _dt(2026, 7, 20)).key == "2026-M07"
    assert Period.containing(QUARTER, _dt(2026, 7, 20)).key == "2026-Q3"


def test_current_period_defaults_to_week():
    assert current_period(reference=_dt(2026, 7, 20)).kind == WEEK


# --------------------------------------------------------------------------- #
# Ad-hoc / trailing windows ({D} retrospective horizon)
# --------------------------------------------------------------------------- #
def test_trailing_window_spans_requested_months():
    p = Period.trailing(12, until=_dt(2026, 7, 20))
    assert p.kind == ADHOC
    assert p.start == _dt(2025, 7, 1)
    assert p.end == _dt(2026, 7, 20)


def test_trailing_rejects_non_positive():
    with pytest.raises(PeriodError):
        Period.trailing(0)


def test_adhoc_requires_end_after_start():
    with pytest.raises(PeriodError):
        Period.adhoc(_dt(2026, 7, 20), _dt(2026, 7, 1))


def test_adhoc_has_no_previous():
    p = Period.trailing(6, until=_dt(2026, 7, 20))
    with pytest.raises(PeriodError):
        p.previous()


# --------------------------------------------------------------------------- #
# Cadence mapping
# --------------------------------------------------------------------------- #
def test_period_for_cadence():
    assert period_for_cadence("weekly", _dt(2026, 7, 20)).kind == WEEK
    assert period_for_cadence("monthly", _dt(2026, 7, 20)).kind == MONTH
    assert period_for_cadence("quarterly", _dt(2026, 7, 20)).kind == QUARTER


def test_unknown_cadence_raises():
    with pytest.raises(PeriodError):
        period_for_cadence("fortnightly")


def test_once_cadence_has_no_recurring_period():
    with pytest.raises(PeriodError):
        period_for_cadence("once")
