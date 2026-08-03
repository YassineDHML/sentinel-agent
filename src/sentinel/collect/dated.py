"""Date-bounded discovery queries.

Google News search honours ``after:``/``before:`` operators inside the query
string, which is currently the only date-restricted discovery mechanism the
project has. :func:`build_dated_queries` generalizes
``scripts/backfill.py::build_week_queries`` (which was ISO-week granular) to an
arbitrary span, so it can also serve a research request's retrospective horizon.

⚠️ Support for ``after:``/``before:`` is **empirical**, not a documented Google
contract. It works today (verified against live results) but is not guaranteed.
"""

from __future__ import annotations

from datetime import date, datetime

from ..logging_conf import get_logger
from ..period import Period

logger = get_logger("collect.dated")


def _as_date(value: date | datetime | str) -> date:
    """Coerce a date/datetime/ISO string to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def build_dated_queries(
    queries: list[str],
    start: date | datetime | str,
    end: date | datetime | str,
) -> list[str]:
    """Bound each query to ``[start, end)`` using Google News date operators.

    Args:
        queries: Base keyword queries.
        start: Inclusive start of the window.
        end: Exclusive end of the window.

    Returns:
        One query string per input, suffixed with ``after:YYYY-MM-DD before:YYYY-MM-DD``.
    """
    after = _as_date(start).isoformat()
    before = _as_date(end).isoformat()
    if before <= after:
        raise ValueError(f"end ({before}) must be after start ({after})")
    return [f"{q} after:{after} before:{before}" for q in queries]


def build_period_queries(queries: list[str], period: Period) -> list[str]:
    """Bound each query to a :class:`~sentinel.period.Period`'s span."""
    return build_dated_queries(queries, period.start, period.end)
