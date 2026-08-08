"""Tests for deep-research generation: budgeting, salvage, assembly, rendering.

Fully offline: the grounded search and the JSON writer are both injected fakes.
"""

from __future__ import annotations

import json

from sentinel.report.research_builder import build_research_context, render_research_html
from sentinel.research.budget import count_words, plan_sections, trim_to_target
from sentinel.research.citations import CitationRegistry
from sentinel.research.contracts import BLOCK_KINDS, PROSPECTIVE_KINDS, ResearchBlock
from sentinel.research.evidence import EvidenceStore
from sentinel.research.grounding import GroundedResult, GroundedSource
from sentinel.research.parse import (
    blocks_from_payload,
    extract_key_figures,
    salvage_json_blocks,
)
from sentinel.research.runner import ResearchConfig, run_research


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _grounded(n_sources=4, text="McKinsey found that 45% of firms adopted AI in 2026."):
    return GroundedResult(
        text=text,
        sources=[GroundedSource(uri=f"https://src/{i}", publisher=f"house{i}.com",
                                title=f"Study {i}", resolved_url=f"https://house{i}.com/study")
                 for i in range(n_sources)],
        queries=["q1", "q2"], finish_reason="STOP", total_tokens=900,
    )


def _block(kind, eid="B1", words=100, prospective=False):
    return {
        "kind": kind, "heading": f"{kind} heading",
        "paragraphs": [] if kind.startswith("key_") else ["word " * words],
        "items": ["a crisp point"] if kind.startswith("key_") else [],
        "evidence": [eid], "prospective": prospective,
        "basis": "extrapolated from the cited trend" if prospective else "",
        "basis_evidence": [eid] if prospective else [], "confidence": "reported",
    }


def _writer_for(sections: dict[str, list[dict]]):
    """A fake writer that returns blocks based on which section is being asked for."""
    def write(prompt: str) -> str:
        if "OPERATIONAL SYNTHESIS" in prompt:
            return json.dumps({"blocks": sections.get("synthesis", [])})
        if "objective_alignment" in prompt or "risk_strategic" in prompt:
            return json.dumps({"blocks": sections.get("implications", [])})
        return json.dumps({"blocks": sections.get("analysis", [])})
    return write


def _config(**kw):
    base = dict(theme="AI in healthcare", language="fr", word_target=600,
                word_tolerance=100, max_facets=1)
    base.update(kw)
    return ResearchConfig(**base)


# --------------------------------------------------------------------------- #
# Budgeting
# --------------------------------------------------------------------------- #
def test_plan_covers_three_sections_and_overshoots():
    plans = plan_sections(3000)
    assert {p.section for p in plans} == {"synthesis", "analysis", "implications"}
    # deliberately allocate above target: trimming is free, expanding costs a call
    assert sum(p.words for p in plans) > 3000
    assert all(p.blocks > 0 and p.words_per_block >= 30 for p in plans)


def test_trim_reduces_to_the_band():
    blocks = [ResearchBlock(kind="use_case", paragraphs=["word " * 200], priority=4)
              for _ in range(6)]
    kept, dropped = trim_to_target(blocks, 400, 50)
    assert dropped > 0
    assert count_words(kept) <= 450


def test_trim_is_a_noop_when_already_inside_the_band():
    blocks = [ResearchBlock(kind="use_case", paragraphs=["word " * 50])]
    kept, dropped = trim_to_target(blocks, 3000, 250)
    assert dropped == 0 and kept == blocks


def test_trim_never_drops_scenarios():
    """Scenarios are mandatory in the spec — the budget may not remove them."""
    blocks = [ResearchBlock(kind="scenario_central", paragraphs=["word " * 300], priority=1)]
    blocks += [ResearchBlock(kind="key_player", paragraphs=["word " * 300], priority=4)
               for _ in range(5)]
    kept, _ = trim_to_target(blocks, 200, 20)
    assert any(b.kind == "scenario_central" for b in kept)


def test_trim_keeps_the_minimum_key_takeaways():
    """The spec demands 5-10 takeaways; trimming must not go below five."""
    blocks = [ResearchBlock(kind="key_takeaway", items=["word " * 40], priority=1)
              for _ in range(8)]
    kept, _ = trim_to_target(blocks, 50, 10)
    assert sum(1 for b in kept if b.kind == "key_takeaway") >= 5


