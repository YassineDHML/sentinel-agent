"""The ``runs`` ledger — one row per (request, period), and the claim protocol.

This is what makes a daily dispatcher safe. Two properties matter:

**Exactly once per period.** Claiming is an ``INSERT`` guarded by
``UNIQUE(request_slug, period_key)``. A second dispatcher (a manual
``workflow_dispatch`` racing the cron, a retried job) loses on the constraint and
gets ``None`` rather than generating a second report and sending a second email.

**But not "never again" after a failure.** A failed run must be retried tomorrow,
and a run left ``running`` by a CI job that hit its timeout must not block the
request forever. So a collision is not automatically a refusal: the existing row
is re-read and, if it is *reclaimable*, taken over with a **conditional update**
— ``UPDATE ... WHERE id = ? AND status = <what we observed>``. Postgres evaluates
that predicate under the row lock, so exactly one racer's update matches; the
loser sees zero affected rows and backs off. No advisory locks, no polling.

The unique violation is expected here, which is why every insert passes
``retry_on=retry_unless_unique``: without it the client would retry a
deterministic failure five times with backoff (see :mod:`sentinel.db.errors`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.errors import is_unique_violation, retry_unless_unique
from ..logging_conf import get_logger

logger = get_logger("schedule.runs")

Row = dict[str, Any]

RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
SKIPPED = "skipped"

STATUSES: tuple[str, ...] = (RUNNING, SUCCESS, FAILED, SKIPPED)

#: Terminal states that mean "this period is done; do not run it again".
BLOCKING_STATUSES: tuple[str, ...] = (SUCCESS, SKIPPED)

#: How long a ``running`` row is trusted before it is assumed abandoned. The
#: weekly GitHub Actions job caps at 30 minutes and the research runs are minutes,
#: so 6 h is far beyond any legitimate run while still recovering the same day.
RECLAIM_AFTER_HOURS = 6


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_blocking(row: Row, *, now: datetime | None = None,
                reclaim_after_hours: int = RECLAIM_AFTER_HOURS) -> bool:
    """Whether an existing run row forbids a new attempt for its period.

    Pure, so the (fiddly) staleness rule is unit-testable without a database.

    * ``success`` / ``skipped`` — done, never re-run.
    * ``running`` — blocking while fresh; reclaimable once stale (a row with an
      unreadable ``started_at`` is treated as stale rather than blocking forever).
    * ``failed`` — never blocking; tomorrow's dispatch retries it.
    """
    status = str(row.get("status") or "").lower()
    if status in BLOCKING_STATUSES:
        return True
    if status != RUNNING:
        return False
    started = _parse(row.get("started_at"))
    if started is None:
        return False
    return started > _now(now) - timedelta(hours=reclaim_after_hours)


class RunRepository:
    """CRUD + claim protocol for the ``runs`` table."""

    TABLE = "runs"

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- reads ------------------------------------------------------------- #
    def for_slugs(self, slugs: list[str]) -> list[Row]:
        """Return every run row for these request slugs (one round-trip)."""
        if not slugs:
            return []
        query = self.db.table(self.TABLE).select("*").in_("request_slug", slugs)
        return self.db.execute(query).data or []

    def blocking_keys(
        self, slugs: list[str], *, now: datetime | None = None,
        reclaim_after_hours: int = RECLAIM_AFTER_HOURS,
    ) -> set[tuple[str, str]]:
        """``(slug, period_key)`` pairs that must not be dispatched.

        A cheap pre-filter so the planner doesn't do pointless work; :meth:`claim`
        remains the authoritative gate.
        """
        return {
            (row["request_slug"], row["period_key"])
            for row in self.for_slugs(slugs)
            if is_blocking(row, now=now, reclaim_after_hours=reclaim_after_hours)
        }

    def get(self, request_slug: str, period_key: str) -> Row | None:
        query = (self.db.table(self.TABLE).select("*")
                 .eq("request_slug", request_slug).eq("period_key", period_key).limit(1))
        data = self.db.execute(query).data or []
        return data[0] if data else None

    def list_recent(self, limit: int = 20) -> list[Row]:
        query = (self.db.table(self.TABLE).select("*")
                 .order("started_at", desc=True).limit(limit))
        return self.db.execute(query).data or []

    # -- claim ------------------------------------------------------------- #
    def claim(
        self,
        *,
        request_slug: str,
        period_key: str,
        report_type: str | None = None,
        period_kind: str | None = None,
        now: datetime | None = None,
        reclaim_after_hours: int = RECLAIM_AFTER_HOURS,
        force: bool = False,
    ) -> Row | None:
        """Take ownership of ``(request_slug, period_key)``, or return ``None``.

        ``None`` means somebody else owns this period (or it is already done) —
        the caller must not generate or send anything.

        ``force`` re-opens a period that is already ``success``/``skipped`` — the
        deliberate "produce it again" escape hatch. It does **not** steal a run
        that is genuinely in flight: a fresh ``running`` row still refuses, because
        overriding it would mean two dispatches generating and emailing at once.
        """
        moment = _now(now)
        row: Row = {
            "request_slug": request_slug,
            "period_key": period_key,
            "report_type": report_type,
            "period_kind": period_kind,
            "status": RUNNING,
            "attempts": 1,
            "started_at": _iso(moment),
        }
        try:
            data = self.db.execute(
                self.db.table(self.TABLE).insert(row), retry_on=retry_unless_unique
            ).data or []
            if data:
                logger.info("Claimed run %s/%s (new).", request_slug, period_key)
                return data[0]
            # An insert that reports no rows still succeeded; re-read to get the id.
            return self.get(request_slug, period_key)
        except Exception as exc:  # noqa: BLE001 - only a collision is expected here
            if not is_unique_violation(exc):
                raise
            logger.debug("Run %s/%s already exists; checking whether it is reclaimable.",
                         request_slug, period_key)

        return self._reclaim(request_slug, period_key, now=moment,
                             reclaim_after_hours=reclaim_after_hours, force=force)

    def _reclaim(
        self, request_slug: str, period_key: str, *, now: datetime,
        reclaim_after_hours: int, force: bool = False,
    ) -> Row | None:
        """Take over an existing row if it is failed, a stale ``running``, or forced."""
        existing = self.get(request_slug, period_key)
        if existing is None:                      # deleted between insert and read
            return None

        observed = str(existing.get("status") or "").lower()
        forced_reopen = force and observed in BLOCKING_STATUSES
        if not forced_reopen and is_blocking(
                existing, now=now, reclaim_after_hours=reclaim_after_hours):
            logger.info("Run %s/%s is owned (status=%s); skipping.%s",
                        request_slug, period_key, observed,
                        "" if observed == RUNNING else " Use --force to produce it again.")
            return None
        if forced_reopen:
            logger.warning("Forcing a re-run of %s/%s (was %s).",
                           request_slug, period_key, observed)
        patch = {
            "status": RUNNING,
            "attempts": int(existing.get("attempts") or 0) + 1,
            "started_at": _iso(now),
            "finished_at": None,
            "error": None,
        }
        # Compare-and-swap on the status we observed: only one racer's UPDATE can
        # match, so only one of them proceeds.
        query = (self.db.table(self.TABLE).update(patch)
                 .eq("id", existing["id"]).eq("status", observed))
        if observed == RUNNING:
            cutoff = _iso(now - timedelta(hours=reclaim_after_hours))
            query = query.lt("started_at", cutoff)

        data = self.db.execute(query).data or []
        if not data:
            logger.info("Lost the race to reclaim run %s/%s; skipping.",
                        request_slug, period_key)
            return None
        logger.warning("Reclaimed %s run %s/%s (attempt %d).",
                       observed, request_slug, period_key, patch["attempts"])
        return data[0]

    # -- completion -------------------------------------------------------- #
    def finish(
        self,
        run_id: Any,
        *,
        status: str,
        report_id: int | None = None,
        error: str | None = None,
        note: str | None = None,
        now: datetime | None = None,
    ) -> Row | None:
        """Close out a claimed run. ``status`` must be a terminal state."""
        if status not in (SUCCESS, FAILED, SKIPPED):
            raise ValueError(f"{status!r} is not a terminal run status")
        patch: Row = {"status": status, "finished_at": _iso(_now(now))}
        for key, value in (("report_id", report_id), ("error", error), ("note", note)):
            if value is not None:
                patch[key] = value if key != "error" else str(value)[:2000]
        data = self.db.execute(
            self.db.table(self.TABLE).update(patch).eq("id", run_id)
        ).data or []
        logger.info("Run %s finished: status=%s%s", run_id, status,
                    f" error={error}" if error else "")
        return data[0] if data else None


class MemoryRunRepository:
    """In-memory stand-in with the same interface (dry runs and tests).

    Keeps the dispatcher free of ``if dry_run`` branches around persistence: it
    always claims and always finishes, against whichever ledger it was given.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Row] = {}
        self._next_id = 1

    def for_slugs(self, slugs: list[str]) -> list[Row]:
        return [r for r in self.rows.values() if r["request_slug"] in set(slugs)]

    def blocking_keys(self, slugs, *, now=None, reclaim_after_hours=RECLAIM_AFTER_HOURS):
        return {
            (r["request_slug"], r["period_key"]) for r in self.for_slugs(slugs)
            if is_blocking(r, now=now, reclaim_after_hours=reclaim_after_hours)
        }

    def get(self, request_slug: str, period_key: str) -> Row | None:
        return self.rows.get((request_slug, period_key))

    def list_recent(self, limit: int = 20) -> list[Row]:
        return sorted(self.rows.values(), key=lambda r: r.get("started_at") or "",
                      reverse=True)[:limit]

    def claim(self, *, request_slug, period_key, report_type=None, period_kind=None,
              now=None, reclaim_after_hours=RECLAIM_AFTER_HOURS, force=False) -> Row | None:
        existing = self.rows.get((request_slug, period_key))
        if existing is not None:
            observed = str(existing.get("status") or "").lower()
            forced_reopen = force and observed in BLOCKING_STATUSES
            if not forced_reopen and is_blocking(
                    existing, now=now, reclaim_after_hours=reclaim_after_hours):
                return None
            existing.update(status=RUNNING, attempts=int(existing.get("attempts") or 0) + 1,
                            started_at=_iso(_now(now)), finished_at=None, error=None)
            return existing
        row = {
            "id": self._next_id, "request_slug": request_slug, "period_key": period_key,
            "report_type": report_type, "period_kind": period_kind, "status": RUNNING,
            "attempts": 1, "started_at": _iso(_now(now)),
        }
        self._next_id += 1
        self.rows[(request_slug, period_key)] = row
        return row

    def finish(self, run_id, *, status, report_id=None, error=None, note=None,
               now=None) -> Row | None:
        for row in self.rows.values():
            if row["id"] == run_id:
                row.update(status=status, finished_at=_iso(_now(now)),
                           report_id=report_id, error=error, note=note)
                return row
        return None
