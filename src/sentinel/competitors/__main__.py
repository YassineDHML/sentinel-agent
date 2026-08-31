"""Generate a competitor comparison report.

    python -m sentinel.competitors --dry-run
    python -m sentinel.competitors --max 6
    python -m sentinel.competitors --company profiles/company.yaml --language en
    python -m sentinel.competitors --profile requests/competitors_quarterly.yaml --dry-run

Reads who "we" are from ``profiles/company.yaml``. ``--dry-run`` still performs the
real research but writes only the local HTML file (no database archive).

``--profile`` previews exactly what the scheduler would produce for a request: the
company profile, language, competitor count, period and output filename all come
from the request file. Individual flags still win over it, so you can vary one
parameter without editing the YAML.

Requires GEMINI_API_KEY. Cost is roughly 2 calls per competitor plus 3.
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
    parser.add_argument("--profile", default=None,
                        help="request profile YAML — preview what the scheduler would produce")
    parser.add_argument("--company", default=None, help="company profile YAML")
    parser.add_argument("--language", default=None, help="report language (en/fr)")
    # default=None, not MAX_COMPETITORS: the resolver below must be able to tell
    # "the user asked for 10" apart from "the user said nothing", or an explicit
    # flag could never be distinguished from — and so never win over — the profile.
    parser.add_argument("--max", type=int, default=None,
                        help=f"max competitors to analyse "
                             f"(default: the profile's value, else {MAX_COMPETITORS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the local file only: no DB archive")
    args = parser.parse_args()

    setup_logging()
    profile = None
    try:
        settings = load_settings()
        require_secrets(["GEMINI_API_KEY"])
        if args.profile:
            from ..request import load_profile

            profile = load_profile(args.profile)
            if profile.report_type != "competitor_scan":
                # Refusing beats warning: the run would otherwise spend real LLM
                # quota producing a report the operator did not ask for.
                raise ConfigError(
                    f"{profile.slug!r} is a {profile.report_type!r} request, not a "
                    f"competitor scan. For a deep-research profile use "
                    f"`python -m sentinel.research --profile {args.profile}`."
                )
        company = load_company(args.company or (profile.company_ref if profile else None))
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    # Every resolution below is: explicit flag > profile > existing default.
    language = args.language or (profile.language if profile else None)
    if language and language != company.language:
        import dataclasses

        company = dataclasses.replace(company, language=language)

    max_competitors = args.max
    if max_competitors is None and profile is not None:
        max_competitors = profile.max_competitors
    if max_competitors is None:
        max_competitors = MAX_COMPETITORS

    if profile is not None:
        from ..period import CADENCE_TO_KIND

        kind = CADENCE_TO_KIND.get(profile.cadence, "month")
        # 'once' maps to 'adhoc', which has no calendar period; a month is the
        # sensible label for a one-off preview.
        period = current_period("month" if kind == "adhoc" else kind)
        slug = profile.slug
    else:
        period = current_period("month")
        slug = "competitors"

    print("")
    if profile is not None:
        print(f"profile  : {profile.slug} [{profile.cadence}] -> {period.key}")
    print(f"company  : {company.name}")
    print(f"language : {company.language}")
    print(f"declared : {', '.join(company.known_competitors) or '(none)'}")
    print(f"max      : {max_competitors} competitors")
    print("-" * 70)

    report = run_competitor_report(company, settings.llm.gemini_model,
                                   max_competitors=max_competitors,
                                   period_label=period.key)

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
        slug=slug, period_key=period.key, report_repo=report_repo,
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
