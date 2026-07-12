"""Tests for the Gemini-only deep analysis: prompt building + anti-hallucination
citation validation. LLM is mocked via a fake provider — no live calls.
"""

from __future__ import annotations

import pytest

from sentinel.analyze.deep_analysis import (
    DeepAnalysis,
    build_deep_analysis_prompt,
    parse_deep_analysis_response,
    run_deep_analysis,
)
from sentinel.analyze.llm import LLMClient, LLMError

ARTICLES = [
    {"url": "https://a.com/1", "title": "OpenAI ships GPT-5", "actor": "OpenAI",
     "topics": ["LLM release"], "summary": "OpenAI released GPT-5 today."},
    {"url": "https://a.com/2", "title": "Mistral raises funding", "actor": "Mistral AI",
     "topics": ["funding and investment"], "summary": "Mistral AI closed a funding round."},
]


class FakeProvider:
    def __init__(self, name, response):
        self.name = name
        self._response = response

    def generate(self, prompt):
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def test_prompt_includes_language_digest_and_articles():
    prompt = build_deep_analysis_prompt(ARTICLES, "DIGEST HERE", report_language="fr")
    assert "French" in prompt
    assert "DIGEST HERE" in prompt
    assert "[1] TITLE: OpenAI ships GPT-5" in prompt
    assert "[2] TITLE: Mistral raises funding" in prompt
    assert "anti-hallucination" in prompt.lower()


def test_prompt_default_language_is_english():
    prompt = build_deep_analysis_prompt(ARTICLES, "digest", report_language="en")
    assert "English" in prompt


# --------------------------------------------------------------------------- #
# Citation validation (the hard anti-hallucination requirement)
# --------------------------------------------------------------------------- #
def test_parse_resolves_valid_citations_to_urls():
    text = '''{"executive_summary": [{"text": "OpenAI shipped GPT-5.", "sources": [1]}],
               "competitive_watch": [{"actor": "OpenAI", "highlights": "shipped GPT-5", "sources": [1]}],
               "opportunities": [{"text": "Consider GPT-5.", "sources": [1, 2]}]}'''
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.executive_summary[0].sources == ["https://a.com/1"]
    assert result.competitive_watch[0].actor == "OpenAI"
    assert result.opportunities[0].sources == ["https://a.com/1", "https://a.com/2"]


def test_parse_drops_items_with_no_valid_citation():
    # source index 99 doesn't exist -> resolves to [] -> item dropped entirely
    text = '{"executive_summary": [{"text": "Made up claim", "sources": [99]}], "competitive_watch": [], "opportunities": []}'
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.executive_summary == []


def test_parse_drops_items_with_missing_sources_field():
    text = '{"executive_summary": [{"text": "no sources at all"}], "competitive_watch": [], "opportunities": []}'
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.executive_summary == []


def test_parse_competitive_watch_requires_actor_and_highlights():
    text = '{"executive_summary": [], "competitive_watch": [{"actor": "OpenAI", "sources": [1]}], "opportunities": []}'
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.competitive_watch == []  # missing "highlights" -> dropped


def test_parse_deduplicates_repeated_source_indices():
    text = '{"executive_summary": [{"text": "x", "sources": [1, 1, 1]}], "competitive_watch": [], "opportunities": []}'
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.executive_summary[0].sources == ["https://a.com/1"]  # deduped


def test_parse_ignores_non_integer_source_entries():
    text = '{"executive_summary": [{"text": "x", "sources": ["not-a-number", 2]}], "competitive_watch": [], "opportunities": []}'
    result = parse_deep_analysis_response(text, ARTICLES)
    assert result.executive_summary[0].sources == ["https://a.com/2"]


def test_parse_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_deep_analysis_response("not json at all", ARTICLES)


# --------------------------------------------------------------------------- #
# run_deep_analysis: Gemini-only, no fallback, graceful failure
# --------------------------------------------------------------------------- #
class _Settings:
    class llm:
        gemini_model = "gemini-flash-latest"

    class app:
        report_language = "en"


def test_run_deep_analysis_success():
    provider = FakeProvider(
        "gemini",
        '{"executive_summary": [{"text": "ok", "sources": [1]}], "competitive_watch": [], "opportunities": []}',
    )
    client = LLMClient(provider, fallback=None, max_attempts=1)
    result = run_deep_analysis(ARTICLES, "digest", _Settings(), client=client)
    assert isinstance(result, DeepAnalysis)
    assert result.executive_summary[0].text == "ok"


def test_run_deep_analysis_returns_none_on_failure_no_fallback(monkeypatch):
    from sentinel.analyze import llm as llm_module

    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    provider = FakeProvider("gemini", RuntimeError("gemini down"))
    client = LLMClient(provider, fallback=None, max_attempts=1)
    result = run_deep_analysis(ARTICLES, "digest", _Settings(), client=client)
    assert result is None  # degrades gracefully, no Groq fallback attempted


def test_run_deep_analysis_empty_articles_returns_none():
    assert run_deep_analysis([], "digest", _Settings()) is None


def test_client_has_no_fallback_configured_for_deep_analysis():
    # sanity check on the contract: run_deep_analysis's default client construction
    # must not attach a Groq fallback (spec: historical-context analysis is Gemini-only)
    import inspect

    from sentinel.analyze.deep_analysis import run_deep_analysis as rda

    src = inspect.getsource(rda)
    assert "fallback=None" in src
