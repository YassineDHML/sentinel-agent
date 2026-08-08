"""Hitting a word target reliably.

LLMs cannot count words. Asking for "3000 words" yields ±40% variance, which
misses a ±250 target most of the time. So the length is controlled *structurally*
instead:

1. **Allocate** — Python decides how many blocks each section gets and how long
   each should be. The prompt then asks for something countable ("3 paragraphs of
   about 100 words"), which models follow far better than a global word count.
2. **Overshoot deliberately** — allocate above target, because expanding is an
   extra LLM call while trimming is free.
3. **Measure in Python** — never trust the model's self-report.
4. **Trim deterministically** — drop the lowest-priority blocks until inside the
   band, respecting floors so the report can't lose a mandatory part.

Trimming can only remove *optional* material: the floors guarantee a minimum of
five key takeaways and keep every scenario and each mandatory kind.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_conf import get_logger
from .contracts import (
    ANALYSIS_KINDS,
    IMPLICATION_KINDS,
    PROSPECTIVE_KINDS,
    SYNTHESIS_KINDS,
    ResearchBlock,
)

logger = get_logger("research.budget")

# Typical lengths the prompts ask for.
WORDS_PER_PARAGRAPH = 105
WORDS_PER_BULLET = 38

# Allocate well above target, for two compounding reasons:
#
# 1. Models under-deliver against a requested length. Measured live: asked for ~203
#    words per prose block, the model produced ~140 (≈72%). Asking for exactly the
#    target therefore lands ~28% short.
# 2. Overshooting is cheap and undershooting is not — surplus is removed by
#    trim_to_target() for free, whereas a shortfall needs another LLM call.
#
# Calibrated against live runs (target 3000 ± 250):
#   overshoot 1.05 -> 2334 words   (delivery ~72%)
#   overshoot 1.35 -> 2654 words   (delivery ~66%)
# 1.55 ≈ 1 / 0.65 puts a typical run inside the band. There is no downside to
# aiming high: trim_to_target() removes any surplus for free, so the error is
# one-sided. Re-calibrate if the model or the target changes.
OVERSHOOT = 1.55

# Never drop these, whatever the budget.
MIN_KEY_TAKEAWAYS = 5
PROTECTED_KINDS: frozenset[str] = frozenset(PROSPECTIVE_KINDS)


@dataclass
class SectionPlan:
    """How much material one section should produce."""

    section: str
    kinds: tuple[str, ...]
    blocks: int
    words: int

    @property
    def words_per_block(self) -> int:
        return max(30, self.words // max(1, self.blocks))


def plan_sections(word_target: int) -> list[SectionPlan]:
    """Split the word target across the three mandatory sections.

    The proportions favour the detailed analysis (§2), which carries the trends,
    disruptions, use cases and scenarios, while keeping §1 dense and scannable —
    an executive must get the essentials in under five minutes.
    """
    total = int(word_target * OVERSHOOT)
    synthesis = int(total * 0.20)      # bullets: takeaways, opportunities, risks, decisions
    analysis = int(total * 0.58)       # the body of the research
    implications = total - synthesis - analysis

    return [
        SectionPlan("synthesis", SYNTHESIS_KINDS,
                    blocks=max(8, synthesis // WORDS_PER_BULLET), words=synthesis),
        SectionPlan("analysis", ANALYSIS_KINDS,
                    blocks=max(9, analysis // (WORDS_PER_PARAGRAPH * 2)), words=analysis),
        SectionPlan("implications", IMPLICATION_KINDS,
                    blocks=max(8, implications // WORDS_PER_PARAGRAPH), words=implications),
    ]


def count_words(blocks: list[ResearchBlock]) -> int:
    return sum(b.word_count for b in blocks)


def _protected(block: ResearchBlock, takeaways_left: int) -> bool:
    """Whether a block must survive trimming."""
    if block.kind in PROTECTED_KINDS:
        return True                                   # scenarios/projections stay
    if block.kind == "key_takeaway" and takeaways_left <= MIN_KEY_TAKEAWAYS:
        return True                                   # the spec demands 5-10
    return False


def trim_to_target(
    blocks: list[ResearchBlock], word_target: int, tolerance: int
) -> tuple[list[ResearchBlock], int]:
    """Drop low-priority blocks until the word count is inside the band.

    Returns ``(kept, dropped_count)``. Costs no LLM calls.
    """
    ceiling = word_target + tolerance
    total = count_words(blocks)
    if total <= ceiling:
        return blocks, 0

    # Trim the least important first; ties broken by dropping the longest, so we
    # reach the target with the fewest removals.
    order = sorted(
        range(len(blocks)),
        key=lambda i: (-blocks[i].priority, -blocks[i].word_count),
    )
    drop: set[int] = set()
    takeaways = sum(1 for b in blocks if b.kind == "key_takeaway")

    for idx in order:
        if total <= ceiling:
            break
        block = blocks[idx]
        if _protected(block, takeaways):
            continue
        drop.add(idx)
        total -= block.word_count
        if block.kind == "key_takeaway":
            takeaways -= 1

    kept = [b for i, b in enumerate(blocks) if i not in drop]
    if drop:
        logger.info("Word budget: trimmed %d block(s), %d -> %d words (target %d±%d).",
                    len(drop), count_words(blocks), count_words(kept), word_target, tolerance)
    return kept, len(drop)


def shortfall(blocks: list[ResearchBlock], word_target: int, tolerance: int) -> int:
    """Words still needed to reach the bottom of the band (0 if already inside)."""
    return max(0, (word_target - tolerance) - count_words(blocks))


def underfilled_kinds(blocks: list[ResearchBlock], limit: int = 3) -> list[str]:
    """Kinds most worth expanding when the report came out short."""
    ranked = sorted(
        (b for b in blocks if b.paragraphs), key=lambda b: b.word_count
    )
    seen: list[str] = []
    for b in ranked:
        if b.kind not in seen:
            seen.append(b.kind)
        if len(seen) >= limit:
            break
    return seen
