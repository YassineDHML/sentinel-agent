"""Generate a deep-research report.

    python -m sentinel.research --profile requests/ai_healthcare_fr.yaml
    python -m sentinel.research --profile requests/ai_healthcare_fr.yaml --dry-run
    python -m sentinel.research --theme "AI in logistics" --language en --words 2000

``--dry-run`` still performs the real research (it must, to produce a real report)
but writes only the local HTML file: nothing is archived to the database and no
email is sent.

Requires GEMINI_API_KEY. A full report costs roughly 6 LLM calls.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from ..config import ConfigError, load_settings, require_secrets
from ..logging_conf import get_logger, setup_logging
from ..period import Period, current_period
from ..report.research_builder import write_research_report
from .runner import ResearchConfig, run_research

logger = get_logger("research.cli")


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.research")
    parser.add_argument("--profile", default=None, help="request profile YAML ({A}..{F})")
    parser.add_argument("--theme", default=None, help="theme, if not using a profile")
    parser.add_argument("--language", default=None, help="report language (en/fr)")
    parser.add_argument("--words", type=int, default=None, help="word target override")
    parser.add_argument("--facets", type=int, default=None,
                        help="number of grounded search calls (default 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the local file only: no DB archive, no email")
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
        require_secrets(["GEMINI_API_KEY"])
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    slug = "adhoc"
    period = current_period("month")
    if args.profile:
        from ..request import load_profile
        from ..period import CADENCE_TO_KIND

        profile = load_profile(args.profile)
        slug = profile.slug
        kind = CADENCE_TO_KIND.get(profile.cadence, "month")
        period = current_period(kind) if kind != "adhoc" else Period.trailing(1)
        config = ResearchConfig.from_profile(profile, period_label=period.key)
    elif args.theme:
        config = ResearchConfig(theme=args.theme, period_label=period.key)
    else:
        parser.error("provide --profile or --theme")

    if args.language:
        config.language = args.language
    if args.words:
        config.word_target = args.words
    if args.facets:
        config.max_facets = args.facets

    print(f"\ntheme    : {config.theme}")
    print(f"language : {config.language} | zone: {config.geo_zone} | sector: {config.sector or '-'}")
    print(f"horizon  : -{config.months_back}m / +{config.horizon_years}y")
    print(f"target   : {config.word_target} ± {config.word_tolerance} words")
    print("-" * 70)

    report = run_research(config, settings.llm.gemini_model, settings=settings)

    report_repo = None
    if not args.dry_run:
        try:
            from ..db import ReportRepository, SupabaseDB

            report_repo = ReportRepository(SupabaseDB.connect())
        except Exception as exc:  # noqa: BLE001 - the local file still gets written
            logger.warning("No database archive this run: %s", exc)

    path, _ = write_research_report(
        report,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        slug=slug, period_key=period.key, report_repo=report_repo,
        app_name=settings.app.name,
    )

    i = report.integrity
    print(f"\nblocks     : {len(report.blocks)}")
    print(f"words      : {i.actual_words} (target {i.target_words} ± {config.word_tolerance})"
          f"{'' if i.on_target else '   << OUTSIDE BAND'}")
    print(f"evidence   : {i.evidence_total} ({i.evidence_web} web, {i.evidence_corpus} corpus)")
    print(f"publishers : {', '.join(i.publishers[:10]) or '-'}")
    print(f"claims     : {i.claims_accepted} kept, {i.claims_dropped} dropped, "
          f"{i.unresolvable_citations} unresolvable citation(s)")
    print(f"llm calls  : {i.llm_calls}")
    if i.degradations:
        print(f"degraded   : {'; '.join(i.degradations)}")
    print(f"\nreport     : {path}\n")
    return 0 if report.blocks else 1


if __name__ == "__main__":
    raise SystemExit(_main())
