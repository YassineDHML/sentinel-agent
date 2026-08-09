"""Deciding which requests are due — pure functions, no I/O.

The scheduler deliberately has **no** ``next_run_at`` column. Storing the next
due date means storing a derived value that has to be advanced correctly on every
path, and drifts the moment a run is missed: a machine that was down on the 1st
would compute the next due date from "now" and skip the month entirely.

Instead, due-ness is derived from the calendar:

    a request is due  <=>  we are inside period P of its cadence
                           AND no run for (slug, P.key) exists yet

That is idempotent (a second dispatch the same day finds the run and stops),
self-healing (a missed Monday is still inside the same week on Tuesday), and
computable offline — which is why every rule in this module is a pure function
over ``(profiles, completed_keys, reference_time)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..logging_conf import get_logger
from ..period import CADENCE_TO_KIND, Period, PeriodError, period_for_cadence
from ..request.profile import RequestProfile

logger = get_logger("schedule.due")

# Why a request was not dispatched. Kept as constants so logs and tests agree.
SKIP_UNSCHEDULED = "not scheduled (scheduled: false)"
SKIP_NON_RECURRING = "cadence 'once' has no recurring period — run it by hand"
SKIP_BAD_CADENCE = "unknown cadence"
SKIP_ALREADY_DONE = "already produced for this period"


@dataclass(frozen=True)
class DueRequest:
    """A request that should run now, and the period it covers."""

    profile: RequestProfile
    period: Period

    @property
    def key(self) -> tuple[str, str]:
        return (self.profile.slug, self.period.key)

    def describe(self) -> str:
        return (f"{self.profile.slug} [{self.profile.report_type}] "
                f"{self.profile.cadence} -> {self.period.key}")


@dataclass(frozen=True)
class SkippedRequest:
    """A request that was considered and passed over, with the reason why.

    Skips are returned rather than silently dropped: "nothing ran today" and
    "everything was already done today" look identical in a log otherwise.
    """

    profile: RequestProfile
    reason: str
    period_key: str | None = None

    def describe(self) -> str:
        where = f" ({self.period_key})" if self.period_key else ""
        return f"{self.profile.slug}{where}: {self.reason}"


def period_for(profile: RequestProfile, reference: datetime | None = None) -> Period | None:
    """The period a request would cover if dispatched now, or ``None``.

    ``None`` means the request has no recurring period (cadence ``once``) or an
    unknown cadence — either way it is not dispatchable.
    """
    if profile.cadence not in CADENCE_TO_KIND:
        return None
    try:
        return period_for_cadence(profile.cadence, reference)
    except PeriodError:
        return None


def plan(
    profiles: Iterable[RequestProfile],
    *,
    completed: set[tuple[str, str]] | None = None,
    reference: datetime | None = None,
    force: bool = False,
    only: str | None = None,
) -> tuple[list[DueRequest], list[SkippedRequest]]:
    """Split ``profiles`` into what should run now and what should not.

    Args:
        profiles: Candidate request profiles.
        completed: ``(slug, period_key)`` pairs already produced (from the runs
            ledger). Anything in here is skipped.
        force: Ignore ``completed`` **and** the ``scheduled`` flag — the escape
            hatch for a manual re-run of a period.
        only: Restrict to this slug (silently drops the rest; they are not
            reported as skips because the operator excluded them on purpose).

    Returns:
        ``(due, skipped)``. ``due`` is ordered by cadence — weekly first, then
        monthly, then quarterly — so the cheapest, most time-sensitive report
        wins when a per-dispatch cap truncates the list.
    """
    done = completed or set()
    due: list[DueRequest] = []
    skipped: list[SkippedRequest] = []

    for profile in profiles:
        if only and profile.slug != only:
            continue

        if not profile.scheduled and not force:
            skipped.append(SkippedRequest(profile, SKIP_UNSCHEDULED))
            continue

        period = period_for(profile, reference)
        if period is None:
            reason = (SKIP_NON_RECURRING if profile.cadence == "once"
                      else f"{SKIP_BAD_CADENCE} {profile.cadence!r}")
            skipped.append(SkippedRequest(profile, reason))
            continue

        if not force and (profile.slug, period.key) in done:
            skipped.append(SkippedRequest(profile, SKIP_ALREADY_DONE, period.key))
            continue

        due.append(DueRequest(profile, period))

    due.sort(key=_cadence_rank)
    return due, skipped


_CADENCE_ORDER = {"weekly": 0, "monthly": 1, "quarterly": 2, "once": 3}


def _cadence_rank(item: DueRequest) -> tuple[int, str]:
    return (_CADENCE_ORDER.get(item.profile.cadence, 9), item.profile.slug)
