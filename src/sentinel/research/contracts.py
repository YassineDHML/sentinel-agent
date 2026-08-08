"""Data model for the deep-research report.

**Why blocks are flat.** A ~3000-word report is far too long for one LLM call, and
a truncated response must still yield something. So every part of the report — a
key takeaway, a scenario, a regulatory risk — is a *block* with a ``kind`` drawn
from a closed vocabulary, delivered as ``{"blocks": [...]}``. If a response is cut
off mid-array we keep every complete block and discard only the tail. A nested
schema (``{"scenarios": {"optimistic": ...}}``) truncated mid-object loses
everything.

The closed ``kind`` vocabulary plays the same role here that the canonical topic
list plays for trends: the model chooses from a fixed set, so assembly is reliable
and unknown values are dropped rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .citations import Claim

# --------------------------------------------------------------------------- #
# Block vocabulary — the three mandatory report sections
# --------------------------------------------------------------------------- #
# §1 SYNTHÈSE OPÉRATIONNELLE
SYNTHESIS_KINDS: tuple[str, ...] = (
    "key_takeaway",
    "top_opportunity",
    "critical_risk",
    "strategic_decision",
)

# §2 ANALYSE DÉTAILLÉE.
# Split into two halves because it carries ~58% of the word budget: one call that
# large truncates (observed live — the whole section was lost). Two smaller calls
# are individually safe and independently recoverable.
ANALYSIS_A_KINDS: tuple[str, ...] = (          # context: what is happening
    "evolution",
    "heavy_trend",
    "weak_signal",
    "disruption_tech",
    "disruption_economic",
    "disruption_regulatory",
    "disruption_societal",
)
ANALYSIS_B_KINDS: tuple[str, ...] = (          # players, evidence, and where it leads
    "competitive_dynamics",
    "key_player",
    "use_case",
    "sector_impact",
    "projection",
    "scenario_optimistic",
    "scenario_central",
    "scenario_degraded",
)
ANALYSIS_KINDS: tuple[str, ...] = ANALYSIS_A_KINDS + ANALYSIS_B_KINDS

# §3 OPPORTUNITÉS, RISQUES & IMPLICATIONS
IMPLICATION_KINDS: tuple[str, ...] = (
    "opportunity",
    "risk_strategic",
    "risk_operational",
    "risk_regulatory",
    "risk_reputational",
    "implication_business_model",
    "implication_organisation",
    "implication_skills",
    "implication_investments",
    "objective_alignment",
)

BLOCK_KINDS: frozenset[str] = frozenset(SYNTHESIS_KINDS + ANALYSIS_KINDS + IMPLICATION_KINDS)

# Kinds that make a claim about the future. These may only be published as Tier C:
# anchored to real evidence AND rendered with a visible hypothesis label.
PROSPECTIVE_KINDS: frozenset[str] = frozenset({
    "projection", "scenario_optimistic", "scenario_central", "scenario_degraded",
})

# Kinds rendered as bullet points rather than prose paragraphs.
BULLET_KINDS: frozenset[str] = frozenset(SYNTHESIS_KINDS)

SECTION_OF_KIND: dict[str, str] = {
    **{k: "synthesis" for k in SYNTHESIS_KINDS},
    **{k: "analysis" for k in ANALYSIS_KINDS},
    **{k: "implications" for k in IMPLICATION_KINDS},
}


def is_prospective(kind: str) -> bool:
    return kind in PROSPECTIVE_KINDS


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #
@dataclass
class ResearchBlock:
    """One unit of the report, already citation-validated."""

    kind: str
    heading: str = ""
    paragraphs: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    claim: Claim | None = None          # carries the evidence + tier + confidence
    horizon: str = ""                   # e.g. "3-5 ans", for prospective blocks
    priority: int = 3                   # 1 = keep at all costs, 5 = trim first

    @property
    def section(self) -> str:
        return SECTION_OF_KIND.get(self.kind, "analysis")

    @property
    def prospective(self) -> bool:
        return is_prospective(self.kind)

    @property
    def text(self) -> str:
        return " ".join([*self.paragraphs, *self.items]).strip()

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def sources(self) -> list[dict[str, str]]:
        return self.claim.sources_for_template() if self.claim else []

    @property
    def needs_hypothesis_label(self) -> bool:
        return bool(self.claim and self.claim.needs_hypothesis_label)

    @property
    def basis(self) -> str:
        return self.claim.basis if self.claim else ""


# --------------------------------------------------------------------------- #
# Integrity — the methodology footer, built in Python from real counts
# --------------------------------------------------------------------------- #
@dataclass
class ResearchIntegrity:
    """Auditable facts about how the report was produced."""

    evidence_total: int = 0
    evidence_corpus: int = 0
    evidence_web: int = 0
    publishers: list[str] = field(default_factory=list)
    grounding_enabled: bool = False
    searches_run: list[str] = field(default_factory=list)
    claims_accepted: int = 0
    claims_dropped: int = 0
    unresolvable_citations: int = 0
    truncated_sections: list[str] = field(default_factory=list)
    llm_calls: int = 0
    target_words: int = 0
    actual_words: int = 0
    degradations: list[str] = field(default_factory=list)

    @property
    def on_target(self) -> bool:
        return abs(self.actual_words - self.target_words) <= max(1, self.target_words // 12)


# --------------------------------------------------------------------------- #
# The assembled report
# --------------------------------------------------------------------------- #
@dataclass
class DeepResearchReport:
    """Everything the template needs, already validated and measured."""

    theme: str
    language: str = "fr"
    period_label: str = ""
    blocks: list[ResearchBlock] = field(default_factory=list)
    integrity: ResearchIntegrity = field(default_factory=ResearchIntegrity)
    params: dict[str, Any] = field(default_factory=dict)

    # -- grouping helpers used by the template ----------------------------- #
    def of_kind(self, *kinds: str) -> list[ResearchBlock]:
        wanted = set(kinds)
        return [b for b in self.blocks if b.kind in wanted]

    def of_section(self, section: str) -> list[ResearchBlock]:
        return [b for b in self.blocks if b.section == section]

    @property
    def word_count(self) -> int:
        return sum(b.word_count for b in self.blocks)

    def scenario(self, which: str) -> ResearchBlock | None:
        blocks = self.of_kind(f"scenario_{which}")
        return blocks[0] if blocks else None
