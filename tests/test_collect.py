"""Tests for the collection layer. All parsing runs on saved fixtures; the few
fetch paths are exercised with mocked HTTP. No live network calls, no secrets.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.collect import base
from sentinel.collect import fulltext, gnews, googlenews, hackernews, producthunt, resolve, rss

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
    monkeypatch.setattr(fulltext, "_fetch_html", lambda url, host: _fx("article_sample.html"))
    text = fulltext.fetch_fulltext("https://example.com/a", delay=0)
    assert text and "flagship model" in text


def test_fetch_fulltext_robots_disallow_returns_none(monkeypatch):
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: False)
    assert fulltext.fetch_fulltext("https://example.com/blocked") is None


def test_fetch_fulltext_too_short_returns_none(monkeypatch):
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    monkeypatch.setattr(fulltext, "_fetch_html", lambda url, host: "<html><body><p>tiny</p></body></html>")
    assert fulltext.fetch_fulltext("https://example.com/a", delay=0) is None


# --- 429 handling + per-host circuit breaker --------------------------------- #
class _FakeResp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            import requests

            raise requests.HTTPError(str(self.status_code))


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


def _reset_fulltext_state():
    fulltext._tripped_hosts.clear()
    fulltext._host_429_count.clear()
    fulltext._robots_cache.clear()
    fulltext._last_fetch_at.clear()


def test_fetch_fulltext_retries_on_429_then_succeeds(monkeypatch):
    _reset_fulltext_state()
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    session = _FakeSession([_FakeResp(429), _FakeResp(200, _fx("article_sample.html"))])
    monkeypatch.setattr(fulltext, "get_session", lambda: session)
    text = fulltext.fetch_fulltext("https://ex.com/a", delay=0)
    assert text and "flagship model" in text
    assert len(session.calls) == 2  # one retry after the 429


def test_fetch_fulltext_gives_up_after_max_429(monkeypatch):
    _reset_fulltext_state()
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    session = _FakeSession([_FakeResp(429)])  # always rate-limited
    monkeypatch.setattr(fulltext, "get_session", lambda: session)
    assert fulltext.fetch_fulltext("https://ex.com/a", delay=0) is None
    assert len(session.calls) == fulltext.MAX_429_RETRIES + 1


def test_fetch_fulltext_circuit_breaker_trips_and_skips_host(monkeypatch):
    _reset_fulltext_state()
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    monkeypatch.setattr(fulltext, "_sleep", lambda s: None)
    session = _FakeSession([_FakeResp(429)])
    monkeypatch.setattr(fulltext, "get_session", lambda: session)
    for i in range(fulltext.HOST_TRIP_THRESHOLD):
        assert fulltext.fetch_fulltext(f"https://vb.com/{i}", delay=0) is None
    assert "vb.com" in fulltext._tripped_hosts
    calls_before = len(session.calls)
    # host tripped -> subsequent fetch returns immediately, no new HTTP request
    assert fulltext.fetch_fulltext("https://vb.com/another", delay=0) is None
    assert len(session.calls) == calls_before


def test_fetch_fulltext_honors_retry_after_header(monkeypatch):
    _reset_fulltext_state()
    monkeypatch.setattr(fulltext, "_robots_allowed", lambda url: True)
    waits = []
    monkeypatch.setattr(fulltext, "_sleep", lambda s: waits.append(s))
    session = _FakeSession(
        [_FakeResp(429, headers={"Retry-After": "5"}), _FakeResp(200, _fx("article_sample.html"))]
    )
    monkeypatch.setattr(fulltext, "get_session", lambda: session)
    text = fulltext.fetch_fulltext("https://ex.com/a", delay=0)
    assert text and "flagship model" in text
    assert waits == [5.0]  # honored the numeric Retry-After


# --------------------------------------------------------------------------- #
# resolve (Google News redirect resolution)
# --------------------------------------------------------------------------- #
def _make_gnews_url(publisher_url: str) -> str:
    """Build a Google-News-style redirect link that base64-embeds ``publisher_url``.

    Mimics the older payload shape (``\\x08\\x13\\x22<len><url>...``), which
    base64url-encodes to a ``CBMi...`` segment — exactly what the offline decoder
    keys off.
    """
    body = publisher_url.encode("utf-8")
    raw = b"\x08\x13\x22" + bytes([len(body)]) + body + b"\xd2\x01\x08trailing"
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"https://news.google.com/rss/articles/{payload}?oc=5"


def test_is_redirect_url_detects_google_news_only():
    assert resolve.is_redirect_url("https://news.google.com/rss/articles/CBMiABC?oc=5")
    assert not resolve.is_redirect_url("https://techcrunch.com/2026/07/01/story")
    assert not resolve.is_redirect_url(None)


def test_decode_google_news_url_extracts_publisher_url():
    real = "https://techcrunch.com/2026/07/01/openai-launch"
    decoded = resolve.decode_google_news_url(_make_gnews_url(real))
    assert decoded == real


def test_decode_google_news_url_returns_none_without_embedded_url():
    # A payload that decodes to bytes with no plain http(s) URL.
    payload = base64.urlsafe_b64encode(b"\x08\x13\x22no-url-here").decode().rstrip("=")
    url = f"https://news.google.com/rss/articles/{payload}?oc=5"
    assert resolve.decode_google_news_url(url) is None


def test_resolve_redirect_passes_through_non_redirect():
    url = "https://theverge.com/story"
    assert resolve.resolve_redirect(url) == url


def test_resolve_redirect_prefers_offline_decode(monkeypatch):
    # Network must NOT be touched when the offline decode already succeeds.
    def boom(*a, **k):
        raise AssertionError("follow_redirect should not be called")

    monkeypatch.setattr(resolve, "follow_redirect", boom)
    real = "https://arstechnica.com/ai/2026/07/model"
    assert resolve.resolve_redirect(_make_gnews_url(real)) == real


def test_resolve_redirect_falls_back_to_http_follow(monkeypatch):
    # Undecodable payload -> HTTP follow captures the final publisher URL.
    monkeypatch.setattr(resolve, "decode_google_news_url", lambda url: None)

    class _Resp:
        url = "https://venturebeat.com/ai/real-article"

        def close(self):
            pass

    class _Session:
        def get(self, url, **kw):
            assert kw["allow_redirects"] is True
            assert "User-Agent" in kw["headers"]
            return _Resp()

    out = resolve.resolve_redirect(
        "https://news.google.com/rss/articles/OPAQUE?oc=5",
        session=_Session(),
    )
    assert out == "https://venturebeat.com/ai/real-article"


def test_resolve_redirect_keeps_original_when_all_fail(monkeypatch):
    monkeypatch.setattr(resolve, "decode_google_news_url", lambda url: None)
    monkeypatch.setattr(resolve, "follow_redirect", lambda url, **kw: None)
    original = "https://news.google.com/rss/articles/OPAQUE?oc=5"
    assert resolve.resolve_redirect(original) == original


def test_resolve_redirect_no_network_skips_http(monkeypatch):
    monkeypatch.setattr(resolve, "decode_google_news_url", lambda url: None)

    def boom(*a, **k):
        raise AssertionError("follow_redirect should not run when allow_network=False")

    monkeypatch.setattr(resolve, "follow_redirect", boom)
    original = "https://news.google.com/rss/articles/OPAQUE?oc=5"
    assert resolve.resolve_redirect(original, allow_network=False) == original


def test_decode_never_resolves_into_another_redirect():
    """A payload embedding another news.google.com link must not be accepted."""
    nested = _make_gnews_url("https://news.google.com/rss/articles/INNER?oc=5")
    assert resolve.decode_google_news_url(nested) is None


def test_follow_redirect_ignores_chain_stuck_on_google():
    class _Resp:
        url = "https://news.google.com/consent"  # never left the redirect host

        def close(self):
            pass

    class _Session:
        def get(self, url, **kw):
            return _Resp()

    assert resolve.follow_redirect("https://news.google.com/x", session=_Session()) is None


def test_follow_redirect_returns_none_on_error():
    class _Session:
        def get(self, url, **kw):
            raise RuntimeError("network down")

    assert resolve.follow_redirect("https://news.google.com/x", session=_Session()) is None


def test_resolve_articles_rewrites_url_in_place():
    real = "https://techcrunch.com/2026/07/01/story"
    articles = [
        base.make_article(_make_gnews_url(real), "Google News (TechCrunch)", title="Story"),
        base.make_article("https://theverge.com/other", "The Verge", title="Other"),
    ]
    resolve.resolve_articles(articles, allow_network=False)
    assert articles[0]["url"] == real                    # redirect -> publisher
    assert articles[1]["url"] == "https://theverge.com/other"  # untouched


def test_collect_googlenews_resolves_before_returning(monkeypatch):
    real = "https://techcrunch.com/2026/07/01/agents"
    gnews_url = _make_gnews_url(real)

    def fake_parse(source, *, query=None):
        return [base.make_article(gnews_url, "Google News (TechCrunch)", title="Agents")]

    monkeypatch.setattr(googlenews, "parse_googlenews", fake_parse)
    out = googlenews.collect_googlenews(["AI agents"])
    assert len(out) == 1
    assert out[0]["url"] == real  # canonical publisher URL, not a news.google.com link


def test_collect_googlenews_does_not_hit_network_by_default(monkeypatch):
    """The HTTP-follow fallback must stay OFF by default.

    Measured against live Google News it resolves nothing while costing ~0.5s per
    article; with hundreds of articles per run that is minutes of wasted requests.
    """
    def fake_parse(source, *, query=None):
        return [base.make_article(
            "https://news.google.com/rss/articles/OPAQUE?oc=5", "Google News", title="X")]

    def boom(*a, **k):
        raise AssertionError("collect_googlenews must not follow redirects by default")

    monkeypatch.setattr(googlenews, "parse_googlenews", fake_parse)
    monkeypatch.setattr(resolve, "follow_redirect", boom)
    out = googlenews.collect_googlenews(["q"])
    # unresolvable link is kept as-is rather than dropped
    assert out[0]["url"] == "https://news.google.com/rss/articles/OPAQUE?oc=5"


def test_collect_googlenews_can_skip_resolution(monkeypatch):
    """resolve=False leaves redirect URLs intact (offline/debug path)."""
    gnews_url = _make_gnews_url("https://techcrunch.com/x")

    def fake_parse(source, *, query=None):
        return [base.make_article(gnews_url, "Google News", title="X")]

    monkeypatch.setattr(googlenews, "parse_googlenews", fake_parse)
    out = googlenews.collect_googlenews(["q"], resolve=False)
    assert out[0]["url"] == gnews_url
