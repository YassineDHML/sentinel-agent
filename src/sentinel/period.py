"""Reporting-period abstraction (weekly / monthly / quarterly / ad-hoc).

The original pipeline is ISO-week-native: ``trends.week``, ``reports.week``,
``output/<week>.html`` and the helpers in :mod:`sentinel.analyze.trends` all assume
one fixed cadence. Parameterized report requests may be weekly, monthly or
quarterly, so period handling is factored out here.

A :class:`Period` is an immutable ``(kind, key, start, end)`` value where ``end``
is **exclusive**, so callers filter with ``start <= ts < end``. Key formats are
chosen to sort chronologically as plain text:

======== ================ ==========================================
kind     key              span
======== ================ ==========================================
week     ``2026-W29``     Monday 00:00 UTC → following Monday
month    ``2026-M07``     1st 00:00 UTC → 1st of next month
quarter  ``2026-Q3``      first day of quarter → first day of next
adhoc    ``adhoc-<...>``  caller-supplied bounds
======== ================ ==========================================

The week flavour is deliberately byte-compatible with
:func:`sentinel.analyze.trends.iso_week_of` and ``week_bounds_iso`` so existing
rows, filenames and tests keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

PeriodKind = Literal["week", "month", "quarter", "adhoc"]

WEEK = "week"
MONTH = "month"
QUARTER = "quarter"
ADHOC = "adhoc"

VALID_KINDS: tuple[str, ...] = (WEEK, MONTH, QUARTER, ADHOC)

# Cadence name (as used in a request profile) -> period kind.
CADENCE_TO_KIND: dict[str, str] = {
    "weekly": WEEK,
    "monthly": MONTH,
    "quarterly": QUARTER,
    "once": ADHOC,
}

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-M(\d{2})$")
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")


class PeriodError(ValueError):
    """Raised when a period key or kind is malformed/unsupported."""


# --------------------------------------------------------------------------- #
# Key formatting
# --------------------------------------------------------------------------- #
def week_key(d: date) -> str:
    """ISO week key for a date, e.g. ``"2026-W29"``."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def month_key(d: date) -> str:
    """Month key for a date, e.g. ``"2026-M07"``."""
    return f"{d.year}-M{d.month:02d}"


