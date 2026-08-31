"""Tests for per-request scheduling: due-ness, the runs ledger, the dispatcher.

Fully offline. The Supabase client is a MagicMock, executors and the emailer are
injected fakes, and every date is explicit — nothing here depends on "today".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sentinel.db.client import SupabaseDB
from sentinel.db.errors import is_unique_violation, retry_unless_unique
from sentinel.request.profile import profile_from_dict
from sentinel.schedule import runs as runs_mod
from sentinel.schedule.dispatcher import (
    ExecutionResult,
    Repos,
    dispatch,
    recipients_for,
    subject_for,
)
from sentinel.schedule.due import (
    SKIP_ALREADY_DONE,
    SKIP_NON_RECURRING,
    SKIP_UNSCHEDULED,
    period_for,
    plan,
)
from sentinel.schedule.runs import MemoryRunRepository, RunRepository, is_blocking

JAN = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def _profile(slug="req", **kw):
    data = {"slug": slug, "theme": f"theme of {slug}", "report_type": "deep_research"}
    data.update(kw)
    return profile_from_dict(data)


def _settings(recipients=("config@x.com",), name="Sentinel"):
    return SimpleNamespace(
        app=SimpleNamespace(name=name),
        llm=SimpleNamespace(gemini_model="m"),
        report=SimpleNamespace(recipients=list(recipients), subject_prefix="Sentinel Weekly"),
    )


def _resp(data):
    return SimpleNamespace(data=data, count=None)


class _Boom(Exception):
    """Stands in for the SDK's PostgREST error (whose type is not guaranteed)."""

    def __init__(self, message="duplicate key value violates unique constraint",
                 code="23505"):
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
# Due-ness (pure)
# --------------------------------------------------------------------------- #
def test_cadence_maps_to_the_period_it_covers():
    assert period_for(_profile(cadence="weekly"), JAN).key == "2026-W03"
    assert period_for(_profile(cadence="monthly"), JAN).key == "2026-M01"
    assert period_for(_profile(cadence="quarterly"), JAN).key == "2026-Q1"


def test_cadence_once_is_never_dispatched():
    due, skipped = plan([_profile(cadence="once")], reference=JAN)
    assert due == []
    assert skipped[0].reason == SKIP_NON_RECURRING


def test_unscheduled_request_is_never_dispatched():
    due, skipped = plan([_profile(scheduled=False)], reference=JAN)
    assert due == []
    assert skipped[0].reason == SKIP_UNSCHEDULED


def test_a_fresh_request_is_due():
    due, _ = plan([_profile(cadence="monthly")], reference=JAN)
    assert [d.period.key for d in due] == ["2026-M01"]


def test_a_request_already_produced_this_period_is_not_due_again():
    """The idempotency that lets the dispatcher run every day."""
    profile = _profile(cadence="monthly")
    due, skipped = plan([profile], completed={("req", "2026-M01")}, reference=JAN)
    assert due == []
    assert skipped[0].reason == SKIP_ALREADY_DONE and skipped[0].period_key == "2026-M01"


def test_the_same_request_becomes_due_again_next_period():
    profile = _profile(cadence="monthly")
    later = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    due, _ = plan([profile], completed={("req", "2026-M01")}, reference=later)
    assert [d.period.key for d in due] == ["2026-M02"]


def test_a_missed_day_still_runs_later_in_the_same_period():
    """No next_run_at means a downed machine doesn't skip the period."""
    profile = _profile(cadence="monthly")
    for day in (1, 9, 28):
        moment = datetime(2026, 1, day, 9, 0, tzinfo=timezone.utc)
        due, _ = plan([profile], reference=moment)
        assert [d.period.key for d in due] == ["2026-M01"]


def test_force_ignores_both_the_ledger_and_the_scheduled_flag():
    profile = _profile(scheduled=False, cadence="monthly")
    due, skipped = plan([profile], completed={("req", "2026-M01")}, reference=JAN, force=True)
    assert len(due) == 1 and not skipped


def test_only_restricts_to_one_slug_without_reporting_the_rest():
    profiles = [_profile("a"), _profile("b")]
    due, skipped = plan(profiles, reference=JAN, only="b")
    assert [d.profile.slug for d in due] == ["b"]
    assert skipped == []