def test_trim_drops_lowest_priority_first():
    important = ResearchBlock(kind="heavy_trend", paragraphs=["word " * 100], priority=2)
    filler = ResearchBlock(kind="key_player", paragraphs=["word " * 100], priority=4)
    kept, _ = trim_to_target([important, filler], 100, 10)
    assert important in kept and filler not in kept


# --------------------------------------------------------------------------- #
# Salvage — what rescues a truncated long response
# --------------------------------------------------------------------------- #
def test_salvage_parses_well_formed_json():
    payload = json.dumps({"blocks": [{"kind": "use_case"}, {"kind": "heavy_trend"}]})
    assert len(salvage_json_blocks(payload)) == 2


def test_salvage_recovers_blocks_from_a_truncated_response():
    """A response cut mid-array must still yield its complete blocks."""
    full = json.dumps({"blocks": [_block("use_case"), _block("heavy_trend"), _block("evolution")]})
    truncated = full[: int(len(full) * 0.72)]          # chopped mid-object
    salvaged = salvage_json_blocks(truncated)
    assert 1 <= len(salvaged) < 3
    assert all(isinstance(b, dict) and "kind" in b for b in salvaged)


def test_salvage_tolerates_code_fences():
    payload = "```json\n" + json.dumps({"blocks": [{"kind": "use_case"}]}) + "\n```"
    assert len(salvage_json_blocks(payload)) == 1


def test_salvage_returns_empty_on_garbage():
    assert salvage_json_blocks("not json at all") == []
    assert salvage_json_blocks("") == []


# --------------------------------------------------------------------------- #
# Block validation
# --------------------------------------------------------------------------- #
def _registry_with_evidence():
    store = EvidenceStore()
    store.add_grounded(GroundedSource(uri="https://mck/1", publisher="mckinsey.com"))
    return CitationRegistry(store), store


def test_unknown_kind_is_dropped():
    """The kind vocabulary is closed, exactly like the canonical topic list."""
    reg, _ = _registry_with_evidence()
    blocks, _ = blocks_from_payload([_block("totally_invented_kind")], reg)
    assert blocks == []


def test_uncited_block_never_reaches_the_report():
    reg, _ = _registry_with_evidence()
    raw = _block("use_case"); raw["evidence"] = []
    blocks, report = blocks_from_payload([raw], reg)
    assert blocks == [] and report.dropped_uncited == 1


def test_block_with_invented_evidence_id_is_dropped():
    reg, _ = _registry_with_evidence()
    raw = _block("use_case", eid="B999")
    blocks, report = blocks_from_payload([raw], reg)
    assert blocks == [] and report.unknown_evidence_ids == 1


def test_prospective_block_is_flagged_for_a_hypothesis_label():
    reg, _ = _registry_with_evidence()
    blocks, _ = blocks_from_payload([_block("scenario_central", prospective=True)], reg)
    assert len(blocks) == 1
    assert blocks[0].prospective and blocks[0].needs_hypothesis_label
    assert blocks[0].basis


def test_scenario_kind_is_prospective_even_if_model_forgets_the_flag():
    reg, _ = _registry_with_evidence()
    raw = _block("scenario_degraded")
    raw["prospective"] = False                      # model omitted it
    raw["basis_evidence"] = ["B1"]
    blocks, _ = blocks_from_payload([raw], reg)
    assert blocks and blocks[0].needs_hypothesis_label


def test_bullet_and_prose_shapes_are_normalised():
    reg, _ = _registry_with_evidence()
    raw = _block("key_takeaway"); raw["paragraphs"] = ["said as a paragraph"]; raw["items"] = []
    blocks, _ = blocks_from_payload([raw], reg)
    assert blocks[0].items and not blocks[0].paragraphs


def test_all_declared_kinds_are_accepted():
    reg, _ = _registry_with_evidence()
    payload = [_block(k) for k in sorted(BLOCK_KINDS)]
    for raw in payload:
        if raw["kind"] in PROSPECTIVE_KINDS:
            raw["basis_evidence"] = ["B1"]
    blocks, _ = blocks_from_payload(payload, reg)
    assert len(blocks) == len(BLOCK_KINDS)


