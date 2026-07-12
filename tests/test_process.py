"""Tests for the processing layer: relevance filter + dedup. No live calls, no DB."""

from __future__ import annotations

from unittest.mock import MagicMock

from sentinel.config import Actor, load_settings
from sentinel.process import ProcessResult, process_articles
from sentinel.process.dedup import (
    dedup_by_title,
    dedup_by_url,
    normalize_title,
    title_similarity,
)
from sentinel.process.filter import RelevanceFilter


def _art(url, title, *, snippet="", source="Test", actor=None):
    return {
        "url": url,
        "title": title,
        "source": source,
        "actor": actor,
        "published_at": None,
        "snippet": snippet,
        "content": None,
        "collected_at": "2026-07-05T00:00:00+00:00",
    }


# --------------------------------------------------------------------------- #
# Relevance filter
# --------------------------------------------------------------------------- #
KEYWORDS = ["AI", "artificial intelligence", "LLM", "funding"]
ACTORS = [
    Actor(name="OpenAI", category="LLM Providers", aliases=["GPT", "ChatGPT"]),
    Actor(name="Anthropic", category="LLM Providers", aliases=["Claude"]),
]
EXCLUDE = ["crypto", "horoscope"]


def _filter():
    return RelevanceFilter(KEYWORDS, ACTORS, EXCLUDE)


def test_keyword_match_keeps_article():
    rf = _filter()
    assert rf.is_relevant(_art("u", "New LLM breaks benchmark records"))


def test_out_of_scope_article_dropped():
    rf = _filter()
    assert not rf.is_relevant(_art("u", "Local bakery wins pastry award"))


def test_word_boundary_avoids_false_positive_on_AI():
    # "email" / "chair" contain the substring "ai" but must NOT match keyword "AI"
    rf = _filter()
    assert not rf.is_relevant(_art("u", "How to organize your email inbox and chair"))


def test_multiword_keyword_phrase_matches():
    rf = _filter()
    assert rf.is_relevant(_art("u", "The rise of artificial intelligence in retail"))


def test_actor_alias_match_and_attribution():
    rf = _filter()
    kept = rf.filter([_art("u", "Claude gets a major upgrade")])
    assert len(kept) == 1
    assert kept[0]["actor"] == "Anthropic"  # attributed from the "Claude" alias


def test_existing_actor_not_overwritten():
    rf = _filter()
    kept = rf.filter([_art("u", "GPT news", actor="OpenAI Blog")])
    assert kept[0]["actor"] == "OpenAI Blog"  # collector-set actor preserved


def test_exclusion_wins_over_keyword():
    rf = _filter()
    # matches keyword "AI" but also an exclusion term -> dropped
    assert not rf.is_relevant(_art("u", "AI crypto token launches to the moon"))


def test_filter_reports_subset():
    rf = _filter()
    arts = [
        _art("u1", "OpenAI launches model"),
        _art("u2", "Gardening tips for spring"),
        _art("u3", "LLM funding round announced"),
    ]
    assert len(rf.filter(arts)) == 2


# --------------------------------------------------------------------------- #
# Dedup: normalization + similarity
# --------------------------------------------------------------------------- #
def test_normalize_title_strips_case_and_punctuation():
    assert normalize_title("OpenAI's GPT-5: launched!") == "openai s gpt 5 launched"
    assert normalize_title("  Multiple   spaces  ") == "multiple spaces"


def test_title_similarity_high_for_case_punct_variants():
    a = "OpenAI launches GPT-5"
    b = "openai launches gpt 5!!!"
    assert title_similarity(a, b) >= 0.9


def test_title_similarity_low_for_distinct_titles():
    assert title_similarity("OpenAI launches GPT-5", "Mistral raises funding round") < 0.5


# --------------------------------------------------------------------------- #
# Dedup: URL
# --------------------------------------------------------------------------- #
def test_dedup_by_url_keeps_first():
    arts = [_art("https://x/1", "A"), _art("https://x/1", "A dup"), _art("https://x/2", "B")]
    out = dedup_by_url(arts)
    assert [a["url"] for a in out] == ["https://x/1", "https://x/2"]