def quarter_key(d: date) -> str:
    """Quarter key for a date, e.g. ``"2026-Q3"``."""
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def key_for(kind: str, d: date) -> str:
    """Period key of the given ``kind`` containing date ``d``."""
    if kind == WEEK:
        return week_key(d)
    if kind == MONTH:
        return month_key(d)
    if kind == QUARTER:
        return quarter_key(d)
    raise PeriodError(f"cannot derive a key for kind {kind!r} from a date")


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
def _utc_midnight(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


def _add_months(d: date, months: int) -> date:
    """Return ``d`` shifted by ``months``, clamped to the 1st (period starts only)."""
    total = (d.year * 12 + (d.month - 1)) + months
    return date(total // 12, total % 12 + 1, 1)


def bounds_for_key(kind: str, key: str) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` UTC datetimes for a period key. ``end`` is exclusive."""
    if kind == WEEK:
        m = _WEEK_RE.match(key)
        if not m:
            raise PeriodError(f"malformed week key: {key!r} (expected YYYY-Www)")
        try:
            monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError as exc:  # e.g. week 53 in a 52-week year
            raise PeriodError(f"invalid ISO week {key!r}: {exc}") from exc
        start = _utc_midnight(monday)
        return start, start + timedelta(weeks=1)

    if kind == MONTH:
        m = _MONTH_RE.match(key)
        if not m:
            raise PeriodError(f"malformed month key: {key!r} (expected YYYY-Mmm)")
        month = int(m.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"invalid month in {key!r}")
        first = date(int(m.group(1)), month, 1)
        return _utc_midnight(first), _utc_midnight(_add_months(first, 1))

    if kind == QUARTER:
        m = _QUARTER_RE.match(key)
        if not m:
            raise PeriodError(f"malformed quarter key: {key!r} (expected YYYY-Qn)")
        first = date(int(m.group(1)), (int(m.group(2)) - 1) * 3 + 1, 1)
        return _utc_midnight(first), _utc_midnight(_add_months(first, 3))

    raise PeriodError(f"cannot derive bounds for kind {kind!r} without explicit dates")


# --------------------------------------------------------------------------- #
# Period value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Period:
    """An immutable reporting period. ``end`` is exclusive."""

    kind: str
    key: str
    start: datetime
    end: datetime

    # -- constructors ---------------------------------------------------- #
    @classmethod
    def from_key(cls, kind: str, key: str) -> "Period":
        """Build a period from its ``kind`` + ``key`` (not valid for ``adhoc``)."""
        start, end = bounds_for_key(kind, key)
        return cls(kind=kind, key=key, start=start, end=end)

    @classmethod
    def containing(cls, kind: str, moment: datetime | date | None = None) -> "Period":
        """Period of ``kind`` containing ``moment`` (defaults to now, UTC)."""
        ref = moment or datetime.now(timezone.utc)
        d = ref.date() if isinstance(ref, datetime) else ref
        return cls.from_key(kind, key_for(kind, d))

    @classmethod
    def adhoc(cls, start: datetime, end: datetime, *, label: str | None = None) -> "Period":
        """An arbitrary span, e.g. a 12-month research window."""
        if end <= start:
            raise PeriodError("adhoc period end must be after start")
        key = label or f"adhoc-{start.date().isoformat()}_{end.date().isoformat()}"
        return cls(kind=ADHOC, key=key, start=start, end=end)

    @classmethod
    def trailing(cls, months: int, *, until: datetime | None = None) -> "Period":
        """The ``months``-long window ending at ``until`` (defaults to now, UTC).

        This is how a request's retrospective horizon {D} becomes a period.
        """
        if months <= 0:
            raise PeriodError("trailing months must be positive")
        end = until or datetime.now(timezone.utc)
        start_date = _add_months(end.date().replace(day=1), -months)
        return cls.adhoc(_utc_midnight(start_date), end)

    # -- derived --------------------------------------------------------- #
    @property
    def bounds_iso(self) -> tuple[str, str]:
        """``(start, end)`` as ISO strings — the shape DB filters expect."""
        return self.start.isoformat(), self.end.isoformat()

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        """Last date *included* in the period (``end`` itself is exclusive)."""
        return (self.end - timedelta(days=1)).date()

    def previous(self, n: int = 1) -> "Period":
        """The period ``n`` steps before this one (not valid for ``adhoc``)."""
        if self.kind == ADHOC:
            raise PeriodError("adhoc periods have no previous period")
        if n <= 0:
            raise PeriodError("n must be positive")
        if self.kind == WEEK:
            return Period.containing(WEEK, self.start_date - timedelta(weeks=n))
        step = 1 if self.kind == MONTH else 3
        return Period.containing(self.kind, _add_months(self.start_date, -step * n))

    def prior_keys(self, n: int) -> list[str]:
        """The ``n`` period keys before this one, **most-recent first**."""
        return [self.previous(k).key for k in range(1, n + 1)]

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.key


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #
def current_period(kind: str = WEEK, reference: datetime | None = None) -> Period:
    """The current period of ``kind`` (defaults to the current ISO week)."""
    return Period.containing(kind, reference)


def period_for_cadence(cadence: str, reference: datetime | None = None) -> Period:
    """Map a request cadence (``weekly``/``monthly``/``quarterly``) to a Period."""
    kind = CADENCE_TO_KIND.get(cadence)
    if kind is None:
        raise PeriodError(f"unknown cadence {cadence!r}; expected one of {sorted(CADENCE_TO_KIND)}")
    if kind == ADHOC:
        raise PeriodError("cadence 'once' has no recurring period; use Period.adhoc/trailing")
    return Period.containing(kind, reference)
