"""Tests for the trend engine. Uses an in-memory fake trends repo — no DB."""

from __future__ import annotations

from datetime import datetime, timezone

from sentinel.analyze.trends import (
    ACCELERATING,
    NEW,
    ONGOING,
    TrendStatus,
    build_trend_digest,
    classify_trend_status,
    compute_trend_statuses,
    count_topics,
    current_iso_week,
    iso_week_of,
    prior_weeks,
    record_week_trends,
)


class FakeTrendRepo:
    """Rows keyed by (topic, week); implements upsert + get_by_week."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert(self, topic, week, article_count, actors=None):
        self.rows[(topic, week)] = {
            "topic": topic, "week": week, "article_count": article_count, "actors": actors or {}
        }

    def get_by_week(self, week):
        return [r for (t, w), r in self.rows.items() if w == week]

    def seed(self, topic, week, count, actors=None):
        self.upsert(topic, week, count, actors)


def _art(url, topics, actor=None):
    return {"url": url, "topics": topics, "actor": actor}


# --------------------------------------------------------------------------- #
# ISO week helpers
# --------------------------------------------------------------------------- #
def test_iso_week_formatting():
    assert iso_week_of(datetime(2026, 7, 8, tzinfo=timezone.utc).date()) == "2026-W28"
    assert current_iso_week(datetime(2026, 7, 8, tzinfo=timezone.utc)) == "2026-W28"


def test_prior_weeks_are_previous_and_ordered_recent_first():
    assert prior_weeks("2026-W28", 4) == ["2026-W27", "2026-W26", "2026-W25", "2026-W24"]


def test_prior_weeks_crosses_year_boundary():
    assert prior_weeks("2026-W02", 3) == ["2026-W01", "2025-W52", "2025-W51"]


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #
def test_count_topics_tallies_counts_and_actors():
    arts = [
        _art("u1", ["AI agents", "product launch"], actor="OpenAI"),
        _art("u2", ["AI agents"], actor="LangChain"),
        _art("u3", ["AI agents"], actor="OpenAI"),
    ]
    counts = count_topics(arts)
    assert counts["AI agents"].article_count == 3
    assert counts["AI agents"].actors == {"OpenAI": 2, "LangChain": 1}
    assert counts["product launch"].article_count == 1


def test_record_week_trends_upserts_each_topic():
    repo = FakeTrendRepo()
    arts = [_art("u1", ["AI agents"], "OpenAI"), _art("u2", ["LLM release"], "Mistral AI")]
    record_week_trends(arts, repo, week="2026-W28")
    assert repo.rows[("AI agents", "2026-W28")]["article_count"] == 1
    assert repo.rows[("LLM release", "2026-W28")]["article_count"] == 1


# --------------------------------------------------------------------------- #
# Status classification (the core: new vs ongoing vs accelerating)
# --------------------------------------------------------------------------- #
def test_classify_new_when_no_prior():
    assert classify_trend_status(5, [0, 0, 0, 0]) == NEW


def test_classify_accelerating_when_rising():
    # prev avg 4.5; current 12 >= 1.5*4.5 -> accelerating
    assert classify_trend_status(12, [3, 4, 5, 6]) == ACCELERATING


def test_classify_ongoing_when_flat():
    # prev avg 5; current 5 < 1.5*5 -> ongoing
    assert classify_trend_status(5, [5, 5, 5, 5]) == ONGOING


def test_classify_ongoing_when_slightly_up_but_below_factor():
    assert classify_trend_status(6, [5, 5, 5, 5]) == ONGOING


def test_classify_not_accelerating_below_min_count():
    # a 1 -> 2 blip shouldn't count as accelerating despite ratio
    assert classify_trend_status(2, [0, 0, 0, 1]) == ONGOING


# --------------------------------------------------------------------------- #
# End-to-end status computation from seeded history
# --------------------------------------------------------------------------- #
def _seed_history(repo, week="2026-W28"):
    pw = list(reversed(prior_weeks(week, 4)))  # oldest -> newest: W24..W27
    for w, c in zip(pw, [3, 4, 5, 6]):
        repo.seed("AI agents", w, c, {"OpenAI": c})
        repo.seed("pricing change", w, 2, {"Anthropic": 2})
    repo.seed("AI agents", week, 12, {"OpenAI": 7, "LangChain": 5})   # accelerating
    repo.seed("LLM release", week, 5, {"Mistral AI": 5})              # new
    repo.seed("pricing change", week, 2, {"Anthropic": 2})           # ongoing


def test_compute_trend_statuses_distinguishes_all_three():
    repo = FakeTrendRepo()
    _seed_history(repo)
    statuses = {s.topic: s for s in compute_trend_statuses(repo, week="2026-W28")}

    assert statuses["AI agents"].status == ACCELERATING
    assert statuses["AI agents"].prior_counts == [3, 4, 5, 6]  # oldest -> newest
    assert statuses["LLM release"].status == NEW
    assert statuses["pricing change"].status == ONGOING


def test_compute_trend_statuses_sorted_accelerating_first():
    repo = FakeTrendRepo()
    _seed_history(repo)
    ordered = [s.status for s in compute_trend_statuses(repo, week="2026-W28")]
    assert ordered[0] == ACCELERATING  # sorted so the hottest trend leads


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #
def test_build_trend_digest_groups_and_stays_compact():
    repo = FakeTrendRepo()
    _seed_history(repo)
    digest = build_trend_digest(compute_trend_statuses(repo, week="2026-W28"), week="2026-W28")
    assert "ACCELERATING:" in digest and "NEW:" in digest and "ONGOING:" in digest
    assert "AI agents: 12" in digest
    assert "prev 3,4,5,6" in digest       # accelerating shows the series
    assert "LLM release: 5" in digest
    # compact: no raw article text/URLs leak into the digest
    assert "http" not in digest
    assert len(digest.splitlines()) < 12