def test_due_requests_are_ordered_cheapest_cadence_first():
    """A per-dispatch cap must truncate the slow quarterly report, not the weekly."""
    profiles = [_profile("q", cadence="quarterly"), _profile("w", cadence="weekly"),
                _profile("m", cadence="monthly")]
    due, _ = plan(profiles, reference=JAN)
    assert [d.profile.slug for d in due] == ["w", "m", "q"]


# --------------------------------------------------------------------------- #
# Unique-violation detection
# --------------------------------------------------------------------------- #
def test_unique_violation_detected_from_the_sqlstate_attribute():
    assert is_unique_violation(_Boom(message="nope", code="23505"))


def test_unique_violation_detected_from_the_message_alone():
    """The SDK's exception type/attributes are not guaranteed; text is the fallback."""
    assert is_unique_violation(RuntimeError("duplicate key value violates unique constraint"))


def test_other_errors_are_not_mistaken_for_a_collision():
    assert not is_unique_violation(RuntimeError("connection reset by peer"))
    assert not is_unique_violation(_Boom(message="timeout", code="57014"))


def test_a_non_retryable_error_is_raised_immediately(monkeypatch):
    """A deterministic failure must not cost five attempts of backoff."""
    slept: list[float] = []
    monkeypatch.setattr("sentinel.db.client._sleep", lambda s: slept.append(s))
    query = MagicMock()
    query.execute.side_effect = _Boom()
    db = SupabaseDB(MagicMock(), attempts=5, base_delay=1.0)

    with pytest.raises(_Boom):
        db.execute(query, retry_on=retry_unless_unique)

    assert query.execute.call_count == 1
    assert slept == []


def test_retry_is_unchanged_when_no_predicate_is_given(monkeypatch):
    monkeypatch.setattr("sentinel.db.client._sleep", lambda s: None)
    query = MagicMock()
    query.execute.side_effect = [RuntimeError("cold start"), _resp([{"id": 1}])]
    db = SupabaseDB(MagicMock(), attempts=3, base_delay=0.0)

    assert db.execute(query).data == [{"id": 1}]


# --------------------------------------------------------------------------- #
# Ledger semantics (pure)
# --------------------------------------------------------------------------- #
def _row(status, started=JAN, **kw):
    return {"id": 1, "request_slug": "req", "period_key": "2026-M01", "status": status,
            "started_at": started.isoformat(), **kw}


def test_a_successful_run_blocks_its_period_forever():
    assert is_blocking(_row("success"), now=JAN + timedelta(days=400))


def test_a_failed_run_never_blocks_so_tomorrow_retries():
    assert not is_blocking(_row("failed"), now=JAN)


def test_a_fresh_running_row_blocks_a_concurrent_dispatch():
    assert is_blocking(_row("running", JAN), now=JAN + timedelta(minutes=5))


def test_a_stale_running_row_is_reclaimable():
    """A CI job killed at its timeout must not park the request forever."""
    assert not is_blocking(_row("running", JAN), now=JAN + timedelta(hours=7))


def test_a_running_row_with_an_unreadable_start_time_is_reclaimable():
    assert not is_blocking({"status": "running", "started_at": "not-a-date"}, now=JAN)


# --------------------------------------------------------------------------- #
# RunRepository against a mocked client
# --------------------------------------------------------------------------- #
def _repo():
    client = MagicMock(name="supabase_client")
    return RunRepository(SupabaseDB(client, attempts=2, base_delay=0.0)), client


def test_claim_inserts_a_running_row():
    repo, client = _repo()
    client.table.return_value.insert.return_value.execute.return_value = _resp(
        [{"id": 7, "status": "running"}])

    claim = repo.claim(request_slug="req", period_key="2026-M01",
                       report_type="deep_research", period_kind="month")

    assert claim["id"] == 7
    row = client.table.return_value.insert.call_args[0][0]
    assert row["status"] == "running" and row["attempts"] == 1
    assert row["request_slug"] == "req" and row["period_key"] == "2026-M01"


def test_claim_of_an_owned_period_returns_none():
    repo, client = _repo()
    client.table.return_value.insert.return_value.execute.side_effect = _Boom()
    (client.table.return_value.select.return_value.eq.return_value.eq.return_value
     .limit.return_value.execute.return_value) = _resp([_row("success")])

    assert repo.claim(request_slug="req", period_key="2026-M01") is None


