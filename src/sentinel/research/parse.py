"""Parsing block responses, including partially-truncated ones.

Long-form generation truncates. Because blocks are a flat array, a response cut
off mid-way still contains many *complete* objects, and
:func:`salvage_json_blocks` recovers them by decoding one element at a time and
discarding only the incomplete tail. A section cut at 80% still delivers 80% of
its content instead of nothing.

Blocks are then validated: unknown ``kind`` values are dropped (the vocabulary is
closed, exactly like the canonical topic list), and every block must survive
citation validation before it can reach the report.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..logging_conf import get_logger
from .citations import CitationRegistry, ValidationReport
from .contracts import BLOCK_KINDS, BULLET_KINDS, ResearchBlock, is_prospective

logger = get_logger("research.parse")


def salvage_json_blocks(text: str) -> list[dict[str, Any]]:
    """Extract every complete block object from a (possibly truncated) response.

    Tries a strict parse first; on failure, walks the ``blocks`` array decoding
    objects one by one and keeps those that are whole.
    """
    if not text or not text.strip():
        return []
    cleaned = text.replace("```json", "").replace("```", "").strip()

    # Fast path: the response is valid JSON.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict) and isinstance(obj.get("blocks"), list):
                return [b for b in obj["blocks"] if isinstance(b, dict)]
        except json.JSONDecodeError:
            pass  # fall through to salvage

    # Salvage path: decode array elements individually.
    marker = cleaned.find('"blocks"')
    if marker == -1:
        return []
    array_start = cleaned.find("[", marker)
    if array_start == -1:
        return []

    decoder = json.JSONDecoder()
    blocks: list[dict[str, Any]] = []
    idx = array_start + 1
    while idx < len(cleaned):
        brace = cleaned.find("{", idx)
        if brace == -1:
            break
        try:
            obj, idx = decoder.raw_decode(cleaned, brace)
        except json.JSONDecodeError:
            break                      # the tail is incomplete — stop cleanly
        if isinstance(obj, dict):
            blocks.append(obj)
    if blocks:
        logger.info("Salvaged %d complete block(s) from a truncated response.", len(blocks))
    return blocks


# Evidence references the model tends to embed in the prose itself, e.g. "…growth
# [B1], [B2]." Citations are already captured structurally and rendered as publisher
# links, so leaving these in shows the reader a meaningless code twice over.
_ONE_REF = r"[\[(]\s*[ABab]\d{1,3}(?:\s*[,;]\s*[ABab]\d{1,3})*\s*[\])]"
# A whole run of references, however the model punctuates it: "[B1]", "[B1, B2]",
# "[B1], [B2]" and "(B1) (B2)" must all disappear cleanly, separators included.
_INLINE_EID = re.compile(rf"\s*{_ONE_REF}(?:\s*[,;]?\s*{_ONE_REF})*")
_ORPHAN_PUNCT = re.compile(r"\s+([.,;:!?])")
_MULTISPACE = re.compile(r"[ \t]{2,}")


def strip_inline_citations(text: str) -> str:
    """Remove ``[B1]``-style evidence references from rendered prose."""
    cleaned = _INLINE_EID.sub("", text or "")
    cleaned = _ORPHAN_PUNCT.sub(r"\1", cleaned)
    return _MULTISPACE.sub(" ", cleaned).strip()


def _clean_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        text = strip_inline_citations(str(v))
        if text:
            out.append(text)
    return out


def blocks_from_payload(
    payload: list[dict[str, Any]],
    registry: CitationRegistry,
    *,
    allowed_kinds: tuple[str, ...] | None = None,
    section: str = "",
    report: ValidationReport | None = None,
) -> tuple[list[ResearchBlock], ValidationReport]:
    """Validate raw block dicts into :class:`ResearchBlock` objects.

    A block is kept only if its ``kind`` is in the closed vocabulary, it has some
    text, and its citations resolve (see :mod:`.citations`). Everything else is
    dropped and counted.
    """
    report = report if report is not None else ValidationReport()
    allowed = set(allowed_kinds) if allowed_kinds else set(BLOCK_KINDS)
    out: list[ResearchBlock] = []

    for raw in payload:
        kind = str(raw.get("kind", "")).strip()
        if kind not in allowed:
            logger.debug("Dropping block with unknown kind %r", kind)
            continue

        paragraphs = _clean_lines(raw.get("paragraphs"))
        items = _clean_lines(raw.get("items"))
        # Bullet kinds sometimes arrive as paragraphs and vice-versa; normalise.
        if kind in BULLET_KINDS and paragraphs and not items:
            items, paragraphs = paragraphs, []
        if kind not in BULLET_KINDS and items and not paragraphs:
            paragraphs, items = items, []
        if not paragraphs and not items:
            continue

        prospective = bool(raw.get("prospective")) or is_prospective(kind)
        claim = registry.validate_claim(
            " ".join([*paragraphs, *items]),
            raw.get("evidence") or [],
            prospective=prospective,
            confidence=str(raw.get("confidence", "reported")),
            basis=str(raw.get("basis", "")),
            basis_eids=raw.get("basis_evidence"),
            section=section or kind,
            report=report,
        )
        if claim is None:
            continue                    # uncited or unanchored — never rendered

        out.append(ResearchBlock(
            kind=kind,
            heading=str(raw.get("heading", "")).strip(),
            paragraphs=paragraphs,
            items=items,
            claim=claim,
            horizon=str(raw.get("horizon", "")).strip(),
            priority=_priority_of(kind),
        ))
    return out, report


# Lower number = more important, kept longest when trimming to the word budget.
_PRIORITY: dict[str, int] = {
    "key_takeaway": 1, "critical_risk": 1, "top_opportunity": 1,
    "scenario_central": 1, "scenario_optimistic": 2, "scenario_degraded": 2,
    "projection": 2, "strategic_decision": 2, "opportunity": 2,
    "heavy_trend": 2, "evolution": 3, "competitive_dynamics": 3,
    "weak_signal": 3, "use_case": 3, "sector_impact": 3, "objective_alignment": 3,
}


def _priority_of(kind: str) -> int:
    if kind in _PRIORITY:
        return _PRIORITY[kind]
    if kind.startswith("risk_") or kind.startswith("implication_"):
        return 3
    if kind.startswith("disruption_") or kind == "key_player":
        return 4
    return 4


def extract_key_figures(text: str, max_figures: int = 14) -> list[str]:
    """Pull numeric statements out of acquired prose.

    These are injected into every writer call so a figure quoted in the synthesis
    matches the one in the body — the cheapest guard against numeric drift.
    """
    import re

    figures: list[str] = []
    # A sentence carrying a percentage, a currency amount, or a large magnitude.
    pattern = re.compile(
        r"[^.\n]*?(?:\d[\d ,.]*\s?(?:%|percent|milliard|billion|million|md|bn)"
        r"|[$€£]\s?\d[\d ,.]*)[^.\n]*\.", re.IGNORECASE)
    for match in pattern.finditer(text or ""):
        sentence = " ".join(match.group(0).split())
        if 20 < len(sentence) < 240 and sentence not in figures:
            figures.append(sentence)
        if len(figures) >= max_figures:
            break
    return figures
