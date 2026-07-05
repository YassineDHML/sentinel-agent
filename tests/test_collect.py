"""Tests for the collection layer. All parsing runs on saved fixtures; the few
fetch paths are exercised with mocked HTTP. No live network calls, no secrets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.collect import base
from sentinel.collect import fulltext, gnews, googlenews, hackernews, producthunt, rss

FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# base: normalization
# --------------------------------------------------------------------------- #
def test_make_article_shape_and_keys():
    art = base.make_article("https://x.com/a ", "Src", title=" Hi ", snippet=" s ")
    assert set(art.keys()) == set(base.ARTICLE_KEYS)
    assert art["url"] == "https://x.com/a"       # trimmed
    assert art["title"] == "Hi"
    assert art["snippet"] == "s"
    assert art["content"] is None
    assert art["collected_at"]                     # stamped


def test_make_article_missing_url_returns_none():
    assert base.make_article("", "Src") is None
    assert base.make_article(None, "Src") is None


def test_to_iso_variants():
    assert base.to_iso(None) is None
    assert base.to_iso(datetime(2026, 7, 1, tzinfo=timezone.utc)).startswith("2026-07-01")
    st = (2026, 7, 1, 9, 0, 0, 0, 183, 0)
    assert base.to_iso(st).startswith("2026-07-01T09:00:00")
    assert base.to_iso("2026-07-01T00:00:00Z").startswith("2026-07-01")
    assert base.to_iso("not a date") == "not a date"  # kept as-is


def test_graceful_swallows_exceptions():
    @base.graceful("boom")
    def collector():
        raise RuntimeError("source down")

    assert collector() == []


# --------------------------------------------------------------------------- #
# rss
# --------------------------------------------------------------------------- #
def test_rss_parse_feed_normalizes_and_skips_linkless():
    articles = rss.parse_feed(_fx("rss_sample.xml"), name="Sample", actor="OpenAI")
    # 3 items in fixture, but the linkless one is skipped
    assert len(articles) == 2
    first = articles[0]
    assert first["title"] == "OpenAI launches new model"
    assert first["url"] == "https://example.com/openai-new-model"
    assert first["source"] == "Sample"
    assert first["actor"] == "OpenAI"
    assert first["published_at"].startswith("2026-06-30")


def test_rss_collect_isolates_per_feed_failure(monkeypatch):
    from sentinel.config import Feed

    good = Feed(name="Good", url="good://feed")
    bad = Feed(name="Bad", url="bad://feed")

    def fake_parse(source, *, name, actor=None):
        if name == "Bad":
            raise RuntimeError("feed exploded")
        return [base.make_article("https://x/1", name)]

    monkeypatch.setattr(rss, "parse_feed", fake_parse)
    out = rss.collect_rss([good, bad])
    assert len(out) == 1  # bad feed isolated, good feed survives


# --------------------------------------------------------------------------- #
# hackernews
# --------------------------------------------------------------------------- #
def test_hn_parse_handles_missing_url_fallback():
    import json

    payload = json.loads(_fx("hn_sample.json"))
    articles = hackernews.parse_hn(payload, query="AI")
    assert len(articles) == 2
    # story with real URL
    assert articles[0]["url"] == "https://openai.com/blog/gpt-5"
    # Ask HN (url null) falls back to the HN item discussion page
    assert articles[1]["url"] == "https://news.ycombinator.com/item?id=40123457"
    assert articles[1]["snippet"].startswith("I'm evaluating")


def test_hn_search_calls_api(monkeypatch):
    import json

    captured = {}

    def fake_get_json(url, params=None, **kw):
        captured["url"] = url
        captured["params"] = params
        return json.loads(_fx("hn_sample.json"))

    monkeypatch.setattr(hackernews, "http_get_json", fake_get_json)
    out = hackernews.search_hn("AI agents")
    assert len(out) == 2
    assert captured["url"] == hackernews.SEARCH_URL
    assert captured["params"]["query"] == "AI agents"
    assert captured["params"]["tags"] == "story"


# --------------------------------------------------------------------------- #
# producthunt
# --------------------------------------------------------------------------- #
def test_producthunt_parse_prefers_website_then_url():
    import json

    payload = json.loads(_fx("producthunt_sample.json"))
    articles = producthunt.parse_producthunt(payload)
    assert len(articles) == 2
    assert articles[0]["url"] == "https://agentforge.example.com"        # website
    assert articles[1]["url"] == "https://www.producthunt.com/posts/promptdeck"  # fallback
    assert articles[0]["snippet"] == "Build AI agents without code"


def test_producthunt_no_token_returns_empty(monkeypatch):
    monkeypatch.delenv("PRODUCTHUNT_TOKEN", raising=False)
    assert producthunt.collect_producthunt() == []


# --------------------------------------------------------------------------- #
# googlenews
# --------------------------------------------------------------------------- #
def test_googlenews_parse_extracts_outlet():
    articles = googlenews.parse_googlenews(_fx("googlenews_sample.xml"), query="AI agents")
    assert len(articles) == 2
    assert articles[0]["source"] == "Google News (TechCrunch)"
    assert articles[0]["title"].startswith("AI agents go mainstream")
    assert articles[0]["published_at"].startswith("2026-07-02")


def test_googlenews_build_url_encodes_query():
    url = googlenews.build_search_url("AI agents & LLMs")
    assert "q=AI+agents" in url and "%26" in url  # space -> +, & -> %26


# --------------------------------------------------------------------------- #
# gnews (discovery layer)
# --------------------------------------------------------------------------- #
def test_gnews_parse_is_discovery_only():
    import json

    payload = json.loads(_fx("gnews_sample.json"))
    articles = gnews.parse_gnews(payload, query="Mistral")
    assert len(articles) == 2
    assert articles[0]["content"] is None                 # discovery layer: no full text
    assert articles[0]["snippet"].startswith("The French startup")
    assert articles[0]["source"] == "GNews (Example News)"


def test_gnews_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("GNEWS_API_KEY", raising=False)
    assert gnews.collect_gnews(["Mistral"]) == []


def test_gnews_respects_rate_limit_between_queries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gnews, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gnews, "search_gnews", lambda q, **kw: [])
    gnews.collect_gnews(["a", "b", "c"], api_key="fake")
    # one inter-query sleep between each of the 3 queries (not before the first)
    assert len(sleeps) == 2
    assert all(s == gnews.REQUEST_INTERVAL for s in sleeps)


def test_gnews_search_clamps_max_to_free_tier(monkeypatch):
    captured = {}

    def fake_get_json(url, params=None, **kw):
        captured["params"] = params
        return {"articles": []}

    monkeypatch.setattr(gnews, "http_get_json", fake_get_json)
    gnews.search_gnews("q", api_key="k", max_articles=100)
    assert captured["params"]["max"] == gnews.MAX_PER_REQUEST  # clamped to 10


# --------------------------------------------------------------------------- #
# fulltext
# --------------------------------------------------------------------------- #
def test_extract_main_text_strips_boilerplate():
    text = fulltext.extract_main_text(_fx("article_sample.html"))
    assert "flagship model" in text
    assert "Copyright 2026" not in text     # footer removed
    assert "Home | News | About" not in text  # nav removed
    assert "tracking" not in text            # script removed


def test_fetch_fulltext_success(monkeypatch):
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    monkeypatch.setattr(fulltext, "http_get_text", lambda url, **kw: _fx("article_sample.html"))
    text = fulltext.fetch_fulltext("https://example.com/a", delay=0)
    assert text and "flagship model" in text


def test_fetch_fulltext_robots_disallow_returns_none(monkeypatch):
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: False)
    assert fulltext.fetch_fulltext("https://example.com/blocked") is None


def test_fetch_fulltext_too_short_returns_none(monkeypatch):
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    monkeypatch.setattr(fulltext, "http_get_text", lambda url, **kw: "<html><body><p>tiny</p></body></html>")
    assert fulltext.fetch_fulltext("https://example.com/a", delay=0) is None
