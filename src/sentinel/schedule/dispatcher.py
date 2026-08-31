"""Run the requests that are due, exactly once each, and deliver them.

This is the "réception périodique" of the specification: the user declares a
cadence in ``requests/<slug>.yaml`` and a daily job turns it into a delivered
report. The dispatcher owns three things and nothing else:

1. **What runs** — delegated to the pure planner in :mod:`sentinel.schedule.due`.
2. **That it runs once** — delegated to the claim protocol in
   :mod:`sentinel.schedule.runs`.
3. **How a report type is produced** — one executor per ``report_type``, each a
   thin adapter over a capability that already exists and is already tested.

No report-generation logic lives here. Adding a fourth report type means adding
an entry to :data:`EXECUTORS`, not editing the scheduler.

Failure isolation is per-request: a request that raises is recorded ``failed``
(so tomorrow retries it) and the remaining requests still run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..deliver.email import send_report
from ..logging_conf import get_logger
from ..period import Period
from ..request.onboarding import Onboarding, load_onboarding
from ..request.profile import RequestProfile, load_profiles
from . import runs as runs_mod
from .due import DueRequest, SkippedRequest, plan

logger = get_logger("schedule.dispatcher")

#: Reports generated per dispatch, at most. Free-tier LLM quotas are per-minute
#: and per-day, and a quarter boundary can make several requests due at once
#: (1 January is the start of a week, a month AND a quarter). Anything over the
#: cap is deferred, not dropped — its period is still current tomorrow.
MAX_RUNS_PER_DISPATCH = 2

#: Longest theme fragment allowed into an email subject line.
SUBJECT_THEME_CHARS = 70


@dataclass
class ExecutionResult:
    """What an executor produced. ``html`` is ``None`` when nothing is sendable."""

    report_path: Path | None = None
    html: str | None = None
    title: str = ""
    word_count: int | None = None
    already_delivered: bool = False        # the executor did its own delivery
    degradations: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    """The result of dispatching one request."""

    slug: str
    report_type: str
    period_key: str
    status: str                            # success / failed / skipped
    report_path: Path | None = None
    delivered: bool = False
    error: str | None = None
    degradations: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"{self.slug} [{self.period_key}] {self.status}"]
        if self.delivered:
            bits.append("delivered")
        if self.error:
            bits.append(f"error={self.error}")
        return " · ".join(bits)


@dataclass
class DispatchReport:
    """Everything one dispatch decided and did."""

    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[SkippedRequest] = field(default_factory=list)
    deferred: list[DueRequest] = field(default_factory=list)
    planned: list[DueRequest] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.status != runs_mod.FAILED for o in self.outcomes)

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.status == runs_mod.SUCCESS)


@dataclass
class Repos:
    """The repositories a dispatch may need. All optional (dry runs use none)."""

    article: Any = None
    trend: Any = None
    report: Any = None
    runs: Any = None


def connect_repos() -> Repos:
    """Build every repository from Supabase. Raises if the DB is unreachable."""
    from ..db import ArticleRepository, ReportRepository, SupabaseDB, TrendRepository

    db = SupabaseDB.connect()
    return Repos(
        article=ArticleRepository(db), trend=TrendRepository(db),
        report=ReportRepository(db), runs=runs_mod.RunRepository(db),
    )


# --------------------------------------------------------------------------- #
# Delivery parameters
# --------------------------------------------------------------------------- #
def recipients_for(
    profile: RequestProfile, settings: Any, onboarding: Onboarding | None = None
) -> list[str]:
    """Who receives this request's report.

    Resolution order — request > onboarding > ``config.yaml``. The last step
    matters: a request that names nobody still reaches the team rather than being
    generated and silently dropped.
    """
    if profile.recipients:
        return list(profile.recipients)
    if onboarding is not None and onboarding.recipients:
        return list(onboarding.recipients)
    return list(getattr(getattr(settings, "report", None), "recipients", []) or [])


def subject_for(profile: RequestProfile, period: Period, *, fallback: str = "Sentinel") -> str:
    """Subject line for a scheduled request.

    Without an explicit ``subject_prefix`` the theme is folded in, because several
    requests can land in the same inbox in the same period and identical subjects
    make them look like duplicates of one report.
    """
    prefix = profile.subject_prefix
    if not prefix:
        theme = profile.theme.strip()
        if len(theme) > SUBJECT_THEME_CHARS:
            theme = theme[:SUBJECT_THEME_CHARS].rstrip() + "…"
        prefix = f"{fallback} · {theme}"
    return f"{prefix} — {period.key}"


# --------------------------------------------------------------------------- #
# Executors — one per report_type
# --------------------------------------------------------------------------- #
def execute_weekly_watch(
    due: DueRequest, settings: Settings, repos: Repos, *, emailer: Callable[..., bool]
) -> ExecutionResult:
    """The v1 weekly watch, run under this request's parameters.

    ``run_pipeline`` already emails and Slacks, so this executor reports
    ``already_delivered`` rather than sending a second copy.
    """
    from ..pipeline import run_pipeline
    from ..request.overlay import apply_profile

    effective = apply_profile(settings, due.profile)
    result = run_pipeline(
        effective, week=due.period.key,
        article_repo=repos.article, trend_repo=repos.trend, report_repo=repos.report,
        emailer=emailer,
    )
    if not result.ok:
        raise RuntimeError("; ".join(result.degradations) or "pipeline reported failure")
    return ExecutionResult(
        report_path=result.report_path, html=None, title=due.profile.theme,
        already_delivered=True, degradations=list(result.degradations),
    )


def execute_deep_research(
    due: DueRequest, settings: Settings, repos: Repos, *, emailer: Callable[..., bool]
) -> ExecutionResult:
    """Capability 1 — the ~3000-word deep-research report."""
    from ..report.research_builder import write_research_report
    from ..research.runner import ResearchConfig, run_research

    config = ResearchConfig.from_profile(due.profile, period_label=due.period.key)
    report = run_research(config, settings.llm.gemini_model, settings=settings)
    if not report.blocks:
        raise RuntimeError("research produced no content")

    path, html = write_research_report(
        report, generated_at=_now_iso(), slug=due.profile.slug,
        period_key=due.period.key, report_repo=repos.report,
        app_name=settings.app.name,
    )
    return ExecutionResult(
        report_path=path, html=html, title=report.theme,
        word_count=report.integrity.actual_words,
        degradations=list(report.integrity.degradations),
    )


def execute_competitor_scan(
    due: DueRequest, settings: Settings, repos: Repos, *, emailer: Callable[..., bool]
) -> ExecutionResult:
    """Capability 2 — the competitor comparison report."""
    import dataclasses

    from ..report.competitor_builder import write_competitor_report
    from ..research.company import load_company
    from ..research.competitors import MAX_COMPETITORS, run_competitor_report

    company = load_company(due.profile.company_ref)
    if due.profile.language and due.profile.language != company.language:
        company = dataclasses.replace(company, language=due.profile.language)

    # None means "no opinion" — defer to the engine's default rather than
    # duplicating the number in the profile layer.
    requested = due.profile.max_competitors
    report = run_competitor_report(
        company, settings.llm.gemini_model, period_label=due.period.key,
        max_competitors=MAX_COMPETITORS if requested is None else requested,
    )
    if not report.dossiers:
        raise RuntimeError("no competitor could be sourced")

    path, html = write_competitor_report(
        report, generated_at=_now_iso(), slug=due.profile.slug,
        period_key=due.period.key, report_repo=repos.report,
        app_name=settings.app.name,
    )
    return ExecutionResult(
        report_path=path, html=html, title=f"{company.name} — competitor analysis",
        degradations=list(report.integrity.degradations),
    )


#: report_type -> executor. The scheduler knows no report internals beyond this.
EXECUTORS: dict[str, Callable[..., ExecutionResult]] = {
    "weekly_watch": execute_weekly_watch,
    "deep_research": execute_deep_research,
    "competitor_scan": execute_competitor_scan,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def dispatch(
    settings: Settings,
    *,
    profiles: list[RequestProfile] | None = None,
    onboarding: Onboarding | None = None,
    repos: Repos | None = None,
    reference: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
    only: str | None = None,
    max_runs: int = MAX_RUNS_PER_DISPATCH,
    emailer: Callable[..., bool] = send_report,
    executors: dict[str, Callable[..., ExecutionResult]] | None = None,
) -> DispatchReport:
    """Plan, claim, run and deliver every request that is due.

    Args:
        profiles: Candidates; defaults to every file in ``requests/``.
        repos: Persistence. Without a runs ledger the dispatch still works but
            loses its exactly-once guarantee, so that case is logged loudly.
        dry_run: Plan only — nothing is generated, claimed, sent or recorded.
        force: Re-run periods already in the ledger, and ignore ``scheduled:
            false``. For manual catch-up.
        max_runs: Cap on reports generated this dispatch; the rest are deferred
            and reported (never silently dropped).

    Returns:
        A :class:`DispatchReport`. Individual failures live in its outcomes; the
        call itself only raises if planning is impossible.
    """
    profiles = profiles if profiles is not None else load_profiles()
    onboarding = onboarding or load_onboarding()
    repos = repos or Repos()
    run_repo = repos.runs
    executors = executors or EXECUTORS

    completed: set[tuple[str, str]] = set()
    if run_repo is not None:
        try:
            completed = run_repo.blocking_keys([p.slug for p in profiles])
        except Exception as exc:  # noqa: BLE001 - a readable ledger is not worth a lost run
            logger.error("Could not read the runs ledger (%s); the claim step still "
                         "protects against duplicates.", exc)
    elif not dry_run:
        logger.warning("No runs ledger available: exactly-once delivery is NOT guaranteed "
                       "for this dispatch.")

    due, skipped = plan(profiles, completed=completed, reference=reference,
                        force=force, only=only)
    report = DispatchReport(skipped=skipped, planned=list(due))

    for item in skipped:
        logger.info("skip %s", item.describe())

    if max_runs is not None and max_runs >= 0 and len(due) > max_runs:
        report.deferred = due[max_runs:]
        due = due[:max_runs]
        logger.warning("Cap of %d report(s) per dispatch reached; deferring %d to the next "
                       "run: %s", max_runs, len(report.deferred),
                       ", ".join(d.profile.slug for d in report.deferred))

    if dry_run:
        for item in due:
            logger.info("[dry-run] would run %s", item.describe())
        return report

    for item in due:
        report.outcomes.append(
            _run_one(item, settings, repos, onboarding=onboarding,
                     emailer=emailer, executors=executors, force=force)
        )

    failed = sum(1 for o in report.outcomes if o.status == runs_mod.FAILED)
    not_claimed = sum(1 for o in report.outcomes if o.status == runs_mod.SKIPPED)
    logger.info("Dispatch finished: %d succeeded, %d failed, %d not claimed, "
                "%d not due, %d deferred.",
                report.succeeded, failed, not_claimed,
                len(report.skipped), len(report.deferred))
    return report


def _run_one(
    item: DueRequest,
    settings: Settings,
    repos: Repos,
    *,
    onboarding: Onboarding | None,
    emailer: Callable[..., bool],
    executors: dict[str, Callable[..., ExecutionResult]],
    force: bool = False,
) -> Outcome:
    """Claim, execute and deliver a single request. Never raises."""
    profile, period = item.profile, item.period
    outcome = Outcome(slug=profile.slug, report_type=profile.report_type,
                      period_key=period.key, status=runs_mod.SKIPPED)

    run_repo = repos.runs or runs_mod.MemoryRunRepository()
    try:
        claim = run_repo.claim(request_slug=profile.slug, period_key=period.key,
                               report_type=profile.report_type, period_kind=period.kind,
                               force=force)
    except Exception as exc:  # noqa: BLE001 - an unusable ledger fails this request only
        outcome.status = runs_mod.FAILED
        outcome.error = f"could not claim a run: {exc}"
        # Deliberately no report: without a claim we cannot promise exactly-once,
        # and a duplicate email is worse than a late one.
        logger.error("%s: %s", profile.slug, outcome.error)
        return outcome

    if claim is None:
        outcome.error = _why_not_claimed(run_repo, profile.slug, period.key)
        logger.info("skip %s: %s", profile.slug, outcome.error)
        return outcome

    executor = executors.get(profile.report_type)
    if executor is None:
        outcome.status = runs_mod.FAILED
        outcome.error = f"no executor for report_type {profile.report_type!r}"
        logger.error("%s: %s", profile.slug, outcome.error)
        _finish(run_repo, claim, status=runs_mod.FAILED, error=outcome.error)
        return outcome

    logger.info("Running %s", item.describe())
    try:
        result = executor(item, settings, repos, emailer=emailer)
    except Exception as exc:  # noqa: BLE001 - one bad request must not stop the rest
        outcome.status = runs_mod.FAILED
        outcome.error = str(exc)
        logger.exception("Request %s failed for %s: %s", profile.slug, period.key, exc)
        _finish(run_repo, claim, status=runs_mod.FAILED, error=str(exc))
        return outcome

    outcome.report_path = result.report_path
    outcome.degradations = list(result.degradations)
    outcome.delivered = result.already_delivered

    if not result.already_delivered and result.html:
        try:
            outcome.delivered = bool(emailer(
                result.html, settings, week=period.key,
                recipients=recipients_for(profile, settings, onboarding),
                subject=subject_for(profile, period,
                                    fallback=getattr(settings.app, "name", "Sentinel")),
            ))
        except Exception as exc:  # noqa: BLE001 - the report exists; delivery is separable
            outcome.degradations.append(f"email delivery failed: {exc}")
            logger.error("Email delivery failed for %s (report kept at %s): %s",
                         profile.slug, result.report_path, exc)

    outcome.status = runs_mod.SUCCESS
    _finish(run_repo, claim, status=runs_mod.SUCCESS,
            report_id=_archived_report_id(repos, profile.slug, period.key),
            note=str(result.report_path) if result.report_path else None)
    logger.info("Done %s", outcome.describe())
    return outcome


def _why_not_claimed(run_repo: Any, slug: str, period_key: str) -> str:
    """Explain a refused claim by reading the row, instead of guessing.

    The three cases look identical from ``claim() -> None`` but mean very
    different things to an operator, so it is worth one extra read.
    """
    try:
        row = run_repo.get(slug, period_key)
    except Exception:  # noqa: BLE001 - never let diagnostics break the dispatch
        return "could not be claimed"
    status = str((row or {}).get("status") or "").lower()
    if status in runs_mod.BLOCKING_STATUSES:
        return f"already produced for this period (status={status}); use --force to redo it"
    if status == runs_mod.RUNNING:
        return "currently running in another dispatch"
    return "could not be claimed"


def _finish(run_repo: Any, claim: dict, **kwargs: Any) -> None:
    """Close a run out. A ledger write must never change what actually happened.

    If this fails after a successful report the row stays ``running`` and is
    reclaimed six hours later — annoying, but it cannot turn a delivered report
    into a raised exception.
    """
    try:
        run_repo.finish(claim["id"], **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not record the outcome of run %s: %s", claim.get("id"), exc)


def _archived_report_id(repos: Repos, slug: str, period_key: str) -> int | None:
    """Best-effort link from the run to its archived report row.

    Bookkeeping must never turn a delivered report into a failed run, so every
    error here is swallowed.
    """
    if repos.report is None:
        return None
    try:
        row = repos.report.get_latest_for_request(slug, period_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not link run to report row: %s", exc)
        return None
    return row.get("id") if row else None