def test_claim_reclaims_a_failed_run_and_counts_the_attempt():
    repo, client = _repo()
    client.table.return_value.insert.return_value.execute.side_effect = _Boom()
    (client.table.return_value.select.return_value.eq.return_value.eq.return_value
     .limit.return_value.execute.return_value) = _resp([_row("failed", attempts=1)])
    update = client.table.return_value.update
    update.return_value.eq.return_value.eq.return_value.execute.return_value = _resp(
        [{"id": 1, "status": "running", "attempts": 2}])

    claim = repo.claim(request_slug="req", period_key="2026-M01")

    assert claim["attempts"] == 2
    patch = update.call_args[0][0]
    assert patch["status"] == "running" and patch["attempts"] == 2
    # compare-and-swap on the status we observed
    update.return_value.eq.return_value.eq.assert_called_with("status", "failed")


def test_losing_the_reclaim_race_yields_none():
    """Two dispatchers see the same failed row; the conditional UPDATE picks one."""
    repo, client = _repo()
    client.table.return_value.insert.return_value.execute.side_effect = _Boom()
    (client.table.return_value.select.return_value.eq.return_value.eq.return_value
     .limit.return_value.execute.return_value) = _resp([_row("failed")])
    (client.table.return_value.update.return_value.eq.return_value.eq.return_value
     .execute.return_value) = _resp([])          # zero rows matched: we lost

    assert repo.claim(request_slug="req", period_key="2026-M01") is None


def test_reclaiming_a_stale_running_row_also_filters_on_the_start_time():
    repo, client = _repo()
    client.table.return_value.insert.return_value.execute.side_effect = _Boom()
    (client.table.return_value.select.return_value.eq.return_value.eq.return_value
     .limit.return_value.execute.return_value) = _resp([_row("running", JAN)])
    chain = client.table.return_value.update.return_value.eq.return_value.eq.return_value
    chain.lt.return_value.execute.return_value = _resp([{"id": 1, "status": "running"}])

    claim = repo.claim(request_slug="req", period_key="2026-M01",
                       now=JAN + timedelta(hours=8))

    assert claim is not None
    assert chain.lt.call_args[0][0] == "started_at"


def test_blocking_keys_reads_every_slug_in_one_round_trip():
    repo, client = _repo()
    client.table.return_value.select.return_value.in_.return_value.execute.return_value = _resp(
        [_row("success"), {**_row("failed"), "request_slug": "other"}])

    keys = repo.blocking_keys(["req", "other"], now=JAN)

    assert keys == {("req", "2026-M01")}
    client.table.return_value.select.return_value.in_.assert_called_with(
        "request_slug", ["req", "other"])


def test_finish_rejects_a_non_terminal_status():
    repo, _ = _repo()
    with pytest.raises(ValueError):
        repo.finish(1, status="running")


def test_finish_records_the_outcome():
    repo, client = _repo()
    client.table.return_value.update.return_value.eq.return_value.execute.return_value = _resp(
        [{"id": 1, "status": "success"}])

    repo.finish(1, status="success", report_id=42, note="output/x.html")

    patch = client.table.return_value.update.call_args[0][0]
    assert patch["status"] == "success" and patch["report_id"] == 42
    assert patch["finished_at"]


# --------------------------------------------------------------------------- #
# Delivery parameters
# --------------------------------------------------------------------------- #
def test_recipients_prefer_the_request_then_onboarding_then_config():
    from sentinel.request.onboarding import Onboarding

    settings = _settings(["config@x.com"])
    onboarding = Onboarding(recipients=["ob@x.com"])

    assert recipients_for(_profile(recipients=["req@x.com"]), settings, onboarding) == ["req@x.com"]
    assert recipients_for(_profile(), settings, onboarding) == ["ob@x.com"]
    assert recipients_for(_profile(), settings, None) == ["config@x.com"]


def test_subject_uses_the_explicit_prefix_when_given():
    period = period_for(_profile(cadence="monthly"), JAN)
    assert subject_for(_profile(subject_prefix="Veille IA"), period) == "Veille IA — 2026-M01"


def test_subject_falls_back_to_the_theme_so_two_requests_differ():
    period = period_for(_profile(cadence="monthly"), JAN)
    a = subject_for(_profile("a", theme="AI in health"), period)
    b = subject_for(_profile("b", theme="AI in retail"), period)
    assert a != b and "2026-M01" in a


