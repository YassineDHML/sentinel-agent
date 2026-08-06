"""Report label translations.

The report template previously hardcoded English section headings, which made a
{B}-language report impossible. Labels now come from here via the template context.

**The ``en`` entries are byte-identical to the strings the template used**, so an
English report renders exactly as before (guarded by the golden-file snapshot test).
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "en"

HEADINGS: dict[str, dict[str, str]] = {
    "en": {
        "report_title": "Weekly Strategic Intelligence",
        "period_label": "Week",
        "generated_label": "generated",
        "s1": "1. Executive Summary",
        "s2": "2. Competitive Watch",
        "s3": "3. Trends Detected",
        "s4": "4. Opportunities for {company}",
        "s5": "5. Sources Consulted",
        "accelerating": "Accelerating",
        "new": "New",
        "ongoing": "Ongoing",
        "articles_this_week": "article{plural} this week",
        "previous_weeks": "previous weeks",
        "first_appearance": "first appearance",
        "prior_avg": "prior avg",
        "empty_s1": "No executive summary available this week.",
        "empty_s2": "No notable actor activity this week.",
        "empty_s3": "None this week.",
        "empty_s4": "No opportunities identified this week.",
        "empty_s5": "No sources recorded this week.",
        "more_sources": "… and {count} more analyzed source{plural}.",
        "footer": (
            "{app} — automated strategic intelligence, non-commercial internship MVP. "
            "Generated automatically, no manual review."
        ),
    },
    "fr": {
        "report_title": "Veille stratégique hebdomadaire",
        "period_label": "Semaine",
        "generated_label": "généré le",
        "s1": "1. Synthèse opérationnelle",
        "s2": "2. Veille concurrentielle",
        "s3": "3. Tendances détectées",
        "s4": "4. Opportunités pour {company}",
        "s5": "5. Sources consultées",
        "accelerating": "En accélération",
        "new": "Nouvelles",
        "ongoing": "Continues",
        "articles_this_week": "article{plural} cette semaine",
        "previous_weeks": "semaines précédentes",
        "first_appearance": "première apparition",
        "prior_avg": "moyenne précédente",
        "empty_s1": "Aucune synthèse disponible cette semaine.",
        "empty_s2": "Aucune activité notable des acteurs cette semaine.",
        "empty_s3": "Aucune cette semaine.",
        "empty_s4": "Aucune opportunité identifiée cette semaine.",
        "empty_s5": "Aucune source enregistrée cette semaine.",
        "more_sources": "… et {count} autre{plural} source{plural} analysée{plural}.",
        "footer": (
            "{app} — veille stratégique automatisée, MVP de stage non commercial. "
            "Généré automatiquement, sans relecture manuelle."
        ),
    },
}


def headings(language: str | None = None, *, company: str = "Welyne") -> dict[str, str]:
    """Return the label set for ``language``, falling back to English.

    ``company`` is interpolated into labels that name the requesting organisation.
    """
    lang = (language or DEFAULT_LANGUAGE).lower()
    labels = HEADINGS.get(lang) or HEADINGS[DEFAULT_LANGUAGE]
    return {k: v.replace("{company}", company) for k, v in labels.items()}
