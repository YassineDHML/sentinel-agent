"""Tests for the competitor comparison report. Fully offline — search and writer
are injected fakes.
"""

from __future__ import annotations

import json

import pytest

from sentinel.config import ConfigError
from sentinel.report.competitor_builder import (
    build_competitor_context,
    render_competitor_html,
)
from sentinel.research.company import CompanyProfile, load_company, profile_from_dict
from sentinel.research.competitors import (
    THREAT_LEVELS,
    discover_competitors,
    run_competitor_report,
    _names_from_text,
)
from sentinel.research.grounding import GroundedResult, GroundedSource

COMPANY = CompanyProfile(
    name="Welyne", one_liner="AI agents for business functions", sector="AI services",
    offer=["AI Commandos"], positioning="specialist agency",
    target_customers=["mid-market"], known_competitors=["CrewAI", "Relevance AI"],
    excluded_names=["Welyne"], language="fr",
)


def _grounded(n=3, text="Findings about the competitor."):
    return GroundedResult(
        text=text,
        sources=[GroundedSource(uri=f"https://s/{i}", publisher=f"src{i}.com",
                                title=f"Doc {i}", resolved_url=f"https://src{i}.com/a")
                 for i in range(n)],
        queries=["q"], finish_reason="STOP")


def _dossier_payload(threat="high", with_traction=True):
    blocks = [
        {"kind": "profile", "threat_level": threat, "positioning": "tech",
         "description": "A rival platform.", "main_offer": "Agent builder",
         "marketing_positioning": "developer-first", "target_customer": "engineering teams",
         "evidence": ["B1"]},
        {"kind": "strength", "text": "Strong developer community", "evidence": ["B1"]},
        {"kind": "weakness", "text": "Weak enterprise support", "evidence": ["B2"]},
        {"kind": "risk", "text": "Could undercut us on price", "horizon": "short",
         "evidence": ["B1"]},
        {"kind": "opportunity", "text": "We can win on ready-made agents", "evidence": ["B2"]},
    ]
    if with_traction:
        blocks.append({"kind": "traction", "text": "Raised $18M in 2026", "evidence": ["B3"]})
    return blocks


def _writer(dossier_blocks=None, synthesis_blocks=None):
    def write(prompt: str) -> str:
        if "strategic synthesis" in prompt.lower():
            return json.dumps({"blocks": synthesis_blocks if synthesis_blocks is not None else [
                {"kind": "top_threat", "text": "CrewAI is closest to our offer",
                 "evidence": ["B1"]},
                {"kind": "action", "text": "Ship vertical agents faster", "evidence": ["B2"]},
            ]})
        return json.dumps({"blocks": dossier_blocks if dossier_blocks is not None
                           else _dossier_payload()})
    return write


# --------------------------------------------------------------------------- #
# Company profile
# --------------------------------------------------------------------------- #
def test_company_profile_requires_a_name():
    with pytest.raises(ConfigError):
        profile_from_dict({"one_liner": "no name"})


def test_shipped_company_profile_is_valid():
    profile = load_company()
    assert profile.name and profile.known_competitors


def test_company_prompt_block_covers_the_essentials():
    block = COMPANY.prompt_block()
    assert "Welyne" in block and "AI Commandos" in block and "mid-market" in block


def test_missing_company_file_explains_itself():
    with pytest.raises(ConfigError, match="who"):
        load_company("profiles/does_not_exist.yaml")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_names_extracted_from_all_common_list_formats():
    text = ("1. Relevance AI — no-code agents\n"
            "2. **CrewAI**: orchestration\n"
            "- Zapier Agents - automation\n"
            "* n8n — open source\n")
    assert _names_from_text(text) == ["Relevance AI", "CrewAI", "Zapier Agents", "n8n"]


def test_declared_competitors_always_survive_discovery():
    """A rival the team already tracks must never be lost to a weak search."""
    names, _ = discover_competitors(COMPANY, "m", grounded_fn=lambda *a, **k: _grounded(text=""))
    assert "CrewAI" in names and "Relevance AI" in names


def test_our_own_company_is_never_a_competitor():
    names, _ = discover_competitors(
        COMPANY, "m",
        grounded_fn=lambda *a, **k: _grounded(text="1. Welyne — us\n2. Acme — rival\n"))
    assert "Welyne" not in names
    assert "Acme" in names


def test_discovery_failure_falls_back_to_the_declared_list():
    def boom(*a, **k):
        raise RuntimeError("search down")

    names, _ = discover_competitors(COMPANY, "m", grounded_fn=boom)
    assert names == ["CrewAI", "Relevance AI"]


def test_discovery_respects_the_max():
    many = "\n".join(f"{i}. Rival{i} — does things" for i in range(1, 20))
    names, _ = discover_competitors(COMPANY, "m", max_n=4,
                                    grounded_fn=lambda *a, **k: _grounded(text=many))
    assert len(names) == 4


