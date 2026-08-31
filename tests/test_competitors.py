"""Tests for the competitor comparison report. Fully offline — search and writer
are injected fakes.
"""

from __future__ import annotations

import json

import pytest

from types import SimpleNamespace

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


def _name_writer(*names, raw=None):
    """Stands in for the discovery name-extraction call."""
    def write(prompt: str) -> str:
        if raw is not None:
            return raw
        return json.dumps({"blocks": [{"name": n, "sells": "things"} for n in names]})
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
def _discover(text="Some prose about rivals.", writer=None, **kw):
    return discover_competitors(
        COMPANY, "m", grounded_fn=lambda *a, **k: _grounded(text=text),
        writer=writer if writer is not None else _name_writer("Acme"), **kw)


def test_names_come_from_the_structured_field_not_the_prose():
    names, _ = _discover(writer=_name_writer("Relevance AI", "n8n"))
    assert "Relevance AI" in names and "n8n" in names


def test_a_field_label_can_no_longer_become_a_competitor():
    """Regression, observed live. A regex over the discovery prose matched the
    model's own sub-headings and produced a dossier for "What it sells" — two
    grounded calls spent on a company that does not exist, and nothing downstream
    could catch it, because the citation tiers verify that sources are real, not
    that the subject is. Only the structured "name" field counts now, so prose
    formatting cannot leak a heading into the competitor list."""
    prose = ("### Sana Labs\n"
             "- **What it sells:** AI learning agents\n"
             "- **Why they compete:** same buyers\n")
    names, _ = _discover(text=prose, writer=_name_writer("Sana Labs"))
    assert names == ["CrewAI", "Relevance AI", "Sana Labs"]
    assert "What it sells" not in names


def test_declared_competitors_always_survive_discovery():
    """A rival the team already tracks must never be lost to a weak search."""
    names, _ = _discover(text="")
    assert "CrewAI" in names and "Relevance AI" in names


def test_an_empty_search_skips_extraction_entirely():
    """No prose means there is nothing to extract, so no second call to pay for."""
    def explode(prompt):
        raise AssertionError("extraction must not run on empty prose")

    names, _ = _discover(text="", writer=explode)
    assert names == ["CrewAI", "Relevance AI"]


def test_our_own_company_is_never_a_competitor():
    names, _ = _discover(writer=_name_writer("Welyne", "Acme"))
    assert "Welyne" not in names
    assert "Acme" in names


def test_discovery_failure_falls_back_to_the_declared_list():
    def boom(*a, **k):
        raise RuntimeError("search down")

    names, _ = discover_competitors(COMPANY, "m", grounded_fn=boom,
                                    writer=_name_writer("Never"))
    assert names == ["CrewAI", "Relevance AI"]


def test_unparseable_extraction_keeps_the_declared_list():
    """The report is still worth producing from what the team declared."""
    names, _ = _discover(writer=_name_writer(raw="not json at all"))
    assert names == ["CrewAI", "Relevance AI"]


def test_an_extraction_crash_never_kills_the_report():
    def boom(prompt):
        raise RuntimeError("writer down")

    names, _ = _discover(writer=boom)
    assert names == ["CrewAI", "Relevance AI"]


def test_discovery_respects_the_max():
    names, _ = _discover(max_n=4,
                         writer=_name_writer(*[f"Rival{i}" for i in range(1, 20)]))
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


# --------------------------------------------------------------------------- #
# The CLI's --profile flag
#
# Without it a client could not preview what their request file will produce:
# they would have to retype max_competitors on the command line, which is exactly
# the drift the profile exists to prevent.
# --------------------------------------------------------------------------- #
def _cli(monkeypatch, argv, *, company=None):
    """Run the competitors CLI against fakes; return (exit_code, captured kwargs)."""
    import sys
    from pathlib import Path

    import sentinel.competitors.__main__ as cli
    import sentinel.report.competitor_builder as builder_mod
    import sentinel.research.company as company_mod
    import sentinel.research.competitors as competitors_mod

    seen: dict = {}

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(
        llm=SimpleNamespace(gemini_model="m"), app=SimpleNamespace(name="Sentinel")))
    monkeypatch.setattr(cli, "require_secrets", lambda names: None)
    monkeypatch.setattr(cli, "load_company",
                        lambda ref=None: (seen.setdefault("company_ref", ref),
                                          company or COMPANY)[1])

    def fake_run(comp, model, *, max_competitors=None, period_label="", **kw):
        seen["max"] = max_competitors
        seen["period"] = period_label
        seen["language"] = comp.language
        return SimpleNamespace(dossiers=[SimpleNamespace(
            threat_level="high", name="X", strengths=[], weaknesses=[], risks=[])],
            top_threats=[], action_plan=[],
            integrity=SimpleNamespace(
                evidence_total=1, claims_accepted=1, claims_dropped=0,
                unresolvable_citations=0, llm_calls=4, degradations=[]))

    monkeypatch.setattr(cli, "run_competitor_report", fake_run)
    monkeypatch.setattr(cli, "write_competitor_report",
                        lambda *a, **kw: (seen.setdefault("slug", kw.get("slug")),
                                          (Path("out.html"), "<html></html>"))[1])
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    return cli._main(), seen


def test_profile_supplies_the_competitor_count_and_period(monkeypatch, tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(
        "slug: my_scan\nreport_type: competitor_scan\ntheme: Rivals\n"
        "cadence: quarterly\nlanguage: en\n"
        "company_ref: profiles/company.yaml\nmax_competitors: 3\n",
        encoding="utf-8")

    code, seen = _cli(monkeypatch, ["--profile", str(path), "--dry-run"])

    assert code == 0
    assert seen["max"] == 3                     # from the profile, not the default
    assert seen["period"].endswith(("Q1", "Q2", "Q3", "Q4"))   # quarterly cadence
    assert seen["slug"] == "my_scan"            # same filename the dispatcher writes
    assert seen["company_ref"] == "profiles/company.yaml"
    assert seen["language"] == "en"


def test_an_explicit_flag_still_beats_the_profile(monkeypatch, tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(
        "slug: my_scan\nreport_type: competitor_scan\ntheme: Rivals\n"
        "cadence: quarterly\ncompany_ref: profiles/company.yaml\nmax_competitors: 3\n",
        encoding="utf-8")

    _, seen = _cli(monkeypatch, ["--profile", str(path), "--max", "7", "--dry-run"])
    assert seen["max"] == 7


def test_without_a_profile_the_engine_default_applies(monkeypatch):
    from sentinel.research.competitors import MAX_COMPETITORS

    _, seen = _cli(monkeypatch, ["--dry-run"])
    assert seen["max"] == MAX_COMPETITORS
    assert seen["slug"] == "competitors"


def test_a_non_competitor_profile_is_refused_before_spending_quota(monkeypatch, tmp_path):
    """A deep_research profile here would silently produce the wrong report."""
    path = tmp_path / "r.yaml"
    path.write_text("slug: research_one\nreport_type: deep_research\ntheme: AI\n",
                    encoding="utf-8")

    code, seen = _cli(monkeypatch, ["--profile", str(path), "--dry-run"])
    assert code == 1
    assert "max" not in seen        # nothing was generated