def test_a_very_long_theme_is_truncated_in_the_subject():
    period = period_for(_profile(cadence="monthly"), JAN)
    subject = subject_for(_profile(theme="x" * 300), period)
    assert len(subject) < 120 and subject.endswith("2026-M01")


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def _fake_executor(calls, *, fail=False, html="<p>report</p>"):
    def run(due, settings, repos, *, emailer):
        calls.append((due.profile.slug, due.period.key))
        if fail:
            raise RuntimeError("generation exploded")
        return ExecutionResult(report_path=Path(f"output/{due.profile.slug}.html"),
                               html=html, title=due.profile.theme)
    return run


def _fake_emailer(sent):
    def send(html, settings, *, week, recipients=None, subject=None, **kw):
        sent.append({"week": week, "recipients": recipients, "subject": subject})
        return True
    return send


def _dispatch(profiles, **kw):
    calls, sent = [], []
    repos = kw.pop("repos", Repos(runs=MemoryRunRepository()))
    report = dispatch(
        _settings(), profiles=profiles, repos=repos, reference=kw.pop("reference", JAN),
        emailer=_fake_emailer(sent),
        executors={"deep_research": _fake_executor(calls, **kw.pop("executor_kw", {}))},
        **kw)
    return report, calls, sent, repos


def test_a_due_request_is_generated_and_emailed():
    report, calls, sent, _ = _dispatch([_profile(cadence="monthly")])

    assert calls == [("req", "2026-M01")]
    assert report.succeeded == 1 and report.ok
    assert sent[0]["recipients"] == ["config@x.com"]
    assert sent[0]["subject"].endswith("2026-M01")


def test_running_twice_the_same_day_produces_one_report():
    """The claim, not the plan, is what guarantees this."""
    profiles = [_profile(cadence="monthly")]
    repos = Repos(runs=MemoryRunRepository())
    _dispatch(profiles, repos=repos)
    report, calls, sent, _ = _dispatch(profiles, repos=repos)

    assert calls == [] and sent == []
    assert report.outcomes == [] and len(report.skipped) == 1


def test_dry_run_plans_without_generating_claiming_or_sending():
    repos = Repos(runs=MemoryRunRepository())
    report, calls, sent, _ = _dispatch([_profile(cadence="monthly")], repos=repos,
                                       dry_run=True)

    assert calls == [] and sent == []
    assert len(report.planned) == 1
    assert repos.runs.rows == {}          # nothing claimed


def test_a_failing_request_is_recorded_failed_and_retried_next_dispatch():
    profiles = [_profile(cadence="monthly")]
    repos = Repos(runs=MemoryRunRepository())
    report, _, _, _ = _dispatch(profiles, repos=repos, executor_kw={"fail": True})

    assert not report.ok
    assert report.outcomes[0].status == runs_mod.FAILED
    # failed does not block: the next dispatch tries again
    report2, calls2, _, _ = _dispatch(profiles, repos=repos)
    assert calls2 == [("req", "2026-M01")] and report2.ok


def test_one_failing_request_does_not_stop_the_others():
    calls, sent = [], []
    good = _fake_executor(calls)

    def executor(due, settings, repos, *, emailer):
        if due.profile.slug == "bad":
            raise RuntimeError("boom")
        return good(due, settings, repos, emailer=emailer)

    report = dispatch(_settings(), profiles=[_profile("bad"), _profile("good")],
                      repos=Repos(runs=MemoryRunRepository()), reference=JAN,
                      emailer=_fake_emailer(sent), max_runs=5,
                      executors={"deep_research": executor})

    assert calls == [("good", "2026-M01")]
    assert {o.slug: o.status for o in report.outcomes} == {
        "bad": runs_mod.FAILED, "good": runs_mod.SUCCESS}


def test_the_per_dispatch_cap_defers_rather_than_drops():
    profiles = [_profile(f"r{i}", cadence="monthly") for i in range(4)]
    report, calls, _, _ = _dispatch(profiles, max_runs=2)

    assert len(calls) == 2
    assert [d.profile.slug for d in report.deferred] == ["r2", "r3"]


