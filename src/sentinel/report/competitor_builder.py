"""Render a :class:`CompetitorReport` into email-ready HTML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logging_conf import get_logger
from ..research.competitors import CompetitorReport
from .builder import DEFAULT_OUTPUT_DIR, TEMPLATES_DIR, _get_env

logger = get_logger("report.competitor_builder")

COMPETITOR_TEMPLATE = "competitor_report.html"

THREAT_COLORS: dict[str, dict[str, str]] = {
    "high": {"fg": "#b91c1c", "bg": "#fef2f2"},
    "medium": {"fg": "#b45309", "bg": "#fffbeb"},
    "low": {"fg": "#047857", "bg": "#ecfdf5"},
}

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "doc_title": "Competitor Analysis",
        "generated_label": "generated",
        "s1": "1. Competitors ranked by threat",
        "s1_hint": "Most dangerous first.",
        "s2": "2. Competitor dossiers",
        "s3": "3. Strategic synthesis",
        "top3": "The three most dangerous", "plan": "Recommended action plan",
        "col_name": "Competitor", "col_threat": "Threat", "col_position": "Positioning",
        "col_offer": "Main offer",
        "f_offer": "Main offer", "f_positioning": "Marketing positioning",
        "f_target": "Target customer", "f_traction": "Traction / market share",
        "b_strengths": "Strengths — what makes them dangerous",
        "b_weaknesses": "Weaknesses — where they are vulnerable",
        "b_risks": "Risks for us", "b_opportunities": "Angles of attack",
        "threat_high": "HIGH", "threat_medium": "MEDIUM", "threat_low": "LOW",
        "hypothesis": "HYPOTHESIS",
        "empty": "Not covered by the available sources.",
        "methodology": "Methodology & traceability",
        "m_competitors": "Competitors analysed", "m_evidence": "Sources",
        "m_accepted": "statements retained", "m_dropped": "statements dropped (unsourced)",
        "m_unresolvable": "unresolvable citations",
        "m_note": ("Every statement cites a source that was actually retrieved. Market-share "
                   "and traction figures are shown only when a source provides them — never "
                   "estimated. Anything unsourced was removed, not reworded."),
        "m_degraded": "Degraded",
        "footer": "Sentinel — automated competitive intelligence. Generated without manual review.",
    },
    "fr": {
        "doc_title": "Analyse concurrentielle",
        "generated_label": "généré le",
        "s1": "1. Concurrents classés par niveau de menace",
        "s1_hint": "Les plus dangereux en premier.",
        "s2": "2. Dossiers par concurrent",
        "s3": "3. Synthèse stratégique",
        "top3": "Les trois plus dangereux", "plan": "Plan d'action recommandé",
        "col_name": "Concurrent", "col_threat": "Menace", "col_position": "Positionnement",
        "col_offer": "Offre principale",
        "f_offer": "Offre principale", "f_positioning": "Positionnement marketing",
        "f_target": "Cible client", "f_traction": "Traction / part de marché",
        "b_strengths": "Forces — ce qui les rend dangereux",
        "b_weaknesses": "Faiblesses — où ils sont vulnérables",
        "b_risks": "Risques pour nous", "b_opportunities": "Angles d'attaque",
        "threat_high": "ÉLEVÉE", "threat_medium": "MOYENNE", "threat_low": "FAIBLE",
        "hypothesis": "HYPOTHÈSE",
        "empty": "Non couvert par les sources disponibles.",
        "methodology": "Méthodologie et traçabilité",
        "m_competitors": "Concurrents analysés", "m_evidence": "Sources",
        "m_accepted": "affirmations retenues", "m_dropped": "affirmations écartées (non sourcées)",
        "m_unresolvable": "citations non résolues",
        "m_note": ("Chaque affirmation cite une source réellement consultée. Les parts de marché "
                   "et chiffres de traction ne sont affichés que lorsqu'une source les fournit — "
                   "jamais estimés. Tout élément non sourcé a été supprimé, et non reformulé."),
        "m_degraded": "Dégradations",
        "footer": "Sentinel — veille concurrentielle automatisée. Généré sans relecture manuelle.",
    },
}


def labels_for(language: str | None) -> dict[str, str]:
    return LABELS.get((language or "en").lower(), LABELS["en"])


def build_competitor_context(
    report: CompetitorReport, *, generated_at: str, app_name: str = "Sentinel"
) -> dict[str, Any]:
    """Assemble the template context (pure, no I/O)."""
    lab = labels_for(report.language)

    dossiers = []
    for d in report.dossiers:
        dossiers.append({
            "name": d.name, "threat_level": d.threat_level, "positioning": d.positioning,
            "description": d.description, "main_offer": d.main_offer,
            "marketing_positioning": d.marketing_positioning,
            "target_customer": d.target_customer, "traction": d.traction,
            "blocks": [
                {"title": lab["b_strengths"], "claims": d.strengths, "color": "#047857"},
                {"title": lab["b_weaknesses"], "claims": d.weaknesses, "color": "#b45309"},
                {"title": lab["b_risks"], "claims": d.risks, "color": "#b91c1c"},
                {"title": lab["b_opportunities"], "claims": d.opportunities, "color": "#1e3a8a"},
            ],
        })

    return {
        "app_name": app_name,
        "company": report.company,
        "report_language": report.language,
        "period_label": report.period_label,
        "generated_at": generated_at,
        "labels": lab,
        "threat_colors": THREAT_COLORS,
        "dossiers": dossiers,
        "top_threats": report.top_threats,
        "action_plan": report.action_plan,
        "integrity": report.integrity,
    }


def render_competitor_html(context: dict[str, Any], *, template_dir: Path | None = None) -> str:
    env = _get_env(template_dir or TEMPLATES_DIR)
    return env.get_template(COMPETITOR_TEMPLATE).render(**context)


def write_competitor_report(
    report: CompetitorReport,
    *,
    generated_at: str,
    slug: str,
    period_key: str,
    output_dir: Path | str | None = None,
    report_repo: Any | None = None,
    app_name: str = "Sentinel",
) -> tuple[Path, str]:
    """Render, archive and write the report. Returns ``(path, html)``."""
    from .builder import report_output_path

    context = build_competitor_context(report, generated_at=generated_at, app_name=app_name)
    html = render_competitor_html(context)

    if report_repo is not None:
        try:
            report_repo.store(
                period_key, html, report_type="competitor", request_slug=slug,
                language=report.language, period_key=period_key,
                title=f"Competitor analysis — {report.company}",
            )
        except Exception as exc:  # noqa: BLE001 - the local file is the fallback record
            logger.error("Could not archive the competitor report (continuing): %s", exc)

    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = report_output_path(period_key, report_type="competitor",
                              request_slug=slug, output_dir=out_dir)
    path.write_text(html, encoding="utf-8")
    logger.info("Competitor report written to %s (%d dossier(s))", path, len(report.dossiers))
    return path, html
