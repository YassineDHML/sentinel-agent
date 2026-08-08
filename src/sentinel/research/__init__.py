"""Deep-research layer: grounded sourcing + enforced citation policy.

Two-phase by necessity (verified live): Google-Search grounding and JSON mode
cannot be combined, so research runs as

    ACQUIRE  — grounded prose calls that harvest real web sources
       ↓
    EvidenceStore — every citable item gets an id (A* corpus, B* web)
       ↓
    WRITE    — ungrounded, structured calls that may cite ids only

See :mod:`.grounding` for the measured API behaviour and :mod:`.citations` for
the three-tier policy.
"""

from .citations import (
    CONFIDENCE_LEVELS,
    TIER_PROSPECTIVE,
    Claim,
    CitationRegistry,
    ValidationReport,
)
from .evidence import TIER_CORPUS, TIER_WEB, Evidence, EvidenceStore
from .grounding import GroundedResult, GroundedSource, grounded_generate

__all__ = [
    "grounded_generate",
    "GroundedResult",
    "GroundedSource",
    "EvidenceStore",
    "Evidence",
    "CitationRegistry",
    "Claim",
    "ValidationReport",
    "TIER_CORPUS",
    "TIER_WEB",
    "TIER_PROSPECTIVE",
    "CONFIDENCE_LEVELS",
]