# --------------------------------------------------------------------------- #
# Dedup: title similarity (the tricky cases)
# --------------------------------------------------------------------------- #
def test_dedup_by_title_merges_cross_source_near_duplicates():
    arts = [
        _art("https://tc/1", "OpenAI launches GPT-5 for enterprise", source="TechCrunch"),
        _art("https://vb/2", "OpenAI Launches GPT-5 for Enterprise!", source="VentureBeat"),
        _art("https://gn/3", "OpenAI launches GPT-5 for enterprise - The Verge", source="Google News"),
        _art("https://x/4", "Mistral AI announces new funding", source="RSS"),
    ]
    deduped, groups = dedup_by_title(arts, threshold=0.85)
    assert len(deduped) == 2          # 3 GPT-5 variants collapse to 1, Mistral stays
    assert len(groups) == 1
    assert len(groups[0]) == 3
    # representative is the first-seen article
    assert deduped[0]["source"] == "TechCrunch"


def test_dedup_by_title_keeps_distinct_stories_apart():
    arts = [
        _art("u1", "OpenAI launches GPT-5 for enterprise"),
        _art("u2", "Mistral AI raises Series B funding round"),
    ]
    deduped, groups = dedup_by_title(arts, threshold=0.85)
    assert len(deduped) == 2  # genuinely different stories -> not merged
    assert groups == []


def test_dedup_by_title_known_limitation_boilerplate_headlines():
    # KNOWN LIMITATION (documented, not a bug): headlines that are structurally
    # identical and differ only by entity/number score very high on pure title
    # similarity, so best-effort title dedup MAY over-merge them. Exact dedup is
    # guaranteed by the URL UNIQUE constraint, not this heuristic.
    arts = [
        _art("u1", "OpenAI raises $1B in new funding round"),
        _art("u2", "Anthropic raises $2B in new funding round"),
    ]
    assert title_similarity(arts[0]["title"], arts[1]["title"]) > 0.85
    deduped, _ = dedup_by_title(arts, threshold=0.85)
    assert len(deduped) == 1  # over-merged at 0.85 — raise the threshold to keep apart
    # a stricter threshold keeps them separate
    assert len(dedup_by_title(arts, threshold=0.92)[0]) == 2


def test_dedup_threshold_is_configurable():
    # Reworded but same story: merges at a lenient threshold, splits at a strict one
    arts = [
        _art("u1", "OpenAI launches new GPT-5 model today"),
        _art("u2", "OpenAI launches its new GPT-5 model"),
    ]
    assert len(dedup_by_title(arts, threshold=0.80)[0]) == 1
    assert len(dedup_by_title(arts, threshold=0.98)[0]) == 2


def test_dedup_handles_empty_titles_without_merging():
    arts = [_art("u1", ""), _art("u2", "")]
    deduped, groups = dedup_by_title(arts, threshold=0.85)
    assert len(deduped) == 2  # empty titles are never considered duplicates
    assert groups == []


# --------------------------------------------------------------------------- #
# Wiring: process_articles
# --------------------------------------------------------------------------- #
def test_process_articles_dry_run_counts():
    settings = load_settings(load_env=False)
    raw = [
        _art("https://a/1", "OpenAI launches GPT-5", source="TechCrunch"),
        _art("https://a/2", "OpenAI Launches GPT-5!", source="VentureBeat"),  # title dup
        _art("https://a/1", "OpenAI launches GPT-5", source="dup-url"),        # url dup
        _art("https://a/3", "Local bakery wins award", source="RSS"),         # off-scope
    ]
    result = process_articles(raw, settings, repo=None, threshold=0.85)
    assert isinstance(result, ProcessResult)
    assert result.collected == 4
    assert result.after_filter == 3          # bakery dropped
    assert result.after_url_dedup == 2       # url dup removed
    assert result.after_title_dedup == 1     # title dup merged
    assert result.persisted is False
    assert len(result.articles) == 1


def test_process_articles_persists_only_db_new(monkeypatch):
    settings = load_settings(load_env=False)
    raw = [
        _art("https://a/1", "OpenAI launches GPT-5"),
        _art("https://a/2", "Mistral AI raises funding"),
    ]
    repo = MagicMock()
    # DB says only the first URL is new (second already stored on a prior run)
    repo.insert_many.return_value = [{"url": "https://a/1"}]

    result = process_articles(raw, settings, repo=repo, threshold=0.85)

    assert result.persisted is True
    assert result.persisted_new == 1
    assert [a["url"] for a in result.articles] == ["https://a/1"]
    # persisted rows are projected to DB columns (no snippet/content) with processed=false
    (rows,), _ = repo.insert_many.call_args
    assert all("snippet" not in r and "content" not in r for r in rows)
    assert all(r["processed"] is False for r in rows)
