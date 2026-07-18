"""Tests for the analysis layer. The LLM is fully mocked via fake providers —
no live API calls, no keys. Covers prompt construction, batching, tag validation,
retry/backoff, and the tiered fallback path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sentinel.analyze import analyze_articles
from sentinel.analyze import llm as llm_module
from sentinel.analyze.classify import parse_classify_response
from sentinel.analyze.llm import LLMClient, LLMError, is_rate_limit
from sentinel.analyze.parsing import extract_json_object
from sentinel.analyze.prompts import build_classify_prompt, build_summary_prompt
from sentinel.analyze.summarize import parse_summary_response

CANON = ["product launch", "funding and investment", "AI agents"]


def _art(url, title, **kw):
    return {"url": url, "title": title, "source": kw.get("source", "Test"),
            "actor": kw.get("actor"), "snippet": kw.get("snippet", ""),
            "content": kw.get("content")}


class FakeProvider:
    """Returns queued responses; an Exception in the queue is raised instead."""

    def __init__(self, name, sequence):
        self.name = name
        self._seq = list(sequence)
        self._i = 0
        self.calls: list[str] = []

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        item = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        if isinstance(item, BaseException):
            raise item
        return item


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def test_summary_prompt_numbers_articles_and_asks_for_json():
    items = [(1, _art("u1", "OpenAI ships GPT-5")), (2, _art("u2", "Mistral funding"))]
    prompt = build_summary_prompt(items)
    assert "[1] TITLE: OpenAI ships GPT-5" in prompt
    assert "[2] TITLE: Mistral funding" in prompt
    assert "JSON" in prompt and "3 to 5" in prompt


def test_classify_prompt_embeds_all_canonical_tags():
    items = [(1, _art("u1", "x"))]
    prompt = build_classify_prompt(items, CANON)
    for tag in CANON:
        assert f"- {tag}" in prompt
    assert "STRICTLY" in prompt


# --------------------------------------------------------------------------- #
# Parsing + tag validation
# --------------------------------------------------------------------------- #
def test_extract_json_object_tolerates_code_fences():
    assert extract_json_object('```json\n{"1": "hi"}\n```') == {"1": "hi"}
    assert extract_json_object('sure, here:\n{"1": ["a"]}\nthanks') == {"1": ["a"]}


def test_extract_json_object_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json_object("no json here")


def test_extract_json_object_merges_concatenated_objects():
    # Observed in the wild: Gemini emitting several objects back-to-back
    text = '{"1": "first summary"}\n{"2": "second summary"}'
    assert extract_json_object(text) == {"1": "first summary", "2": "second summary"}


def test_unparseable_batch_is_reasked_once_then_succeeds():
    provider = FakeProvider("gemini", ["<<not json>>", '{"1": "recovered"}'])
    client = LLMClient(provider, batch_size=10)
    out = client.summarize_batch([_art("u1", "a")])
    assert out == {"u1": "recovered"}
    assert len(provider.calls) == 2  # one re-ask


def test_unparseable_batch_twice_is_skipped():
    provider = FakeProvider("gemini", ["<<junk>>", "<<junk again>>"])
    client = LLMClient(provider, batch_size=10)
    assert client.summarize_batch([_art("u1", "a")]) == {}
    assert len(provider.calls) == 2  # asked, re-asked, then gave up


def test_parse_summary_ignores_unknown_indices():
    out = parse_summary_response('{"1": "good", "9": "orphan"}', {"1": "u1"})
    assert out == {"u1": "good"}


def test_parse_classify_drops_invented_tags_and_normalizes_case():
    text = '{"1": ["Product Launch", "totally made up", "AI Agents"], "2": []}'
    out = parse_classify_response(text, {"1": "u1", "2": "u2"}, CANON)
    assert out["u1"] == ["product launch", "AI agents"]  # canonical casing, invented dropped
    assert out["u2"] == []


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #
def test_summarize_batches_by_size():
    # 3 articles, batch_size 2 -> 2 calls; each batch re-indexes from 1
    provider = FakeProvider("gemini", ['{"1": "s1", "2": "s2"}', '{"1": "s3"}'])
    client = LLMClient(provider, batch_size=2)
    arts = [_art("u1", "a"), _art("u2", "b"), _art("u3", "c")]
    out = client.summarize_batch(arts)
    assert len(provider.calls) == 2
    assert out == {"u1": "s1", "u2": "s2", "u3": "s3"}


def test_classify_validates_against_canonical():
    provider = FakeProvider("gemini", ['{"1": ["product launch", "nonsense"], "2": ["AI agents"]}'])
    client = LLMClient(provider, batch_size=10)
    out = client.classify_batch([_art("u1", "a"), _art("u2", "b")], CANON)
    assert out == {"u1": ["product launch"], "u2": ["AI agents"]}


def test_failed_batch_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    # provider always errors -> batch skipped, empty result, no exception
    provider = FakeProvider("gemini", [RuntimeError("boom")])
    client = LLMClient(provider, batch_size=10, max_attempts=2)
    assert client.summarize_batch([_art("u1", "a")]) == {}


# --------------------------------------------------------------------------- #
# Retry / backoff / fallback
# --------------------------------------------------------------------------- #
def test_retry_then_success(monkeypatch):
    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    provider = FakeProvider("gemini", [RuntimeError("x"), RuntimeError("x"), '{"1": "ok"}'])
    client = LLMClient(provider, batch_size=10, max_attempts=3)
    out = client.summarize_batch([_art("u1", "a")])
    assert out == {"u1": "ok"}
    assert len(provider.calls) == 3


def test_fallback_used_when_primary_fails(monkeypatch):
    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    primary = FakeProvider("gemini", [RuntimeError("down")])
    fallback = FakeProvider("groq", ['{"1": "from-groq"}'])
    client = LLMClient(primary, fallback, batch_size=10, max_attempts=1)
    out = client.summarize_batch([_art("u1", "a")])
    assert out == {"u1": "from-groq"}
    assert len(fallback.calls) == 1


def test_both_providers_fail_batch_skipped(monkeypatch):
    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    primary = FakeProvider("gemini", [RuntimeError("down")])
    fallback = FakeProvider("groq", [RuntimeError("also down")])
    client = LLMClient(primary, fallback, batch_size=10, max_attempts=1)
    assert client.summarize_batch([_art("u1", "a")]) == {}


def test_generate_raises_llmerror_without_fallback(monkeypatch):
    monkeypatch.setattr(llm_module, "_sleep", lambda s: None)
    client = LLMClient(FakeProvider("gemini", [RuntimeError("x")]), max_attempts=1)
    with pytest.raises(LLMError):
        client.generate("prompt")


def test_is_rate_limit_detection():
    class RateLimitError(Exception):
        pass

    class Boom(Exception):
        code = 429

    assert is_rate_limit(RateLimitError())
    assert is_rate_limit(Boom())
    assert not is_rate_limit(ValueError("nope"))


# --------------------------------------------------------------------------- #
# Orchestration: analyze_articles
# --------------------------------------------------------------------------- #
def test_analyze_articles_attaches_and_persists():
    arts = [_art("u1", "OpenAI ships GPT-5"), _art("u2", "random")]
    client = MagicMock(spec=LLMClient)
    client.summarize_batch.return_value = {"u1": "a summary"}   # only u1 summarized
    client.classify_batch.return_value = {"u1": ["product launch"], "u2": []}
    repo = MagicMock()

    out = analyze_articles(arts, settings=MagicMock(topics=CANON), client=client, repo=repo)

    assert out[0]["summary"] == "a summary"
    assert out[0]["topics"] == ["product launch"]
    assert out[1]["summary"] is None
    # only the article that got a summary is persisted + marked processed
    repo.mark_analyzed.assert_called_once_with("u1", summary="a summary", topics=["product launch"])


def test_analyze_articles_empty_input():
    assert analyze_articles([], settings=MagicMock(topics=CANON), client=MagicMock()) == []