# --------------------------------------------------------------------------- #
# Dossiers
# --------------------------------------------------------------------------- #
def _run(**kw):
    return run_competitor_report(
        COMPANY, "m", writer=kw.pop("writer", _writer()),
        grounded_fn=kw.pop("grounded_fn", lambda *a, **k: _grounded()), **kw)


def test_report_builds_a_dossier_per_competitor():
    report = _run(max_competitors=2)
    assert len(report.dossiers) == 2
    d = report.dossiers[0]
    assert d.strengths and d.weaknesses and d.risks and d.opportunities
    assert d.main_offer == "Agent builder"


def test_threat_levels_are_validated_against_the_closed_set():
    report = _run(max_competitors=1, writer=_writer(_dossier_payload(threat="apocalyptic")))
    assert report.dossiers[0].threat_level in THREAT_LEVELS


def test_dossiers_are_ranked_most_dangerous_first():
    order = ["low", "high"]

    def writer(prompt: str) -> str:
        if "strategic synthesis" in prompt.lower():
            return json.dumps({"blocks": []})
        return json.dumps({"blocks": _dossier_payload(threat=order.pop(0) if order else "low")})

    report = _run(max_competitors=2, writer=writer)
    assert report.dossiers[0].threat_level == "high"


def test_unsourced_traction_is_omitted_not_estimated():
    """A market-share figure with no source must not appear at all."""
    payload = _dossier_payload(with_traction=False)
    payload.append({"kind": "traction", "text": "Probably about 20% share", "evidence": []})
    report = _run(max_competitors=1, writer=_writer(payload))
    assert report.dossiers[0].traction is None


def test_sourced_traction_is_kept():
    report = _run(max_competitors=1)
    assert report.dossiers[0].traction is not None
    assert "18M" in report.dossiers[0].traction.text


def test_competitor_without_sources_is_skipped_not_guessed():
    ungrounded = GroundedResult(text="I think they do X", sources=[])
    report = _run(max_competitors=2, grounded_fn=lambda *a, **k: ungrounded)
    assert report.dossiers == []
    assert any("no competitor" in d for d in report.integrity.degradations)


def test_one_failing_competitor_does_not_lose_the_report():
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:                      # the first dossier research fails
            raise RuntimeError("rate limited")
        return _grounded()

    report = _run(max_competitors=2, grounded_fn=flaky)
    assert len(report.dossiers) == 1             # the other survived


def test_inline_evidence_ids_are_stripped_from_dossier_text():
    payload = _dossier_payload()
    payload[1]["text"] = "Strong community [B1], [B2]."
    report = _run(max_competitors=1, writer=_writer(payload))
    assert report.dossiers[0].strengths[0].text == "Strong community."


def test_synthesis_produces_top_threats_and_actions():
    report = _run(max_competitors=1)
    assert report.top_threats and report.action_plan


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _rendered(language="fr", **kw):
    """Render and return ``(report, context, html, text)``.

    ``text`` is the HTML with entities decoded: autoescaping turns an apostrophe
    into ``&#39;``, so French labels like "Plan d'action" never match the raw HTML
    even though they display correctly.
    """
    import dataclasses
    import html as html_mod

    company = dataclasses.replace(COMPANY, language=language)
    report = run_competitor_report(
        company, "m", writer=kw.get("writer", _writer()),
        grounded_fn=lambda *a, **k: _grounded(), max_competitors=kw.get("max_competitors", 2))
    ctx = build_competitor_context(report, generated_at="2026-08-08T09:00:00+00:00")
    rendered = render_competitor_html(ctx)
    return report, ctx, rendered, html_mod.unescape(rendered)


def test_render_has_the_three_sections_in_french():
    *_, text = _rendered("fr")
    assert "1. Concurrents classés par niveau de menace" in text
    assert "2. Dossiers par concurrent" in text
    assert "3. Synthèse stratégique" in text
    assert "Plan d'action recommandé" in text


def test_render_in_english():
    *_, text = _rendered("en")
    assert "Competitors ranked by threat" in text
    assert "Recommended action plan" in text


def test_threat_badges_are_rendered():
    *_, text = _rendered("fr")
    assert "ÉLEVÉE" in text or "MOYENNE" in text or "FAIBLE" in text


def test_render_lists_every_dossier_with_its_sections():
    *_, text = _rendered("fr")
    assert "CrewAI" in text
    assert "Forces" in text and "Faiblesses" in text
    assert "Risques pour nous" in text and "Angles d'attaque" in text


def test_methodology_states_figures_are_never_estimated():
    *_, text = _rendered("fr")
    assert "Méthodologie" in text
    assert "jamais estimés" in text


def test_output_is_email_safe():
    _, _, html, _ = _rendered()
    low = html.lower()
    assert "<script" not in low and "<style" not in low and "<img" not in low


def test_llm_text_is_escaped():
    payload = _dossier_payload()
    payload[1]["text"] = "<script>alert(1)</script>"
    _, _, html, _ = _rendered(writer=_writer(payload), max_competitors=1)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
