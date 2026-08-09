"""Integration tests for the pipeline: the whole flow with ALL externals mocked
(collectors, LLM, repos, full-text fetch, email, Slack). Asserts stage order and
graceful-degradation behavior. No network, no secrets, no DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sentinel import pipeline as pipe
from sentinel.analyze.llm import LLMClient
from sentinel.config import load_settings

EXPECTED_ORDER = ["collect", "process", "fulltext", "analyze", "trends", "report", "deliver"]


def _settings():
    return load_settings(load_env=False)


def _raw(url, title, actor=None):
    return {"url": url, "title": title, "source": "Test", "actor": actor,
            "published_at": "2026-07-13T10:00:00+00:00",
            "snippet": f"{title} snippet", "content": None,
            "collected_at": "2026-07-13T10:00:00+00:00"}


RAW_BATCH = [
    _raw("https://a.com/gpt5", "OpenAI launches GPT-5"),
    _raw("https://b.com/gpt5", "OpenAI Launches GPT-5!"),          # title dup
    _raw("https://a.com/mistral", "Mistral AI raises funding"),
    _raw("https://a.com/bakery", "Local bakery wins award"),       # off-scope
]


class FakeProvider:
    """Scripted provider: 'summary'/'classify'/'deep' responses keyed by prompt content."""

    def __init__(self, name="gemini", fail=False):
        self.name = name
        self.fail = fail

    def generate(self, prompt: str) -> str:
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        if "canonical list" in prompt or "FIXED taxonomy" in prompt:
            return '{"1": ["product launch"], "2": ["funding and investment"]}'
        if "executive_summary" in prompt:
            return ('{"executive_summary": [{"text": "Big week for OpenAI.", "sources": [1]}],'
                    ' "competitive_watch": [], "opportunities": []}')
        return '{"1": "Summary one.", "2": "Summary two."}'


class MemoryArticleRepo:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.content_writes: list[str] = []

    def insert_many(self, articles):
        new = []
        for a in articles:
            if a["url"] not in self.rows:
                self.rows[a["url"]] = dict(a)
                new.append(dict(a))
        return new

    def list_unprocessed(self, limit=None):
        out = [dict(r) for r in self.rows.values() if not r.get("processed")]
        return out[:limit] if limit else out

    def list_by_week(self, start, end):
        return [dict(r) for r in self.rows.values()]

    def set_content(self, url, content):
        self.rows[url]["content"] = content
        self.content_writes.append(url)

    def mark_analyzed(self, url, *, summary, topics):
        self.rows[url].update(summary=summary, topics=topics, processed=True)


class MemoryTrendRepo(pipe._MemoryTrendRepo):
    pass


def _run(monkeypatch, *, dry_run=False, provider=None, deep_available=True,
         collectors=None, emailer=None, slacker=None, article_repo=None):
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: f"full text of {url} " * 20)
    monkeypatch.setattr("sentinel.analyze.llm._sleep", lambda s: None)  # no real backoff waits
    provider = provider or FakeProvider()
    client = LLMClient(provider, fallback=None, batch_size=8, max_attempts=1)
    article_repo = article_repo or MemoryArticleRepo()
    trend_repo = MemoryTrendRepo()
    report_repo = MagicMock()
    emailer = emailer or MagicMock(return_value=True)
    slacker = slacker or MagicMock(return_value=True)

    result = pipe.run_pipeline(
        _settings(), dry_run=dry_run, week="2026-W28",
        collectors=collectors or {"rss": lambda: list(RAW_BATCH)},
        llm_client=client, deep_available=deep_available,
        article_repo=article_repo, trend_repo=trend_repo, report_repo=report_repo,
        emailer=emailer, slacker=slacker,
    )
    return result, article_repo, trend_repo, report_repo, emailer, slacker


# --------------------------------------------------------------------------- #
# Happy path: stage order, counts, persistence, delivery
# --------------------------------------------------------------------------- #
def test_pipeline_runs_stages_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipe, "run_pipeline", pipe.run_pipeline)  # no-op, clarity
    result, articles, trends, reports, emailer, slacker = _run(monkeypatch)

    assert result.stage_names() == EXPECTED_ORDER
    assert result.ok is True
    assert result.degradations == []


def test_pipeline_happy_path_full_effects(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    result, articles, trends, reports, emailer, slacker = _run(monkeypatch)

    # process: off-scope dropped, title-dup merged -> 2 stored
    assert len(articles.rows) == 2
    # analyze: both marked processed with summaries + canonical topics
    assert all(r.get("processed") for r in articles.rows.values())
    # fulltext fetched and cached back
    assert len(articles.content_writes) == 2
    # trends recorded for the week
    assert trends.get_by_week("2026-W28")
    # report archived + emailed + slacked
    reports.store.assert_called_once()
    emailer.assert_called_once()
    slacker.assert_called_once()
    # deep-analysis highlights flowed into Slack
    assert slacker.call_args.kwargs["highlights"] == ["Big week for OpenAI."]
    # REGRESSION (report must use the analyzed articles, not "0 analyzed"): the
    # report file contains the analyzed article + the deep-analysis narrative.
    html = result.report_path.read_text(encoding="utf-8")
    assert "OpenAI launches GPT-5" in html          # analyzed article listed as a source
    assert "Big week for OpenAI." in html           # deep-analysis exec summary


def test_pipeline_dry_run_touches_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "text " * 100)
    import sentinel.pipeline as p
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)

    emailer = MagicMock(return_value=True)
    article_repo = MemoryArticleRepo()
    provider = FakeProvider()
    client = LLMClient(provider, fallback=None, batch_size=8, max_attempts=1)
    result = p.run_pipeline(
        _settings(), dry_run=True, week="2026-W28",
        collectors={"rss": lambda: list(RAW_BATCH)},
        llm_client=client, deep_available=True,
        article_repo=article_repo, emailer=emailer, slacker=MagicMock(return_value=True),
    )
    assert result.ok is True
    assert article_repo.rows == {}                       # nothing persisted
    assert emailer.call_args.kwargs["dry_run"] is True   # delivery in dry-run mode
    assert result.report_path is not None and result.report_path.exists()
    assert "dryrun" in result.report_path.name


# --------------------------------------------------------------------------- #
# Degradation behavior
# --------------------------------------------------------------------------- #
def test_failing_source_is_skipped_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)

    def broken():
        raise RuntimeError("feed exploded")

    result, articles, *_ = _run(
        monkeypatch,
        collectors={"rss": lambda: list(RAW_BATCH), "hackernews": broken},
    )
    assert result.ok is True
    assert result.stage_names() == EXPECTED_ORDER          # every stage still ran
    assert any("hackernews" in d for d in result.degradations)
    assert len(articles.rows) == 2                          # rss articles survived


def test_deep_analysis_failure_degrades_but_report_ships(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)

    class DeepFailProvider(FakeProvider):
        def generate(self, prompt):
            if "executive_summary" in prompt:
                raise RuntimeError("gemini overloaded")
            return super().generate(prompt)

    result, articles, trends, reports, emailer, _ = _run(
        monkeypatch, provider=DeepFailProvider()
    )
    assert result.ok is True
    assert any("deep analysis failed" in d for d in result.degradations)
    reports.store.assert_called_once()                     # report still archived
    emailer.assert_called_once()                           # and still delivered


def test_groq_only_mode_skips_deep_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    # deep_available=False models the Groq-only tier: light tasks ran, deep skipped
    result, articles, trends, reports, emailer, slacker = _run(
        monkeypatch, provider=FakeProvider(name="groq"), deep_available=False,
    )
    assert result.ok is True
    assert all(r.get("processed") for r in articles.rows.values())  # light tasks ran
    assert slacker.call_args.kwargs["highlights"] is None           # no deep output
    emailer.assert_called_once()                                    # report delivered


def test_email_failure_is_loud_and_fails_live_run(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    emailer = MagicMock(side_effect=RuntimeError("SMTP down"))
    result, *_ , reports, _, slacker = _run(monkeypatch, emailer=emailer)

    assert result.ok is False                              # live run fails loudly
    assert any("EMAIL DELIVERY FAILED" in d for d in result.degradations)
    reports.store.assert_called_once()                     # report was archived first
    slacker.assert_called_once()                           # slack still attempted


def test_slack_failure_never_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    slacker = MagicMock(side_effect=RuntimeError("webhook gone"))
    result, *_ = _run(monkeypatch, slacker=slacker)
    assert result.ok is True                               # slack is optional
    assert any("slack" in d.lower() for d in result.degradations)


def test_profiled_run_does_not_clobber_weekly_output(monkeypatch, tmp_path):
    """A profiled dry-run must write to its own file, not over the weekly report."""
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "x " * 200)
    monkeypatch.setattr("sentinel.analyze.llm._sleep", lambda s: None)

    from sentinel.request import apply_profile, profile_from_dict

    client = LLMClient(FakeProvider(), fallback=None, batch_size=8, max_attempts=1)
    common = dict(
        dry_run=True, week="2026-W28",
        collectors={"rss": lambda: list(RAW_BATCH)},
        llm_client=client, deep_available=True,
        article_repo=MemoryArticleRepo(), trend_repo=MemoryTrendRepo(),
        report_repo=MagicMock(), emailer=MagicMock(return_value=True),
        slacker=MagicMock(return_value=True),
    )
    plain = pipe.run_pipeline(_settings(), **common)
    profiled = pipe.run_pipeline(
        apply_profile(_settings(), profile_from_dict(
            {"slug": "ai_health", "theme": "AI in health", "report_type": "deep_research"})),
        **common,
    )

    assert plain.report_path.name == "2026-W28.dryrun.html"
    assert profiled.report_path != plain.report_path
    assert "ai_health" in profiled.report_path.name
    assert plain.report_path.exists() and profiled.report_path.exists()


def test_weekly_watch_profile_keeps_the_historical_filename(monkeypatch, tmp_path):
    """report_type 'weekly_watch' maps to the historical <week>.dryrun.html name."""
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "x " * 200)
    monkeypatch.setattr("sentinel.analyze.llm._sleep", lambda s: None)

    from sentinel.request import apply_profile, profile_from_dict

    result = pipe.run_pipeline(
        apply_profile(_settings(), profile_from_dict({
            "slug": "weekly_ai_saas", "theme": "AI SaaS B2B",
            "report_type": "weekly_watch", "language": "en", "cadence": "weekly"})),
        dry_run=True, week="2026-W28",
        collectors={"rss": lambda: list(RAW_BATCH)},
        llm_client=LLMClient(FakeProvider(), fallback=None, batch_size=8, max_attempts=1),
        deep_available=True,
        article_repo=MemoryArticleRepo(), trend_repo=MemoryTrendRepo(),
        report_repo=MagicMock(), emailer=MagicMock(return_value=True),
        slacker=MagicMock(return_value=True),
    )
    assert result.report_path.name == "2026-W28.dryrun.html"


def _run_profiled(monkeypatch, tmp_path, profile_data, trend_repo):
    """Run the live pipeline under a request profile, everything else mocked."""
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "x " * 200)
    monkeypatch.setattr("sentinel.analyze.llm._sleep", lambda s: None)

    from sentinel.request import apply_profile, profile_from_dict

    settings = apply_profile(_settings(), profile_from_dict(profile_data))
    return pipe.run_pipeline(
        settings, dry_run=False, week="2026-W28",
        collectors={"rss": lambda: list(RAW_BATCH)},
        llm_client=LLMClient(FakeProvider(), fallback=None, batch_size=8, max_attempts=1),
        deep_available=True,
        article_repo=MemoryArticleRepo(), trend_repo=trend_repo,
        report_repo=MagicMock(), emailer=MagicMock(return_value=True),
        slacker=MagicMock(return_value=True),
    )


def test_a_profiled_run_writes_trends_under_its_own_scope(monkeypatch, tmp_path):
    """Phase 18: several themes track trends without touching each other."""
    trend_repo = MemoryTrendRepo()
    # topics the FakeProvider actually returns, so rows really get written
    _run_profiled(monkeypatch, tmp_path,
                  {"slug": "retail_watch", "theme": "AI in retail",
                   "topics": ["product launch", "funding and investment"]}, trend_repo)

    scopes = {scope for (scope, _topic, _week) in trend_repo.rows}
    assert scopes == {"retail_watch"}, "a request must not write into the shared scope"
    assert trend_repo.rows, "its own scope must actually receive the counts"


def test_two_profiles_sharing_a_topic_do_not_overwrite_each_other(monkeypatch, tmp_path):
    """The exact corruption the global UNIQUE(topic, week) key allowed."""
    trend_repo = MemoryTrendRepo()
    for slug in ("retail_watch", "health_watch"):
        _run_profiled(monkeypatch, tmp_path,
                      {"slug": slug, "theme": slug,
                       "topics": ["product launch", "funding and investment"]}, trend_repo)

    keys = set(trend_repo.rows)
    assert ("retail_watch", "product launch", "2026-W28") in keys
    assert ("health_watch", "product launch", "2026-W28") in keys


def test_a_custom_taxonomy_in_a_shared_scope_still_skips_persistence(monkeypatch, tmp_path):
    """The one case namespacing cannot resolve stays refused."""
    trend_repo = MemoryTrendRepo()
    result = _run_profiled(monkeypatch, tmp_path,
                           {"slug": "custom_tax", "theme": "T",
                            "topics": ["product launch"], "trend_scope": "__default__"},
                           trend_repo)

    assert trend_repo.rows == {}
    assert any("not persisted" in s.note for s in result.stages if s.name == "trends")


def test_no_llm_skips_analysis_but_report_still_ships(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "x " * 200)
    repo = MemoryArticleRepo()
    result = pipe.run_pipeline(
        _settings(), dry_run=False, week="2026-W28",
        collectors={"rss": lambda: list(RAW_BATCH)},
        llm_client=None, deep_available=False,           # no LLM available at all
        article_repo=repo, trend_repo=MemoryTrendRepo(), report_repo=MagicMock(),
        emailer=MagicMock(return_value=True), slacker=MagicMock(return_value=True),
    )
    assert result.ok is True
    assert result.stage_names() == EXPECTED_ORDER          # every stage still ran
    assert all(not r.get("summary") for r in repo.rows.values())  # analysis skipped
    assert result.report_path.exists()                     # report + delivery still happen


# --------------------------------------------------------------------------- #
# LLM tiering (_build_llm)
# --------------------------------------------------------------------------- #
def test_build_llm_gemini_is_primary_deep_on(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    result = pipe.PipelineResult()
    client, deep = pipe._build_llm(_settings(), result)
    assert deep is True
    assert client.primary.name == "gemini"
    assert result.degradations == []


def test_build_llm_groq_only_deep_off(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")   # no GEMINI (stripped by conftest)
    result = pipe.PipelineResult()
    client, deep = pipe._build_llm(_settings(), result)
    assert deep is False                        # deep analysis never runs on Groq
    assert client.primary.name == "groq"
    assert any("gemini unavailable" in d.lower() for d in result.degradations)


def test_build_llm_no_keys_returns_none(monkeypatch):
    result = pipe.PipelineResult()              # all secrets stripped by conftest
    client, deep = pipe._build_llm(_settings(), result)
    assert client is None and deep is False
    assert any("no llm" in d.lower() for d in result.degradations)


def test_since_filter_drops_older_articles(monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.report.builder.DEFAULT_OUTPUT_DIR", tmp_path)
    old = _raw("https://a.com/old", "OpenAI old news")
    old["published_at"] = "2026-06-01T00:00:00+00:00"
    monkeypatch.setattr(pipe, "fetch_fulltext", lambda url: "text " * 100)

    client = LLMClient(FakeProvider(), fallback=None, batch_size=8, max_attempts=1)
    repo = MemoryArticleRepo()
    result = pipe.run_pipeline(
        _settings(), dry_run=False, week="2026-W28", since="2026-07-01",
        collectors={"rss": lambda: [RAW_BATCH[0], old]},
        llm_client=client, deep_available=True,
        article_repo=repo, trend_repo=MemoryTrendRepo(), report_repo=MagicMock(),
        emailer=MagicMock(return_value=True), slacker=MagicMock(return_value=True),
    )
    assert "https://a.com/old" not in repo.rows            # filtered by --since
    assert "https://a.com/gpt5" in repo.rows
