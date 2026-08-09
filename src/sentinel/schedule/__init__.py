"""Per-request scheduling — the specification's "réception périodique".

A request declares its own cadence (``weekly`` / ``monthly`` / ``quarterly``) in
``requests/<slug>.yaml``; a **daily** dispatcher works out which requests are due
and produces exactly one report per request per period.

Three separable pieces:

* :mod:`~sentinel.schedule.due` — pure, calendar-driven due-ness (no I/O).
* :mod:`~sentinel.schedule.runs` — the ``runs`` ledger and its claim protocol,
  which is what makes "exactly once" true across retries and races.
* :mod:`~sentinel.schedule.dispatcher` — claim, execute the right capability,
  deliver, record.

The production weekly watch is deliberately **not** dispatched from here: its
request file carries ``scheduled: false`` because ``.github/workflows/weekly.yml``
already owns it. Two owners would mean two Monday emails.
"""

from .dispatcher import (
    EXECUTORS,
    MAX_RUNS_PER_DISPATCH,
    DispatchReport,
    Outcome,
    Repos,
    connect_repos,
    dispatch,
    recipients_for,
    subject_for,
)
from .due import DueRequest, SkippedRequest, period_for, plan
from .runs import MemoryRunRepository, RunRepository, is_blocking

__all__ = [
    "dispatch",
    "connect_repos",
    "DispatchReport",
    "Outcome",
    "Repos",
    "EXECUTORS",
    "MAX_RUNS_PER_DISPATCH",
    "recipients_for",
    "subject_for",
    "plan",
    "period_for",
    "DueRequest",
    "SkippedRequest",
    "RunRepository",
    "MemoryRunRepository",
    "is_blocking",
]