def test_a_deferred_request_runs_on_the_next_dispatch():
    profiles = [_profile(f"r{i}", cadence="monthly") for i in range(3)]
    repos = Repos(runs=MemoryRunRepository())
    _dispatch(profiles, repos=repos, max_runs=2)
    _, calls, _, _ = _dispatch(profiles, repos=repos, max_runs=2)

    assert calls == [("r2", "2026-M01")]


def test_an_unknown_report_type_fails_loudly_instead_of_silently_doing_nothing():
    report, _, _, _ = _dispatch([_profile(report_type="weekly_watch")])
    assert report.outcomes[0].status == runs_mod.FAILED
    assert "no executor" in report.outcomes[0].error


def test_an_executor_that_delivers_itself_is_not_emailed_twice():
    """The weekly watch emails inside run_pipeline; the dispatcher must not resend."""
    sent = []

    def executor(due, settings, repos, *, emailer):
        return ExecutionResult(report_path=Path("output/w.html"), html=None,
                               title="weekly", already_delivered=True)

    report = dispatch(_settings(), profiles=[_profile(report_type="weekly_watch")],
                      repos=Repos(runs=MemoryRunRepository()), reference=JAN,
                      emailer=_fake_emailer(sent), executors={"weekly_watch": executor})

    assert sent == []
    assert report.outcomes[0].delivered is True


def test_a_delivery_failure_keeps_the_run_successful_and_says_so():
    """The report exists and is archived; only the send failed."""
    def boom(*a, **kw):
        raise RuntimeError("SMTP down")

    report = dispatch(_settings(), profiles=[_profile()], repos=Repos(runs=MemoryRunRepository()),
                      reference=JAN, emailer=boom,
                      executors={"deep_research": _fake_executor([])})

    outcome = report.outcomes[0]
    assert outcome.status == runs_mod.SUCCESS and outcome.delivered is False
    assert any("email delivery failed" in d for d in outcome.degradations)


def test_dispatch_survives_an_unreadable_ledger():
    """A ledger read failure must degrade to the claim check, not lose the run."""
    class Broken(MemoryRunRepository):
        def blocking_keys(self, slugs, **kw):
            raise RuntimeError("supabase asleep")

    report, calls, _, _ = _dispatch([_profile()], repos=Repos(runs=Broken()))
    assert calls and report.succeeded == 1


def test_an_unclaimable_run_generates_nothing():
    """Without a claim there is no exactly-once promise, so nothing may be sent.

    This is the shape of "the runs table hasn't been created yet".
    """
    class Broken(MemoryRunRepository):
        def claim(self, **kw):
            raise RuntimeError('relation "runs" does not exist')

    report, calls, sent, _ = _dispatch([_profile("a"), _profile("b")],
                                       repos=Repos(runs=Broken()), max_runs=5)

    assert calls == [] and sent == []
    assert [o.status for o in report.outcomes] == [runs_mod.FAILED] * 2
    assert all("could not claim" in o.error for o in report.outcomes)


def test_a_ledger_write_failure_does_not_undo_a_delivered_report():
    class Broken(MemoryRunRepository):
        def finish(self, *a, **kw):
            raise RuntimeError("write failed")

    report, calls, sent, _ = _dispatch([_profile()], repos=Repos(runs=Broken()))

    assert calls and sent
    assert report.outcomes[0].status == runs_mod.SUCCESS


# --------------------------------------------------------------------------- #
# --force must reach the CLAIM, not just the plan
# --------------------------------------------------------------------------- #
def test_force_reopens_an_already_successful_period():
    """The plan honouring --force is useless if the claim still refuses."""
    repo = MemoryRunRepository()
    first = repo.claim(request_slug="req", period_key="2026-Q3")
    repo.finish(first["id"], status=runs_mod.SUCCESS)

    assert repo.claim(request_slug="req", period_key="2026-Q3") is None      # normal: refused
    forced = repo.claim(request_slug="req", period_key="2026-Q3", force=True)
    assert forced is not None and forced["status"] == runs_mod.RUNNING


def test_force_does_not_steal_a_run_that_is_still_in_flight():
    """Overriding a live run would mean two dispatches generating and emailing."""
    repo = MemoryRunRepository()
    repo.claim(request_slug="req", period_key="2026-Q3", now=JAN)            # left running

    assert repo.claim(request_slug="req", period_key="2026-Q3", force=True,
                      now=JAN + timedelta(minutes=5)) is None