def test_inline_evidence_ids_are_stripped_from_prose():
    """Observed live: the model embeds "[B1], [B2]" in the sentence itself.

    Citations are captured structurally and rendered as publisher links, so leaving
    the raw ids in the prose shows the reader a meaningless code twice over.
    """
    from sentinel.research.parse import strip_inline_citations

    assert strip_inline_citations(
        "Investment accelerated sharply [B1], [B2]. Adoption rose [A3]."
    ) == "Investment accelerated sharply. Adoption rose."
    assert strip_inline_citations("Growth (B7) continued") == "Growth continued"
    # ordinary bracketed text must survive
    assert strip_inline_citations("The [European] market") == "The [European] market"


def test_blocks_have_inline_ids_removed():
    reg, _ = _registry_with_evidence()
    raw = _block("use_case")
    raw["paragraphs"] = ["Adoption grew by 45% last year [B1]."]
    blocks, _ = blocks_from_payload([raw], reg)
    assert blocks[0].paragraphs == ["Adoption grew by 45% last year."]
    assert blocks[0].sources, "the citation itself must still be attached"


def test_key_figures_extracted_for_the_shared_ledger():
    text = ("McKinsey reports that 45% of firms adopted AI. The market reached $12 billion. "
            "Nothing numeric in this one.")
    figures = extract_key_figures(text)
    assert any("45%" in f for f in figures)
    assert any("12 billion" in f for f in figures)


# --------------------------------------------------------------------------- #
# End-to-end runner (fakes only)
# --------------------------------------------------------------------------- #
def _full_sections():
    return {
        "analysis": [_block("heavy_trend"), _block("weak_signal"), _block("use_case"),
                     _block("scenario_central", prospective=True)],
        "implications": [_block("opportunity"), _block("risk_regulatory"),
                         _block("implication_skills")],
        "synthesis": [_block("key_takeaway") for _ in range(6)] + [_block("critical_risk")],
    }


def test_run_research_produces_all_three_sections():
    report = run_research(_config(), "test-model",
                          writer=_writer_for(_full_sections()),
                          grounded_fn=lambda *a, **k: _grounded())
    assert report.of_section("synthesis")
    assert report.of_section("analysis")
    assert report.of_section("implications")
    assert report.integrity.evidence_web == 4
    assert report.integrity.llm_calls >= 4        # 1 acquire + 3 writes


def test_synthesis_is_generated_last_but_placed_first():
    """Order matters: §1 must be derived from the finished body, yet printed first."""
    seen: list[str] = []

    def writer(prompt: str) -> str:
        which = "synthesis" if "OPERATIONAL SYNTHESIS" in prompt else (
            "implications" if "risk_strategic" in prompt else "analysis")
        seen.append(which)
        return json.dumps({"blocks": _full_sections()[which]})

    report = run_research(_config(), "m", writer=writer, grounded_fn=lambda *a, **k: _grounded())
    assert seen[-1] == "synthesis"                       # generated last
    assert report.blocks[0].section == "synthesis"       # rendered first


def test_run_research_without_evidence_returns_empty_rather_than_inventing():
    """No sources must mean no report — never an unsourced one."""
    empty = GroundedResult(text="from memory", sources=[])
    report = run_research(_config(), "m", writer=_writer_for(_full_sections()),
                          grounded_fn=lambda *a, **k: empty)
    assert report.blocks == []
    assert any("no evidence" in d for d in report.integrity.degradations)


def test_grounding_failure_degrades_without_crashing():
    def boom(*a, **k):
        raise RuntimeError("quota exhausted")

    report = run_research(_config(), "m", writer=_writer_for(_full_sections()), grounded_fn=boom)
    assert report.blocks == []
    assert any("failed" in d for d in report.integrity.degradations)


def test_writer_failure_still_produces_what_survived():
    calls = {"n": 0}

    def flaky(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("section 1 died")
        return json.dumps({"blocks": _full_sections()["implications"]})

    report = run_research(_config(), "m", writer=flaky, grounded_fn=lambda *a, **k: _grounded())
    assert report.blocks                                     # something survived
    assert any("failed" in d for d in report.integrity.degradations)


def test_analysis_is_written_in_two_passes():
    """§2 carries ~58% of the budget; one call that large truncates (seen live)."""
    prompts: list[str] = []

    def writer(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"blocks": _full_sections()["analysis"]})

    run_research(_config(), "m", writer=writer, grounded_fn=lambda *a, **k: _grounded())
    # context pass offers weak_signal but not scenarios; outlook pass is the reverse
    context_pass = [p for p in prompts if '"weak_signal"' in p and '"scenario_central"' not in p]
    outlook_pass = [p for p in prompts if '"scenario_central"' in p and '"weak_signal"' not in p]
    assert context_pass and outlook_pass, "analysis must be split into two distinct calls"


