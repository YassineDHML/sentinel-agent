"""Tests for the research layer: grounding extraction, evidence store, citation tiers.

Fully offline. The fake response objects mirror the **real observed shape** of a
grounded Gemini response (verified live, Aug 2026) — in particular that
``web.domain`` is ``None`` and the publisher lands in ``web.title``.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from sentinel.research import (
    TIER_CORPUS,
    TIER_PROSPECTIVE,
    TIER_WEB,
    CitationRegistry,
    EvidenceStore,
    GroundedSource,
)
from sentinel.research import grounding as gmod


# --------------------------------------------------------------------------- #
# Fakes shaped like the real SDK response
# --------------------------------------------------------------------------- #
def _web(uri, title, domain=None):
    return SimpleNamespace(uri=uri, title=title, domain=domain)


def _response(*, chunks=(), queries=(), text="answer", finish="STOP",
              total=100, thoughts=20, with_metadata=True):
    meta = SimpleNamespace(
        grounding_chunks=[SimpleNamespace(web=w) for w in chunks],
        web_search_queries=list(queries),
        grounding_supports=[],
    ) if with_metadata else None
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(
            finish_reason=SimpleNamespace(name=finish), grounding_metadata=meta)],
        usage_metadata=SimpleNamespace(total_token_count=total, thoughts_token_count=thoughts),
    )


class FakeClient:
    """Captures the config it was called with, returns a scripted response."""

    def __init__(self, response):
        self._response = response
        self.captured = {}
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, *, model, contents, config):
        self.captured = {"model": model, "contents": contents, "config": config}
        return self._response


# --------------------------------------------------------------------------- #
# Grounding extraction
# --------------------------------------------------------------------------- #
def test_publisher_read_from_title_when_domain_is_none():
    """Observed live: every chunk had domain=None and the domain string in title."""
    assert gmod._publisher_of(_web("u", "mckinsey.com")) == "mckinsey.com"


def test_publisher_prefers_domain_when_present():
    assert gmod._publisher_of(_web("u", "Some Article Title", domain="bcg.com")) == "bcg.com"


def test_extract_builds_sources_and_queries():
    resp = _response(
        chunks=[_web("https://vertexaisearch.cloud.google.com/x/1", "mckinsey.com"),
                _web("https://vertexaisearch.cloud.google.com/x/2", "bcg.com")],
        queries=["McKinsey AI adoption", "BCG AI healthcare"],
    )
    out = gmod._extract(resp, resolve_urls=False)
    assert out.grounded is True
    assert [s.publisher for s in out.sources] == ["mckinsey.com", "bcg.com"]
    assert out.queries == ["McKinsey AI adoption", "BCG AI healthcare"]
    assert out.publishers() == ["mckinsey.com", "bcg.com"]


def test_no_grounding_metadata_means_not_grounded():
    """The model may answer from memory — that must never look like evidence."""
    out = gmod._extract(_response(with_metadata=False), resolve_urls=False)
    assert out.grounded is False
    assert out.sources == []


def test_chunks_without_web_are_skipped():
    resp = _response()
    resp.candidates[0].grounding_metadata.grounding_chunks = [
        SimpleNamespace(web=None),                      # e.g. a maps/image chunk
        SimpleNamespace(web=_web("https://x/1", "idc.com")),
    ]
    out = gmod._extract(resp, resolve_urls=False)
    assert [s.publisher for s in out.sources] == ["idc.com"]


def test_truncation_is_detected():
    out = gmod._extract(_response(finish="MAX_TOKENS"), resolve_urls=False)
    assert out.truncated is True


def test_extract_survives_bare_response():
    out = gmod._extract(SimpleNamespace(text="hi"), resolve_urls=False)
    assert out.text == "hi" and out.grounded is False


def test_grounded_generate_never_sets_json_mode(monkeypatch):
    """JSON mode + search tool is rejected by the API — it must never be sent."""
    client = FakeClient(_response(chunks=[_web("https://x/1", "gartner.com")]))
    gmod.grounded_generate("p", "test-model", client=client, resolve_urls=False)
    cfg = client.captured["config"]
    assert getattr(cfg, "response_mime_type", None) is None
    assert cfg.tools, "the search tool must be attached"


def test_grounded_generate_strips_microseconds_from_time_filter():
    """Sub-second precision is rejected: 'Granularity of nano is not supported'."""
    client = FakeClient(_response())
    now = dt.datetime(2026, 8, 4, 12, 30, 45, 123456, tzinfo=dt.timezone.utc)
    gmod.grounded_generate("p", "m", client=client, resolve_urls=False,
                           since=now - dt.timedelta(days=90), until=now)
    interval = client.captured["config"].tools[0].google_search.time_range_filter
    assert interval.start_time.microsecond == 0
    assert interval.end_time.microsecond == 0


def test_grounded_generate_uses_positive_thinking_budget():
    """thinking_budget=0 is rejected by the model; a small positive cap is used."""
    client = FakeClient(_response())
    gmod.grounded_generate("p", "m", client=client, resolve_urls=False)
    assert client.captured["config"].thinking_config.thinking_budget > 0


def test_thinking_config_omitted_when_none():
    client = FakeClient(_response())
    gmod.grounded_generate("p", "m", client=client, resolve_urls=False, thinking_budget=None)
    assert getattr(client.captured["config"], "thinking_config", None) is None


# --------------------------------------------------------------------------- #
# Evidence store
# --------------------------------------------------------------------------- #
def test_ids_are_tier_prefixed_and_sequential():
    store = EvidenceStore()
    a = store.add_article({"url": "https://n/1", "title": "T", "source": "TechCrunch"})
    b = store.add_grounded(GroundedSource(uri="https://g/1", publisher="mckinsey.com"))
    assert a.eid == "A1" and a.tier == TIER_CORPUS
    assert b.eid == "B1" and b.tier == TIER_WEB


def test_same_url_reuses_one_id():
    store = EvidenceStore()
    first = store.add_article({"url": "https://n/1", "title": "T"})
    again = store.add_article({"url": "https://n/1", "title": "T"})
    assert first.eid == again.eid and len(store) == 1


def test_grounded_evidence_prefers_resolved_publisher_url():
    """Redirect shells can expire; the durable URL is stored when available."""
    store = EvidenceStore()
    e = store.add_grounded(GroundedSource(
        uri="https://vertexaisearch.cloud.google.com/redirect/abc",
        publisher="mckinsey.com",
        resolved_url="https://www.mckinsey.com/insights/state-of-ai"))
    assert e.url == "https://www.mckinsey.com/insights/state-of-ai"
    assert e.label() == "mckinsey.com"


def test_publisher_inferred_from_url_when_missing():
    store = EvidenceStore()
    e = store.add_grounded(GroundedSource(uri="https://www.bcg.com/pub", publisher=""))
    assert e.label() == "bcg.com"


def test_urlless_evidence_is_ignored():
    store = EvidenceStore()
    assert store.add_article({"title": "no url"}) is None
    assert len(store) == 0


def test_lookup_is_case_insensitive_and_reports_unknown():
    store = EvidenceStore()
    store.add_article({"url": "https://n/1"})
    assert store.get("a1") is not None
    assert store.get("Z9") is None and store.known("Z9") is False


def test_prompt_block_exposes_ids_not_raw_urls():
    store = EvidenceStore()
    store.add_grounded(GroundedSource(uri="https://g/1", publisher="idc.com", title="IDC study"))
    block = store.prompt_block()
    assert "[B1]" in block and "idc.com" in block


# --------------------------------------------------------------------------- #
# Citation policy — the heart of the anti-hallucination guarantee
# --------------------------------------------------------------------------- #
def _store_with_both():
    store = EvidenceStore()
    store.add_article({"url": "https://news/1", "title": "Corpus article", "source": "TechCrunch"})
    store.add_grounded(GroundedSource(uri="https://mck/1", publisher="mckinsey.com"))
    return store


def test_tier_a_claim_citing_corpus_article():
    reg = CitationRegistry(_store_with_both())
    claim = reg.validate_claim("Something happened.", ["A1"])
    assert claim is not None and claim.tier == TIER_CORPUS


def test_tier_b_claim_citing_retrieved_web_source():
    """This is what makes 'cite McKinsey' possible at all."""
    reg = CitationRegistry(_store_with_both())
    claim = reg.validate_claim("McKinsey reports rising adoption.", ["B1"])
    assert claim is not None and claim.tier == TIER_WEB
    assert claim.evidence[0].label() == "mckinsey.com"


def test_invented_evidence_id_is_dropped():
    """A hallucinated citation cannot resolve, so the claim never reaches the report."""
    from sentinel.research.citations import ValidationReport

    reg = CitationRegistry(_store_with_both())
    rep = ValidationReport()
    assert reg.validate_claim("Invented.", ["B99"], report=rep) is None
    assert rep.unknown_evidence_ids == 1


def test_uncited_claim_is_dropped():
    from sentinel.research.citations import ValidationReport

    reg = CitationRegistry(_store_with_both())
    rep = ValidationReport()
    assert reg.validate_claim("No source at all.", [], report=rep) is None
    assert rep.dropped_uncited == 1


def test_projection_requires_an_anchor():
    """Tier C exists so forecasts are possible — but never free-floating."""
    from sentinel.research.citations import ValidationReport

    reg = CitationRegistry(_store_with_both())
    rep = ValidationReport()
    assert reg.validate_claim("By 2030 everything changes.", [], prospective=True,
                              report=rep) is None
    assert rep.dropped_unanchored_projection == 1


def test_anchored_projection_is_accepted_and_labelled():
    reg = CitationRegistry(_store_with_both())
    claim = reg.validate_claim(
        "Adoption should double by 2030.", [], prospective=True,
        basis="extrapolating the reported growth rate", basis_eids=["B1"])
    assert claim is not None
    assert claim.tier == TIER_PROSPECTIVE
    assert claim.needs_hypothesis_label is True
    assert claim.basis


def test_projection_cannot_claim_established_fact():
    """A forecast presented as settled fact is downgraded, not published as-is."""
    from sentinel.research.citations import ValidationReport

    reg = CitationRegistry(_store_with_both())
    rep = ValidationReport()
    claim = reg.validate_claim("Will certainly happen.", ["A1"], prospective=True,
                               confidence="established", report=rep)
    assert claim.confidence == "estimated"
    assert rep.downgraded_confidence == 1


def test_partially_valid_citations_keep_the_good_ones():
    from sentinel.research.citations import ValidationReport

    reg = CitationRegistry(_store_with_both())
    rep = ValidationReport()
    claim = reg.validate_claim("Mixed.", ["A1", "B404"], report=rep)
    assert claim is not None
    assert [e.eid for e in claim.evidence] == ["A1"]
    assert rep.unknown_evidence_ids == 1


def test_duplicate_ids_are_deduplicated():
    reg = CitationRegistry(_store_with_both())
    claim = reg.validate_claim("x", ["A1", "A1", "a1"])
    assert len(claim.evidence) == 1


def test_validate_many_reports_totals():
    reg = CitationRegistry(_store_with_both())
    rep = reg.validate_many([
        {"text": "good", "evidence": ["A1"]},
        {"text": "bad", "evidence": ["ZZ"]},
        {"text": "uncited", "evidence": []},
        {"text": "forecast", "evidence": [], "prospective": True, "basis_evidence": ["B1"]},
    ], section="analysis")
    assert len(rep.accepted) == 2                 # "good" + the anchored forecast
    assert rep.unknown_evidence_ids == 1          # the single invented id "ZZ"
    # two CLAIMS dropped: the one citing only an invented id, and the uncited one.
    # The counters use different units and must never be summed together.
    assert rep.dropped_uncited == 2
    assert rep.dropped_total == 2
    assert "accepted" in rep.summary()


def test_claim_exposes_sources_for_the_template():
    reg = CitationRegistry(_store_with_both())
    claim = reg.validate_claim("x", ["B1"])
    src = claim.sources_for_template()[0]
    assert src["label"] == "mckinsey.com" and src["tier"] == TIER_WEB