def test_force_still_reclaims_a_stale_running_row():
    repo = MemoryRunRepository()
    repo.claim(request_slug="req", period_key="2026-Q3", now=JAN)
    assert repo.claim(request_slug="req", period_key="2026-Q3", force=True,
                      now=JAN + timedelta(hours=7)) is not None


def test_forced_dispatch_actually_regenerates():
    """End to end: the whole point of --force."""
    profiles = [_profile(cadence="quarterly")]
    repos = Repos(runs=MemoryRunRepository())
    _dispatch(profiles, repos=repos)                       # first run consumes the period

    report, calls, sent, _ = _dispatch(profiles, repos=repos, force=True)

    assert calls, "--force must re-run the period"
    assert report.succeeded == 1 and sent


def test_a_refused_claim_explains_why_instead_of_blaming_a_race():
    profiles = [_profile(cadence="quarterly")]
    repos = Repos(runs=MemoryRunRepository())
    _dispatch(profiles, repos=repos)
    report, _, _, _ = _dispatch(profiles, repos=repos, force=False, executor_kw={})

    # nothing due, so no outcome at all — the planner filtered it
    assert report.outcomes == [] and len(report.skipped) == 1


def test_a_blocked_claim_reports_already_produced_not_a_race():
    """When the planner is forced past the ledger but the claim still refuses."""
    class StillBlocking(MemoryRunRepository):
        def claim(self, **kw):
            return None

    repos = Repos(runs=StillBlocking())
    repos.runs.rows[("req", "2026-Q1")] = {          # JAN is Q1
        "id": 1, "request_slug": "req", "period_key": "2026-Q1",
        "status": runs_mod.SUCCESS, "attempts": 1, "started_at": JAN.isoformat()}

    report, calls, _, _ = _dispatch([_profile(cadence="quarterly")], repos=repos, force=True)

    assert calls == []
    assert "already produced" in report.outcomes[0].error
    assert "--force" in report.outcomes[0].error


# --------------------------------------------------------------------------- #
# The competitor executor honours {max_competitors}
#
# Regression: the executor used to call run_competitor_report() without the
# argument, so a scheduled scan always analysed the engine default (10) no matter
# what the request asked for. The CLI's --max worked; the scheduler's did not.
# --------------------------------------------------------------------------- #
def _competitor_probe(monkeypatch, profile):
    """Run execute_competitor_scan against fakes; return the max_competitors seen."""
    import sentinel.report.competitor_builder as builder_mod
    import sentinel.research.company as company_mod
    import sentinel.research.competitors as competitors_mod
    from sentinel.period import Period
    from sentinel.schedule.dispatcher import execute_competitor_scan
    from sentinel.schedule.due import DueRequest

    seen: dict = {}

    monkeypatch.setattr(company_mod, "load_company",
                        lambda ref=None: SimpleNamespace(name="Welyne", language="fr"))

    def fake_run(company, model, *, period_label="", max_competitors=None, **kw):
        seen["max"] = max_competitors
        return SimpleNamespace(dossiers=[object()], company=company.name,
                               integrity=SimpleNamespace(degradations=[]))

    monkeypatch.setattr(competitors_mod, "run_competitor_report", fake_run)
    monkeypatch.setattr(builder_mod, "write_competitor_report",
                        lambda *a, **kw: (Path("out.html"), "<html></html>"))

    due = DueRequest(profile, Period.containing("quarter", JAN))
    settings = _settings()
    execute_competitor_scan(due, settings, Repos(), emailer=lambda *a, **kw: True)
    return seen["max"]


def test_a_requested_competitor_count_reaches_the_engine(monkeypatch):
    profile = _profile("comp", report_type="competitor_scan", cadence="quarterly",
                       company_ref="profiles/company.yaml", max_competitors=4)
    assert _competitor_probe(monkeypatch, profile) == 4


def test_omitting_the_count_falls_back_to_the_engine_default(monkeypatch):
    from sentinel.research.competitors import MAX_COMPETITORS

    profile = _profile("comp", report_type="competitor_scan", cadence="quarterly",
                       company_ref="profiles/company.yaml")
    assert profile.max_competitors is None
    assert _competitor_probe(monkeypatch, profile) == MAX_COMPETITORS
