"""Tests for sentinel.config (deferred from Phase 0). No secrets, no live calls."""

from __future__ import annotations

import textwrap

import pytest

from sentinel.config import (
    ConfigError,
    Settings,
    get_secret,
    load_settings,
    missing_secrets,
    require_secrets,
)


def test_load_settings_reads_project_config():
    """The real config.yaml loads and satisfies the structural invariants.

    Deliberately asserts INVARIANTS, not literal contents: the monitored scope
    (categories/actors/topics) is expected to change as the watch is retuned or
    parameterized, and a test that pins exact values would red-line CI on every
    legitimate config edit.
    """
    settings = load_settings(load_env=False)
    assert isinstance(settings, Settings)
    assert settings.app.name
    assert settings.categories, "at least one category must be declared"
    assert len(settings.actors) >= 1
    assert len(settings.relevance_keywords) >= 1
    # canonical topic list stays in the range the trend engine is tuned for
    assert 15 <= len(settings.topics) <= 25
    assert len(set(settings.topics)) == len(settings.topics), "topics must be unique"
    # model names must come from config, never hardcoded in code
    assert settings.llm.gemini_model
    assert settings.llm.groq_model
    assert settings.llm.batch_size >= 1
    # every actor's category must be one of the declared categories
    assert all(a.category in settings.categories for a in settings.actors)
    assert settings.report.recipients, "at least one recipient must be configured"


def test_missing_config_file_raises():
    with pytest.raises(ConfigError, match="config.yaml not found"):
        load_settings("does_not_exist_config.yaml", load_env=False)


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_missing_required_key_raises(tmp_path):
    cfg = _write(
        tmp_path,
        """
        app: {name: Test}
        llm: {gemini_model: g, groq_model: q}
        categories: [A]
        actors: [{name: X, category: A}]
        relevance: {keywords: [ai]}
        topics: [t]
        # 'report' section omitted -> should raise
        """,
    )
    with pytest.raises(ConfigError, match="report"):
        load_settings(cfg, load_env=False)


def test_actor_with_unknown_category_raises(tmp_path):
    cfg = _write(
        tmp_path,
        """
        app: {name: Test}
        llm: {gemini_model: g, groq_model: q}
        categories: [A]
        actors: [{name: X, category: Nope}]
        relevance: {keywords: [ai]}
        topics: [t]
        report: {recipients: [x@y.z]}
        """,
    )
    with pytest.raises(ConfigError, match="not one of the declared categories"):
        load_settings(cfg, load_env=False)


def test_require_secrets(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY.*SUPABASE_URL|SUPABASE_URL.*GEMINI_API_KEY"):
        require_secrets(["GEMINI_API_KEY", "SUPABASE_URL"])

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("SUPABASE_URL", "u")
    require_secrets(["GEMINI_API_KEY", "SUPABASE_URL"])  # no raise


def test_missing_secrets_returns_absent(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert missing_secrets(["SUPABASE_URL", "SUPABASE_KEY"]) == ["SUPABASE_KEY"]


def test_get_secret_required_and_optional(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert get_secret("SLACK_WEBHOOK_URL", required=False, default="fallback") == "fallback"
    with pytest.raises(ConfigError):
        get_secret("SLACK_WEBHOOK_URL", required=True)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hook")
    assert get_secret("SLACK_WEBHOOK_URL") == "https://hook"