def test_unparseable_section_is_reasked_once():
    """A section returning junk gets one more chance before being abandoned."""
    calls = {"n": 0}

    def flaky(prompt: str) -> str:
        if "OPERATIONAL SYNTHESIS" in prompt:
            return json.dumps({"blocks": _full_sections()["synthesis"]})
        calls["n"] += 1
        if calls["n"] == 1:
            return "<<not json at all>>"          # first attempt unusable
        return json.dumps({"blocks": _full_sections()["analysis"]})

    report = run_research(_config(), "m", writer=flaky, grounded_fn=lambda *a, **k: _grounded())
    assert calls["n"] >= 2, "the section should have been re-asked"
    assert report.of_section("analysis"), "the retry's output should be used"


def test_corpus_articles_become_tier_a_evidence():
    report = run_research(
        _config(), "m", writer=_writer_for(_full_sections()),
        grounded_fn=lambda *a, **k: _grounded(),
        corpus_articles=[{"url": "https://news/1", "title": "T", "source": "TechCrunch"}])
    assert report.integrity.evidence_corpus == 1
    assert report.integrity.evidence_web == 4


def test_report_is_trimmed_towards_the_word_target():
    big = {k: [_block(kind, words=300) for kind in v_kinds]
           for k, v_kinds in (("analysis", ["heavy_trend", "use_case", "evolution"]),
                              ("implications", ["opportunity", "risk_strategic"]),
                              ("synthesis", ["key_takeaway"]))}
    report = run_research(_config(word_target=300, word_tolerance=50), "m",
                          writer=_writer_for(big), grounded_fn=lambda *a, **k: _grounded())
    assert report.integrity.actual_words <= 350


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _rendered(language="fr"):
    report = run_research(_config(language=language), "m",
                          writer=_writer_for(_full_sections()),
                          grounded_fn=lambda *a, **k: _grounded())
    ctx = build_research_context(report, generated_at="2026-08-08T09:00:00+00:00")
    return report, ctx, render_research_html(ctx)


def test_render_contains_the_three_mandatory_sections_in_french():
    _, _, html = _rendered("fr")
    assert "1. Synthèse opérationnelle" in html
    assert "2. Analyse détaillée" in html
    assert "3. Opportunités, risques et implications stratégiques" in html


def test_render_in_english():
    _, _, html = _rendered("en")
    assert "1. Operational Synthesis" in html
    assert "2. Detailed Analysis" in html


def test_prospective_content_is_visibly_labelled():
    """A forecast must never read as established fact."""
    _, _, html = _rendered("fr")
    assert "HYPOTHÈSE" in html


def test_methodology_footer_exposes_the_counts():
    _, ctx, html = _rendered("fr")
    assert "Méthodologie et traçabilité" in html
    assert str(ctx["integrity"].evidence_total) in html
    assert "supprimé" in html            # the "dropped, not reworded" statement


def test_sources_are_listed_and_deduplicated():
    _, ctx, html = _rendered()
    urls = [s["url"] for s in ctx["sources"]]
    assert len(urls) == len(set(urls))
    assert "house0.com" in html


def test_no_scripts_or_external_assets_in_output():
    """Requirement: clean HTML, email-pasteable, no scripts or complex CSS."""
    _, _, html = _rendered()
    lowered = html.lower()
    assert "<script" not in lowered
    assert "<style" not in lowered
    assert "<img" not in lowered


def test_llm_text_is_escaped_not_rendered_as_html():
    reg, store = _registry_with_evidence()
    from sentinel.research.contracts import DeepResearchReport, ResearchIntegrity

    blocks, _ = blocks_from_payload(
        [{"kind": "use_case", "heading": "h", "paragraphs": ["<script>alert(1)</script>"],
          "items": [], "evidence": ["B1"]}], reg)
    report = DeepResearchReport(theme="t", blocks=blocks, integrity=ResearchIntegrity())
    html = render_research_html(build_research_context(report, generated_at="x"))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
