"""Generate a competitor comparison report.

    python -m sentinel.competitors --dry-run
    python -m sentinel.competitors --max 6
    python -m sentinel.competitors --company profiles/company.yaml --language en

Reads who "we" are from ``profiles/company.yaml``. ``--dry-run`` still performs the
real research but writes only the local HTML file (no database archive).

Requires GEMINI_API_KEY. Cost is roughly 2 calls per competitor plus 2.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from ..config import ConfigError, load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from ..period import current_period
from ..report.competitor_builder import write_competitor_report
from ..research.company import load_company
from ..research.competitors import MAX_COMPETITORS, run_competitor_report

logger = get_logger("competitors.cli")


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.competitors")
    parser.add_argument("--company", default=None, help="company profile YAML")
    parser.add_argument("--language", default=None, help="report language (en/fr)")
    parser.add_argument("--max", type=int, default=MAX_COMPETITORS,
                        help=f"max competitors to analyse (default {MAX_COMPETITORS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the local file only: no DB archive")
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
        require_secrets(["GEMINI_API_KEY"])
        company = load_company(args.company)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    if args.language:
        import dataclasses

        company = dataclasses.replace(company, language=args.language)

    period = current_period("month")
    print(f"\ncompany  : {company.name}")
    print(f"language : {company.language}")
    print(f"declared : {', '.join(company.known_competitors) or '(none)'}")
    print(f"max      : {args.max} competitors")
    print("-" * 70)

    report = run_competitor_report(company, settings.llm.gemini_model,
                                   max_competitors=args.max, period_label=period.key)

    report_repo = None
    if not args.dry_run:
        try:
            from ..db import ReportRepository, SupabaseDB

            report_repo = ReportRepository(SupabaseDB.connect())
        except Exception as exc:  # noqa: BLE001 - the local file still gets written
            logger.warning("No database archive this run: %s", exc)

    path, _ = write_competitor_report(
        report,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        slug="competitors", period_key=period.key, report_repo=report_repo,
        app_name=settings.app.name,
    )

    i = report.integrity
    print(f"\ncompetitors : {len(report.dossiers)}")
    for d in report.dossiers:
        print(f"   {d.threat_level:<6} {d.name:<26} "
              f"{len(d.strengths)}S / {len(d.weaknesses)}W / {len(d.risks)}R")
    print(f"top threats : {len(report.top_threats)} | actions: {len(report.action_plan)}")
    print(f"evidence    : {i.evidence_total} | claims kept {i.claims_accepted}, "
          f"dropped {i.claims_dropped}, unresolvable {i.unresolvable_citations}")
    print(f"llm calls   : {i.llm_calls}")
    if i.degradations:
        print(f"degraded    : {'; '.join(i.degradations)}")
    print(f"\nreport      : {path}\n")
    return 0 if report.dossiers else 1


if __name__ == "__main__":
    raise SystemExit(_main())
