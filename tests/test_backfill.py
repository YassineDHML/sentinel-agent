"""Tests for scripts/backfill.py. Collector, LLM, and trend repo are all mocked —
no network, no LLM, no DB. The script is loaded by path (scripts/ isn't a package).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sentinel.config import load_settings

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill.py"
_spec = importlib.util.spec_from_file_location("backfill", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _settings():
    return load_settings(load_env=False)


def _raw(url, title):
    return {"url": url, "title": title, "source": "Google News", "actor": None,
            "published_at": "2026-06-10T00:00:00+00:00", "snippet": f"{title} details",
            "content": None, "collected_at": "2026-06-10T00:00:00+00:00"}


class FakeClient:
    def __init__(self):
        self.classify_calls = 0

    def classify_batch(self, articles, canonical_tags):
        self.classify_calls += 1
        # tag anything mentioning OpenAI as an LLM release, else product launch
        return {
            a["url"]: (["LLM release"] if "OpenAI" in a["title"] else ["product launch"])
            for a in articles
        }


class MemoryTrendRepo:
    def __init__(self):
        self.rows = {}

    def upsert(self, topic, week, article_count, actors=None):
        self.rows[(topic, week)] = {"topic": topic, "week": week,
                                    "article_count": article_count, "actors": actors or {}}

    def get_by_week(self, week):
        return [r for (t, w), r in self.rows.items() if w == week]


# --------------------------------------------------------------------------- #
def test_build_week_queries_adds_date_operators():
    qs = backfill.build_week_queries(["OpenAI", "AI agents"], "2026-W24")
    assert len(qs) == 2
    assert all(" after:" in q and " before:" in q for q in qs)
    assert qs[0].startswith("OpenAI after:")
    # date range is one ISO week apart (Mon -> next Mon)
    after = qs[0].split("after:")[1].split(" ")[0]
    before = qs[0].split("before:")[1]
    assert after < before


def test_run_backfill_records_each_week(monkeypatch):
    settings = _settings()

    def fake_collect(queries):
        # returns in-scope articles regardless of query
        return [_raw("https://n/openai", "OpenAI launches GPT-5"),
                _raw("https://n/mistral", "Mistral AI raises a round")]

    client = FakeClient()
    repo = MemoryTrendRepo()
    results = backfill.run_backfill(
        settings, weeks=3, end_week="2026-W28",
        collect_fn=fake_collect, client=client, trend_repo=repo,
    )

    # 3 weeks backfilled: W25, W26, W27 (the 3 before W28)
    assert set(results) == {"2026-W25", "2026-W26", "2026-W27"}
    # each week persisted its topic rows
    assert repo.get_by_week("2026-W25")
    assert repo.get_by_week("2026-W27")
    # classification ran once per week (single batch of 2 articles)
    assert client.classify_calls == 3


def test_run_backfill_dry_run_does_not_persist(monkeypatch):
    settings = _settings()
    client = FakeClient()
    repo = MemoryTrendRepo()
    results = backfill.run_backfill(
        settings, weeks=2, end_week="2026-W28", dry_run=True,
        collect_fn=lambda q: [_raw("https://n/o", "OpenAI news")],
        client=client, trend_repo=repo,
    )
    assert set(results) == {"2026-W26", "2026-W27"}
    assert repo.rows == {}                      # nothing written in dry-run
    assert all(results[w] for w in results)      # but counts still computed


def test_run_backfill_caps_articles_per_week():
    settings = _settings()
    # distinct titles so title-dedup doesn't merge them (all mention OpenAI -> in scope)
    headlines = [
        "OpenAI ships a new flagship model",
        "OpenAI cuts its API prices sharply",
        "OpenAI opens an office in Tokyo",
        "OpenAI hires a new chief financial officer",
        "OpenAI launches an enterprise app store",
        "OpenAI partners with a major German software vendor",
        "OpenAI faces a fresh copyright lawsuit",
        "OpenAI reports record quarterly revenue",
    ]
    many = [_raw(f"https://n/{i}", h) for i, h in enumerate(headlines)]
    client = FakeClient()
    repo = MemoryTrendRepo()
    backfill.run_backfill(
        settings, weeks=1, end_week="2026-W28", limit_per_week=5,
        collect_fn=lambda q: list(many), client=client, trend_repo=repo,
    )
    # only 5 of the 8 distinct articles are classified/counted
    row = repo.get_by_week("2026-W27")[0]
    assert row["article_count"] == 5
