"""The three-tier citation policy — enforced in code, not merely requested.

The original rule ("every claim cites a collected article, else it is deleted")
made two of the new requirements impossible: a McKinsey study is not in our
article corpus, and a 3–5 year projection cannot cite last week's news. Relaxing
the rule would have traded away the one property that makes the report
trustworthy, so it is **replaced by three enforced tiers** instead:

===== ================================================ ==========================
Tier  Meaning                                          Enforcement
===== ================================================ ==========================
A     Cites an article Sentinel collected              must resolve to Tier A evidence
B     Cites a web source the search actually retrieved must resolve to Tier B evidence
C     A projection / scenario / argued hypothesis      must be anchored to >=1 A or B
                                                       item AND rendered with a
                                                       visible hypothesis label
===== ================================================ ==========================

Two structural safeguards do the heavy lifting:

1. **The model never writes a URL** — it emits evidence ids only. An invented id
   resolves to nothing and the claim is dropped, so fabricated links cannot exist.
2. **Publisher names are stored alongside links**, so attribution survives even if
   a grounding redirect expires.

Anything that fails validation is *dropped and counted*, never silently reworded.
The counts land in the report's methodology footer, making the guarantee auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..logging_conf import get_logger
from .evidence import TIER_CORPUS, TIER_WEB, Evidence, EvidenceStore

logger = get_logger("research.citations")

TIER_PROSPECTIVE = "C"
VALID_TIERS = (TIER_CORPUS, TIER_WEB, TIER_PROSPECTIVE)

# How confident a statement is. Prospective content may never claim 'established'.
CONFIDENCE_LEVELS = ("established", "reported", "estimated", "hypothetical")
PROSPECTIVE_CONFIDENCE = ("estimated", "hypothetical")


@dataclass
class Claim:
    """A single statement destined for the report, with its evidence."""

    text: str
    evidence: list[Evidence] = field(default_factory=list)
    tier: str = TIER_CORPUS
    confidence: str = "reported"
    basis: str = ""                      # why a projection follows from its evidence
    section: str = ""

    @property
    def is_prospective(self) -> bool:
        return self.tier == TIER_PROSPECTIVE

    @property
    def needs_hypothesis_label(self) -> bool:
        """Prospective claims must be visibly marked as analysis, not fact."""
        return self.is_prospective

    def sources_for_template(self) -> list[dict[str, str]]:
        return [{"url": e.url, "label": e.label(), "title": e.title, "tier": e.tier}
                for e in self.evidence]


@dataclass
class ValidationReport:
    """What survived validation, and what did not — for the methodology footer.

    Note the two different units: ``unknown_evidence_ids`` counts *citations* that
    failed to resolve (a hallucination signal), while the ``dropped_*`` fields count
    *claims* removed. One claim citing two invented ids contributes 2 to the former
    and 1 to the latter, so they must not be summed together.
    """

    accepted: list[Claim] = field(default_factory=list)
    unknown_evidence_ids: int = 0          # unresolvable citations seen (not claims)
    dropped_uncited: int = 0               # claims with no usable evidence
    dropped_unanchored_projection: int = 0  # forecasts with no factual anchor
    downgraded_confidence: int = 0

    @property
    def dropped_total(self) -> int:
        """Number of CLAIMS dropped (never mixes in the id-level counter)."""
        return self.dropped_uncited + self.dropped_unanchored_projection

    def summary(self) -> str:
        return (f"{len(self.accepted)} claim(s) accepted; "
                f"{self.dropped_total} dropped "
                f"({self.dropped_uncited} uncited, "
                f"{self.dropped_unanchored_projection} unanchored projection(s)); "
                f"{self.unknown_evidence_ids} unresolvable citation(s)")


class CitationRegistry:
    """Validates model-emitted claims against the evidence store."""

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    # -- helpers ----------------------------------------------------------- #
    def resolve(self, eids: Iterable[Any]) -> tuple[list[Evidence], int]:
        """Resolve evidence ids to evidence. Returns ``(resolved, unknown_count)``.

        Unknown ids are the fingerprint of a hallucinated citation; they are
        counted rather than tolerated.
        """
        resolved: list[Evidence] = []
        unknown = 0
        for raw in eids or []:
            if not isinstance(raw, str):
                unknown += 1
                continue
            item = self.store.get(raw)
            if item is None:
                logger.debug("Dropping unknown evidence id %r", raw)
                unknown += 1
            elif item not in resolved:
                resolved.append(item)
        return resolved, unknown

    @staticmethod
    def _infer_tier(evidence: list[Evidence], prospective: bool) -> str:
        if prospective:
            return TIER_PROSPECTIVE
        # A claim citing any corpus article is Tier A; otherwise Tier B.
        return TIER_CORPUS if any(e.tier == TIER_CORPUS for e in evidence) else TIER_WEB

    # -- the rule ---------------------------------------------------------- #
    def validate_claim(
        self,
        text: str,
        eids: Iterable[Any],
        *,
        prospective: bool = False,
        confidence: str = "reported",
        basis: str = "",
        basis_eids: Iterable[Any] | None = None,
        section: str = "",
        report: ValidationReport | None = None,
    ) -> Claim | None:
        """Validate one claim. Returns the :class:`Claim`, or ``None`` if rejected.

        Rejection reasons, all counted in ``report``:
        * no evidence cited at all;
        * every cited id was unknown (i.e. invented);
        * a projection with no factual anchor.
        """
        report = report if report is not None else ValidationReport()
        text = (text or "").strip()
        if not text:
            return None

        resolved, unknown = self.resolve(eids)
        if unknown:
            report.unknown_evidence_ids += unknown

        # A projection may anchor on separately-declared basis evidence.
        if prospective and basis_eids:
            extra, extra_unknown = self.resolve(basis_eids)
            report.unknown_evidence_ids += extra_unknown
            for e in extra:
                if e not in resolved:
                    resolved.append(e)

        if not resolved:
            if prospective:
                report.dropped_unanchored_projection += 1
                logger.debug("Dropping unanchored projection: %r", text[:70])
            else:
                report.dropped_uncited += 1
                logger.debug("Dropping uncited claim: %r", text[:70])
            return None

        conf = confidence if confidence in CONFIDENCE_LEVELS else "reported"
        # A forward-looking statement can never be presented as established fact.
        if prospective and conf not in PROSPECTIVE_CONFIDENCE:
            conf = "estimated"
            report.downgraded_confidence += 1

        claim = Claim(
            text=text,
            evidence=resolved,
            tier=self._infer_tier(resolved, prospective),
            confidence=conf,
            basis=(basis or "").strip(),
            section=section,
        )
        report.accepted.append(claim)
        return claim

    def validate_many(self, items: list[dict], *, section: str = "") -> ValidationReport:
        """Validate a batch of model-emitted claim dicts.

        Each item may carry: ``text``, ``evidence`` (ids), ``prospective``,
        ``confidence``, ``basis``, ``basis_evidence``.
        """
        report = ValidationReport()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            self.validate_claim(
                item.get("text", ""),
                item.get("evidence") or item.get("sources") or [],
                prospective=bool(item.get("prospective")),
                confidence=str(item.get("confidence", "reported")),
                basis=str(item.get("basis", "")),
                basis_eids=item.get("basis_evidence"),
                section=section or str(item.get("section", "")),
                report=report,
            )
        logger.info("Citation validation [%s]: %s", section or "report", report.summary())
        return report
