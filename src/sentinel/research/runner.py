"""The deep-research runner: acquire evidence, write the report, measure, trim.

```
ACQUIRE (grounded prose, N facet calls)
   ↓  EvidenceStore  — every source gets an id (A* corpus, B* web)
WRITE  §2 analysis  →  §3 implications  →  §1 synthesis (LAST, from the body)
   ↓  citation validation on every block
MEASURE in Python  →  TRIM deterministically  →  DeepResearchReport
```

Two ordering decisions worth knowing:

* **§1 is generated last though it is printed first.** Deriving the executive
  summary from the finished body is the strongest guard against the summary
  contradicting the analysis — and it is how a human analyst works.
* **Every stage degrades rather than aborts.** A failed facet, an unparseable
  section, a missing LLM: each is logged, counted in :class:`ResearchIntegrity`,
  and the report ships with whatever survived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..logging_conf import get_logger
from .budget import count_words, plan_sections, trim_to_target
from .citations import CitationRegistry, ValidationReport
from .contracts import (
    ANALYSIS_A_KINDS,
    ANALYSIS_B_KINDS,
    IMPLICATION_KINDS,
    SYNTHESIS_KINDS,
    DeepResearchReport,
    ResearchBlock,
    ResearchIntegrity,
)
from .evidence import EvidenceStore
from .grounding import GroundedResult, grounded_generate
from .parse import blocks_from_payload, extract_key_figures, salvage_json_blocks
from .prompts import ACQUIRE_FACETS, build_facet_prompt, build_section_prompt, build_synthesis_prompt

logger = get_logger("research.runner")

# Output ceiling per writing call. Generous because extended thinking is charged
# against the same allowance (one observed call spent 2,298 of 3,290 tokens thinking).
SECTION_MAX_TOKENS = 9000
ACQUIRE_MAX_TOKENS = 4000

# Cap thinking on writing calls. Without this the model can spend the entire output
# allowance reasoning and return nothing parseable — observed live: the analysis
# section, 58% of the report, came back empty. 0 is rejected by the model.
WRITER_THINKING_BUDGET = 512

# Re-ask once when a section returns nothing usable, before giving up on it.
SECTION_RETRIES = 1


@dataclass
class ResearchConfig:
    """Everything the runner needs, derived from a request profile."""

    theme: str
    language: str = "fr"
    geo_zone: str = "world"
    sector: str | None = None
    objective: str | None = None
    months_back: int = 12
    horizon_years: int = 3
    word_target: int = 3000
    word_tolerance: int = 250
    max_facets: int = 3
    period_label: str = ""

    @classmethod
    def from_profile(cls, profile: Any, *, period_label: str = "") -> "ResearchConfig":
        return cls(
            theme=profile.theme,
            language=profile.language,
            geo_zone=profile.geo_zone,
            sector=profile.sector,
            objective=profile.objective,
            months_back=profile.horizon_past_months,
            horizon_years=profile.horizon_future_years,
            word_target=profile.word_target,
            word_tolerance=profile.word_tolerance,
            period_label=period_label,
        )


# A writer call: (prompt) -> raw JSON text. Injected so tests need no network.
Writer = Callable[[str], str]


def _default_writer(model: str, settings: Any) -> Writer:
    """Ungrounded JSON-mode writer (tools and JSON mode cannot coexist).

    The thinking budget is essential here, not an optimisation: left unbounded the
    model can consume the whole output allowance reasoning and return nothing.
    """
    from ..analyze.llm import GeminiProvider, LLMClient

    provider = GeminiProvider(
        model, json_mode=True,
        max_output_tokens=SECTION_MAX_TOKENS,
        thinking_budget=WRITER_THINKING_BUDGET,
    )
    client = LLMClient(provider, fallback=None, min_interval_seconds=4.0)
    return client.generate


def acquire_evidence(
    config: ResearchConfig,
    model: str,
    *,
    store: EvidenceStore | None = None,
    grounded_fn: Callable[..., GroundedResult] = grounded_generate,
    integrity: ResearchIntegrity | None = None,
) -> tuple[EvidenceStore, list[str], list[str]]:
    """Phase 1: run grounded searches and collect citable evidence.

    Returns ``(store, key_figures, acquired_texts)``. A facet that fails or returns
    no sources is skipped — the run continues with whatever was gathered.
    """
    store = store or EvidenceStore()
    integrity = integrity or ResearchIntegrity()
    figures: list[str] = []
    texts: list[str] = []

    for name, hint in ACQUIRE_FACETS[: config.max_facets]:
        prompt = build_facet_prompt(
            config.theme, hint,
            sector=config.sector, geo_zone=config.geo_zone, months_back=config.months_back,
        )
        try:
            result = grounded_fn(prompt, model, max_output_tokens=ACQUIRE_MAX_TOKENS)
            integrity.llm_calls += 1
        except Exception as exc:  # noqa: BLE001 - one dead facet must not kill the run
            integrity.degradations.append(f"acquire '{name}' failed: {exc}")
            logger.warning("Acquire facet %r failed: %s", name, exc)
            continue

        if not result.grounded:
            integrity.degradations.append(f"acquire '{name}' returned no sources")
            logger.warning("Acquire facet %r returned no sources; its text is not evidence.", name)
            continue

        store.add_grounded_sources(result.sources)
        texts.append(result.text)
        for q in result.queries:
            if q not in integrity.searches_run:
                integrity.searches_run.append(q)
        for fig in extract_key_figures(result.text):
            if fig not in figures:
                figures.append(fig)

    integrity.grounding_enabled = len(store) > 0
    logger.info("Acquire complete: %s | %d key figure(s) | %d search(es)",
                store.summary(), len(figures), len(integrity.searches_run))
    return store, figures, texts


def _write_section(
    writer: Writer,
    registry: CitationRegistry,
    *,
    prompt: str,
    kinds: tuple[str, ...],
    section: str,
    report: ValidationReport,
    integrity: ResearchIntegrity,
) -> list[ResearchBlock]:
    """Run one writing call and validate its blocks, re-asking once. Never raises."""
    payload: list[dict[str, Any]] = []

    for attempt in range(SECTION_RETRIES + 1):
        try:
            raw = writer(prompt)
            integrity.llm_calls += 1
        except Exception as exc:  # noqa: BLE001
            integrity.degradations.append(f"section '{section}' generation failed: {exc}")
            logger.error("Section %r failed: %s", section, exc)
            return []

        payload = salvage_json_blocks(raw)
        if payload:
            break
        if attempt < SECTION_RETRIES:
            logger.warning("Section %r returned nothing parseable; re-asking once.", section)

    if not payload:
        integrity.degradations.append(f"section '{section}' returned no parseable blocks")
        logger.error("Section %r produced nothing parseable after %d attempt(s).",
                     section, SECTION_RETRIES + 1)
        return []

    blocks, _ = blocks_from_payload(
        payload, registry, allowed_kinds=kinds, section=section, report=report)
    logger.info("Section %-14s -> %2d block(s), %4d words", section, len(blocks),
                count_words(blocks))
    return blocks


def run_research(
    config: ResearchConfig,
    model: str,
    *,
    settings: Any = None,
    writer: Writer | None = None,
    grounded_fn: Callable[..., GroundedResult] = grounded_generate,
    corpus_articles: list[dict] | None = None,
) -> DeepResearchReport:
    """Produce a complete deep-research report.

    Args:
        config: The {A}..{F} parameters plus the word target.
        model: Model id from config.
        writer: Injected JSON writer (tests pass a fake; default builds a Gemini one).
        grounded_fn: Injected grounded search (tests pass a fake).
        corpus_articles: Optional collected articles, added as Tier A evidence.
    """
    integrity = ResearchIntegrity(target_words=config.word_target)
    store = EvidenceStore()
    if corpus_articles:
        store.add_articles(corpus_articles)

    # -- Phase 1: acquire ------------------------------------------------- #
    store, figures, _texts = acquire_evidence(
        config, model, store=store, grounded_fn=grounded_fn, integrity=integrity)

    if len(store) == 0:
        integrity.degradations.append("no evidence gathered — report cannot be written")
        logger.error("No evidence at all; returning an empty report rather than inventing one.")
        return _finalise([], store, integrity, config, ValidationReport())

    writer = writer or _default_writer(model, settings)
    registry = CitationRegistry(store)
    validation = ValidationReport()
    evidence_block = store.prompt_block()
    plans = {p.section: p for p in plan_sections(config.word_target)}

    # -- Phase 2: body ----------------------------------------------------- #
    # The analysis is split in two: it carries ~58% of the budget and a single call
    # that large truncates (observed live — the entire section was lost).
    analysis_plan = plans["analysis"]
    passes = (
        ("analysis-context", ANALYSIS_A_KINDS, analysis_plan.blocks // 2 + 1),
        ("analysis-outlook", ANALYSIS_B_KINDS, analysis_plan.blocks // 2 + 1),
        ("implications", IMPLICATION_KINDS, plans["implications"].blocks),
    )
    body: list[ResearchBlock] = []
    for section, kinds, n_blocks in passes:
        words_per_block = (analysis_plan.words_per_block if section.startswith("analysis")
                           else plans["implications"].words_per_block)
        prompt = build_section_prompt(
            theme=config.theme, kinds=kinds, n_blocks=n_blocks,
            words_per_block=words_per_block, evidence_block=evidence_block,
            language=config.language, objective=config.objective, sector=config.sector,
            horizon_years=config.horizon_years, key_figures=figures,
            written_so_far=[b.heading for b in body if b.heading],
        )
        body += _write_section(writer, registry, prompt=prompt, kinds=kinds,
                               section=section, report=validation, integrity=integrity)

    # -- Phase 3: synthesis, written LAST from the finished body ----------- #
    synthesis: list[ResearchBlock] = []
    if body:
        summary_lines = [b.heading or b.text[:110] for b in body]
        prompt = build_synthesis_prompt(
            theme=config.theme, body_summary=summary_lines,
            evidence_block=evidence_block, n_blocks=plans["synthesis"].blocks,
            language=config.language)
        synthesis = _write_section(writer, registry, prompt=prompt, kinds=SYNTHESIS_KINDS,
                                   section="synthesis", report=validation, integrity=integrity)
    else:
        integrity.degradations.append("no body produced — synthesis skipped")

    # §1 first in the document, though generated last.
    blocks = synthesis + body
    return _finalise(blocks, store, integrity, config, validation)


def _finalise(
    blocks: list[ResearchBlock],
    store: EvidenceStore,
    integrity: ResearchIntegrity,
    config: ResearchConfig,
    validation: ValidationReport,
) -> DeepResearchReport:
    """Measure, trim to the word band, and assemble the report."""
    from .evidence import TIER_CORPUS, TIER_WEB

    kept, dropped = trim_to_target(blocks, config.word_target, config.word_tolerance)
    if dropped:
        integrity.degradations.append(f"trimmed {dropped} block(s) to meet the word target")

    integrity.evidence_total = len(store)
    integrity.evidence_corpus = len(store.by_tier(TIER_CORPUS))
    integrity.evidence_web = len(store.by_tier(TIER_WEB))
    integrity.publishers = store.publishers()
    integrity.claims_accepted = len(validation.accepted)
    integrity.claims_dropped = validation.dropped_total
    integrity.unresolvable_citations = validation.unknown_evidence_ids
    integrity.actual_words = count_words(kept)

    logger.info(
        "Research report: %d block(s), %d words (target %d±%d) | %s | %s",
        len(kept), integrity.actual_words, config.word_target, config.word_tolerance,
        store.summary(), validation.summary(),
    )
    if not integrity.on_target:
        logger.warning("Word count %d is outside the target band %d±%d.",
                       integrity.actual_words, config.word_target, config.word_tolerance)

    return DeepResearchReport(
        theme=config.theme,
        language=config.language,
        period_label=config.period_label,
        blocks=kept,
        integrity=integrity,
        params={
            "theme": config.theme, "language": config.language,
            "geo_zone": config.geo_zone, "sector": config.sector,
            "objective": config.objective,
            "horizon_past_months": config.months_back,
            "horizon_future_years": config.horizon_years,
        },
    )
