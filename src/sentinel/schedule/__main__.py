"""Dispatch the request profiles that are due today.

    python -m sentinel.schedule --dry-run          # what would run today
    python -m sentinel.schedule                    # run + deliver what is due
    python -m sentinel.schedule --only ai_healthcare_fr --force
    python -m sentinel.schedule --date 2026-09-01 --dry-run

Intended to run **daily** from CI. Nothing bad happens if it runs more often, or
if a day is missed: due-ness comes from the calendar and the ``runs`` ledger, not
from a timer.

``--dry-run`` here means *plan only* — it prints what would run and exits without
generating, sending, claiming or recording anything. (This is stricter than the
per-capability CLIs, where ``--dry-run`` still generates a local report.)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from ..config import ConfigError, load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from ..request.onboarding import load_onboarding
from ..request.profile import load_profiles
from .dispatcher import MAX_RUNS_PER_DISPATCH, Repos, connect_repos, dispatch

logger = get_logger("schedule.cli")


def _parse_date(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.schedule")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit: nothing generated, sent or recorded")
    parser.add_argument("--force", action="store_true",
                        help="re-run periods already in the ledger, and ignore 'scheduled: false'")
    parser.add_argument("--only", default=None, help="dispatch a single request slug")
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS_PER_DISPATCH,
                        help=f"cap reports generated this dispatch (default {MAX_RUNS_PER_DISPATCH})")
    parser.add_argument("--date", default=None,
                        help="pretend today is this date (ISO), for planning and catch-up")
    parser.add_argument("--requests", default=None, help="request profile directory")
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
        onboarding = load_onboarding()
        reference = _parse_date(args.date) if args.date else None
    except (ConfigError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    profiles = load_profiles(args.requests, defaults=onboarding)
    if not profiles:
        logger.error("No request profiles found — nothing to dispatch.")
        return 1

    repos = Repos()
    if not args.dry_run:
        try:
            require_secrets(["SUPABASE_URL", "SUPABASE_KEY", "GEMINI_API_KEY"])
            repos = connect_repos()
        except Exception as exc:  # noqa: BLE001 - live dispatch needs the ledger
            logger.error("FATAL: cannot initialize live dispatch: %s", exc)
            return 1

    print(f"\nrequests : {len(profiles)} profile(s)")
    print(f"reference: {(reference or datetime.now(timezone.utc)).date().isoformat()}")
    print(f"mode     : {'plan only' if args.dry_run else 'live'}"
          f"{' (forced)' if args.force else ''}")
    print("-" * 70)

    report = dispatch(settings, profiles=profiles, onboarding=onboarding, repos=repos,
                      reference=reference, dry_run=args.dry_run, force=args.force,
                      only=args.only, max_runs=args.max_runs)

    print(f"\ndue      : {len(report.planned)}")
    for item in report.planned:
        print(f"   {item.describe()}")
    if report.deferred:
        print(f"deferred : {', '.join(d.profile.slug for d in report.deferred)} "
              f"(cap {args.max_runs}/dispatch; still due tomorrow)")
    if report.skipped:
        print(f"skipped  : {len(report.skipped)}")
        for item in report.skipped:
            print(f"   {item.describe()}")
    if report.outcomes:
        print(f"\nran      : {len(report.outcomes)}")
        for outcome in report.outcomes:
            print(f"   {outcome.describe()}")
            if outcome.report_path:
                print(f"      -> {outcome.report_path}")
            for note in outcome.degradations:
                print(f"      ! {note}")
    print()
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
