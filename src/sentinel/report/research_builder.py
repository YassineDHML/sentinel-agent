"""Render a :class:`DeepResearchReport` into email-ready HTML.

The model produces structured JSON; this module turns it into a template context.
The LLM never emits HTML, and Jinja autoescaping stays on, so the "no scripts, no
complex CSS, email-pasteable" requirement is satisfied by construction rather than
by asking the model to behave.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logging_conf import get_logger
from ..research.contracts import DeepResearchReport, ResearchBlock
from .builder import DEFAULT_OUTPUT_DIR, TEMPLATES_DIR, _get_env

logger = get_logger("report.research_builder")

RESEARCH_TEMPLATE = "research_report.html"


# --------------------------------------------------------------------------- #
# Labels (FR/EN) — the template holds no natural language of its own
# --------------------------------------------------------------------------- #
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "doc_title": "Strategic Intelligence Report",
        "generated_label": "generated",
        "s1": "1. Operational Synthesis",
        "s1_hint": "The essentials in under five minutes.",
        "s2": "2. Detailed Analysis",
        "s3": "3. Opportunities, Risks & Strategic Implications",
        "sources": "Sources consulted",
        "hypothesis": "HYPOTHESIS",
        "empty": "Not covered by the available sources.",
        "methodology": "Methodology & traceability",
        "m_evidence": "Evidence", "m_web": "web", "m_corpus": "corpus",
        "m_searches": "searches run", "m_accepted": "claims retained",
        "m_dropped": "claims dropped (uncited)", "m_unresolvable": "unresolvable citations",
        "m_words": "words",
        "m_note": ("Every statement cites a source that was actually retrieved. "
                   "Forward-looking statements are labelled HYPOTHESIS and anchored to "
                   "cited evidence. Anything that could not be sourced was removed, not reworded."),
        "m_degraded": "Degraded",
        "footer": "Sentinel — automated strategic intelligence. Generated without manual review.",
        "g_takeaways": "Key takeaways", "g_opportunities": "Major opportunities",
        "g_risks": "Critical risks", "g_decisions": "Decisions to weigh",
        "g_evolutions": "Key developments", "g_trends": "Strong trends",
        "g_weak": "Weak signals", "g_disruptions": "Disruptions",
        "g_competition": "Competitive landscape", "g_usecases": "Use cases & sector impact",
        "g_outlook": "Outlook & scenarios",
        "g_opps": "Business opportunities", "g_riskset": "Risks",
        "g_implications": "Implications", "g_alignment": "Alignment with your priority",
    },
    "fr": {
        "doc_title": "Rapport de veille stratégique",
        "generated_label": "généré le",
        "s1": "1. Synthèse opérationnelle",
        "s1_hint": "L'essentiel en moins de cinq minutes.",
        "s2": "2. Analyse détaillée",
        "s3": "3. Opportunités, risques et implications stratégiques",
        "sources": "Sources consultées",
        "hypothesis": "HYPOTHÈSE",
        "empty": "Non couvert par les sources disponibles.",
        "methodology": "Méthodologie et traçabilité",
        "m_evidence": "Preuves", "m_web": "web", "m_corpus": "corpus",
        "m_searches": "recherches effectuées", "m_accepted": "affirmations retenues",
        "m_dropped": "affirmations écartées (non sourcées)",
        "m_unresolvable": "citations non résolues", "m_words": "mots",
        "m_note": ("Chaque affirmation cite une source réellement consultée. Les projections "
                   "sont signalées HYPOTHÈSE et rattachées à des preuves citées. Tout élément "
                   "non sourçable a été supprimé, et non reformulé."),
        "m_degraded": "Dégradations",
        "footer": "Sentinel — veille stratégique automatisée. Généré sans relecture manuelle.",
        "g_takeaways": "Enseignements clés", "g_opportunities": "Opportunités majeures",
        "g_risks": "Risques critiques", "g_decisions": "Arbitrages à considérer",
        "g_evolutions": "Évolutions clés", "g_trends": "Tendances lourdes",
        "g_weak": "Signaux faibles", "g_disruptions": "Ruptures",
        "g_competition": "Dynamique concurrentielle", "g_usecases": "Cas d'usage et impacts sectoriels",
        "g_outlook": "Projection et scénarios",
        "g_opps": "Opportunités business", "g_riskset": "Risques",
        "g_implications": "Implications", "g_alignment": "Alignement avec votre objectif",
    },
}


def labels_for(language: str | None) -> dict[str, str]:
    return LABELS.get((language or "en").lower(), LABELS["en"])


def _group(title: str, blocks: list[ResearchBlock], color: str = "#1e3a8a") -> dict[str, Any]:
    return {"title": title, "blocks": blocks, "color": color}


def build_research_context(
    report: DeepResearchReport, *, generated_at: str, app_name: str = "Sentinel"
) -> dict[str, Any]:
    """Assemble the template context from a finished report (pure, no I/O)."""
    lab = labels_for(report.language)
    k = report.of_kind

    synthesis_groups = [
        _group(lab["g_takeaways"], k("key_takeaway"), "#111827"),
        _group(lab["g_opportunities"], k("top_opportunity"), "#047857"),
        _group(lab["g_risks"], k("critical_risk"), "#b91c1c"),
        _group(lab["g_decisions"], k("strategic_decision"), "#1e3a8a"),
    ]
    analysis_groups = [
        _group(lab["g_evolutions"], k("evolution")),
        _group(lab["g_trends"], k("heavy_trend")),
        _group(lab["g_weak"], k("weak_signal")),
        _group(lab["g_disruptions"], k("disruption_tech", "disruption_economic",
                                       "disruption_regulatory", "disruption_societal")),
        _group(lab["g_competition"], k("competitive_dynamics", "key_player")),
        _group(lab["g_usecases"], k("use_case", "sector_impact")),
        _group(lab["g_outlook"], k("projection", "scenario_optimistic",
                                   "scenario_central", "scenario_degraded")),
    ]
    implication_groups = [
        _group(lab["g_opps"], k("opportunity"), "#047857"),
        _group(lab["g_riskset"], k("risk_strategic", "risk_operational",
                                   "risk_regulatory", "risk_reputational"), "#b91c1c"),
        _group(lab["g_implications"], k("implication_business_model", "implication_organisation",
                                        "implication_skills", "implication_investments")),
        _group(lab["g_alignment"], k("objective_alignment"), "#1e3a8a"),
    ]

    # Deduplicated source list, ordered by first appearance in the report.
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in report.blocks:
        for s in block.sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                sources.append(s)

    scope_bits = [v for v in (report.params.get("sector"),
                              (report.params.get("geo_zone") or "").replace("_", " ")) if v]

    return {
        "app_name": app_name,
        "theme": report.theme,
        "report_language": report.language,
        "period_label": report.period_label,
        "generated_at": generated_at,
        "labels": lab,
        "scope_line": " · ".join(scope_bits),
        "synthesis_groups": synthesis_groups,
        "analysis_groups": analysis_groups,
        "implication_groups": implication_groups,
        "has_synthesis": any(g["blocks"] for g in synthesis_groups),
        "has_analysis": any(g["blocks"] for g in analysis_groups),
        "has_implications": any(g["blocks"] for g in implication_groups),
        "sources": sources,
        "integrity": report.integrity,
    }


def render_research_html(context: dict[str, Any], *, template_dir: Path | None = None) -> str:
    """Render the research report template. Autoescaping stays on."""
    env = _get_env(template_dir or TEMPLATES_DIR)
    return env.get_template(RESEARCH_TEMPLATE).render(**context)


def write_research_report(
    report: DeepResearchReport,
    *,
    generated_at: str,
    slug: str,
    period_key: str,
    output_dir: Path | str | None = None,
    report_repo: Any | None = None,
    app_name: str = "Sentinel",
) -> tuple[Path, str]:
    """Render, archive and write the report. Returns ``(path, html)``.

    A database failure never prevents the local file from being written.
    """
    from .builder import report_output_path

    context = build_research_context(report, generated_at=generated_at, app_name=app_name)
    html = render_research_html(context)

    if report_repo is not None:
        try:
            report_repo.store(
                period_key, html,
                report_type="research", request_slug=slug,
                language=report.language, period_key=period_key,
                title=report.theme, word_count=report.integrity.actual_words,
                params=report.params,
            )
        except Exception as exc:  # noqa: BLE001 - the local file is the fallback record
            logger.error("Could not archive the research report (continuing): %s", exc)

    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = report_output_path(period_key, report_type="research",
                              request_slug=slug, output_dir=out_dir)
    path.write_text(html, encoding="utf-8")
    logger.info("Research report written to %s (%d words)", path, report.integrity.actual_words)
    return path, html
