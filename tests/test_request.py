"""Tests for request profiles, the settings overlay and locale resolution.

Pure logic — no network, no DB, no LLM. The central invariant asserted here is
that a run WITHOUT a profile behaves exactly as the historical pipeline.
"""

from __future__ import annotations

import textwrap

import pytest

from sentinel.config import ConfigError, load_settings
from sentinel.request import (
    GEO_ZONES,
    RequestProfile,
    apply_profile,
    assert_trend_safe,
    load_profile,
    profile_from_dict,
    profile_of,
    resolve_locale,
    theme_of,
)
from sentinel.request.locale import LocaleError, zone_is_global

MINIMAL = {"slug": "s", "theme": "Quantum computing in finance"}


def _settings():
    return load_settings(load_env=False)


# --------------------------------------------------------------------------- #
# Locale ({B} x {C})
# --------------------------------------------------------------------------- #
def test_default_locale_reproduces_historical_google_news_params():
    """The (en, world) default must match the previously hardcoded URL exactly."""
    params = resolve_locale().googlenews_params()
    assert params == {"hl": "en-US", "gl": "US", "ceid": "US:en"}


def test_default_locale_reproduces_historical_gnews_params():
    """Historically GNews received lang only — no country."""
    assert resolve_locale().gnews_params(include_country=False) == {"lang": "en"}


def test_french_france_locale():
    loc = resolve_locale("fr", "france")
    assert loc.googlenews_params() == {"hl": "fr", "gl": "FR", "ceid": "FR:fr"}
    assert loc.gnews_params(include_country=True) == {"lang": "fr", "country": "fr"}


def test_zone_picks_language_appropriate_country():
    assert resolve_locale("en", "europe").country == "GB"
    assert resolve_locale("fr", "europe").country == "FR"
    assert resolve_locale("en", "world").country == "US"
    assert resolve_locale("fr", "world").country == "FR"     # language home
    assert resolve_locale("en", "asia").country == "SG"


def test_all_declared_zones_resolve_for_supported_languages():
    for zone in GEO_ZONES:
        for lang in ("en", "fr"):
            loc = resolve_locale(lang, zone)
            assert loc.country and loc.hl and ":" in loc.ceid


def test_unknown_zone_or_language_raises():
    with pytest.raises(LocaleError):
        resolve_locale("en", "atlantis")
    with pytest.raises(LocaleError):
        resolve_locale("kl", "world")


def test_zone_is_global():
    assert zone_is_global("world") and not zone_is_global("france")


# --------------------------------------------------------------------------- #
# Profile validation
# --------------------------------------------------------------------------- #
def test_minimal_profile_gets_sane_defaults():
    p = profile_from_dict(MINIMAL)
    assert p.theme == "Quantum computing in finance"
    assert p.language == "fr" and p.geo_zone == "world"
    assert p.horizon_past_months == 12 and p.horizon_future_years == 3
    assert p.objective == "growth" and p.cadence == "monthly"
    assert p.word_target == 3000
    # nothing overridden -> None so config.yaml values survive
    assert p.discovery_queries is None and p.topics is None
    assert not p.has_custom_taxonomy


def test_theme_is_required_and_non_empty():
    with pytest.raises(ConfigError, match="theme"):
        profile_from_dict({"slug": "s"})
    with pytest.raises(ConfigError, match="must not be empty"):
        profile_from_dict({"slug": "s", "theme": "   "})


def test_closed_enums_are_enforced():
    for key, bad in (("geo_zone", "mars"), ("objective", "vibes"),
                     ("cadence", "fortnightly"), ("report_type", "novel"),
                     ("language", "kl")):
        with pytest.raises(ConfigError):
            profile_from_dict({**MINIMAL, key: bad})


def test_horizon_validation():
    with pytest.raises(ConfigError, match="past_months"):
        profile_from_dict({**MINIMAL, "horizon": {"past_months": 0}})
    with pytest.raises(ConfigError, match="future_years"):
        profile_from_dict({**MINIMAL, "horizon": {"future_years": -1}})


def test_word_target_sanity():
    with pytest.raises(ConfigError, match="implausibly small"):
        profile_from_dict({**MINIMAL, "word_target": 50})


def test_competitor_scan_requires_a_company_ref():
    with pytest.raises(ConfigError, match="company_ref"):
        profile_from_dict({**MINIMAL, "report_type": "competitor_scan"})
    ok = profile_from_dict({**MINIMAL, "report_type": "competitor_scan",
                            "company_ref": "profiles/company.yaml"})
    assert ok.company_ref == "profiles/company.yaml"


def test_max_competitors_defaults_to_none_so_the_engine_decides():
    """None, not 10 — the number must live in exactly one place when nobody
    overrides it, otherwise the profile layer silently forks the default."""
    assert profile_from_dict(MINIMAL).max_competitors is None


def test_max_competitors_is_accepted_and_coerced():
    assert profile_from_dict({**MINIMAL, "max_competitors": 6}).max_competitors == 6
    # YAML can hand us a string; the engine slices a list with it, so coerce.
    assert profile_from_dict({**MINIMAL, "max_competitors": "3"}).max_competitors == 3


@pytest.mark.parametrize("bad", [0, -1, 21, 200])
def test_max_competitors_outside_the_band_is_refused(bad):
    """0 would produce an empty list and fail the run with 'no competitor could be
    sourced'; 200 would burn a day of free-tier quota on one report."""
    with pytest.raises(ConfigError, match="max_competitors"):
        profile_from_dict({**MINIMAL, "max_competitors": bad})


def test_a_non_numeric_max_competitors_raises_config_error_not_value_error():
    """load_profiles() only catches ConfigError. A bare int() raising ValueError
    would abort the entire daily dispatch instead of skipping one bad file."""
    with pytest.raises(ConfigError, match="must be an integer"):
        profile_from_dict({**MINIMAL, "max_competitors": "six"})


def test_empty_topics_override_is_rejected():
    """An empty list would silently disable classification; omit the key instead."""
    with pytest.raises(ConfigError, match="empty list"):
        profile_from_dict({**MINIMAL, "topics": []})


def test_custom_taxonomy_is_flagged():
    p = profile_from_dict({**MINIMAL, "topics": ["a", "b"]})
    assert p.has_custom_taxonomy


def test_feeds_override_parsed_into_feed_objects():
    p = profile_from_dict({**MINIMAL, "feeds": [
        {"name": "X", "url": "https://x/feed", "actor": "ActorX"}]})
    assert p.feeds[0].name == "X" and p.feeds[0].actor == "ActorX"


def test_describe_is_one_line_and_informative():
    p = profile_from_dict({**MINIMAL, "sector": "finance"})
    d = p.describe()
    assert "\n" not in d
    assert "Quantum computing" in d and "finance" in d


# --------------------------------------------------------------------------- #
# Loading from disk (the shipped profiles must be valid)
# --------------------------------------------------------------------------- #
def test_shipped_profiles_are_valid():
    from sentinel.request.profile import REQUESTS_DIR, list_profiles

    files = list_profiles()
    assert files, f"no request profiles found in {REQUESTS_DIR}"
    for path in files:
        profile = load_profile(path)
        assert profile.slug and profile.theme


def test_load_profile_derives_slug_from_filename(tmp_path):
    p = tmp_path / "my_request.yaml"
    p.write_text("theme: Some theme\n", encoding="utf-8")
    assert load_profile(p).slug == "my_request"


def test_load_profile_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_profile("does_not_exist_anywhere.yaml")


def test_load_profile_rejects_invalid_yaml_content(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""
        theme: Fine
        geo_zone: nowhere
    """), encoding="utf-8")
    with pytest.raises(ConfigError, match="geo_zone"):
        load_profile(p)


# --------------------------------------------------------------------------- #
# Overlay — the critical no-profile-means-no-change invariant
# --------------------------------------------------------------------------- #
def test_apply_profile_none_returns_settings_unchanged():
    s = _settings()
    assert apply_profile(s, None) is s
    assert profile_of(s) is None


def test_overlay_sets_language_and_carries_profile():
    s = apply_profile(_settings(), profile_from_dict({**MINIMAL, "language": "fr"}))
    assert s.app.report_language == "fr"
    assert profile_of(s).theme == "Quantum computing in finance"


def test_overlay_only_overrides_keys_the_profile_sets():
    base = _settings()
    s = apply_profile(base, profile_from_dict(MINIMAL))   # overrides nothing but language
    assert s.discovery_queries == base.discovery_queries
    assert s.relevance_keywords == base.relevance_keywords
    assert s.topics == base.topics
    assert s.feeds == base.feeds
    assert s.report.recipients == base.report.recipients


def test_overlay_applies_declared_overrides():
    base = _settings()
    s = apply_profile(base, profile_from_dict({
        **MINIMAL,
        "discovery_queries": ["q1", "q2"],
        "relevance_keywords": ["k1"],
        "relevance_exclude": ["x1"],
        "recipients": ["a@b.c"],
        "subject_prefix": "Custom",
    }))
    assert s.discovery_queries == ["q1", "q2"]
    assert s.relevance_keywords == ["k1"]
    assert s.relevance_exclude == ["x1"]
    assert s.report.recipients == ["a@b.c"]
    assert s.report.subject_prefix == "Custom"
    assert base.discovery_queries != ["q1", "q2"], "base settings must not be mutated"


def test_theme_of_falls_back_for_unparameterized_runs():
    assert theme_of(_settings()) == "AI SaaS B2B"
    s = apply_profile(_settings(), profile_from_dict(MINIMAL))
    assert theme_of(s) == "Quantum computing in finance"


def test_a_request_gets_its_own_trend_scope_by_default():
    """Own history by construction: you opt IN to sharing, never out."""
    from sentinel.request import trend_scope_of

    s = apply_profile(_settings(), profile_from_dict(MINIMAL))
    assert trend_scope_of(s) == "s"          # the slug, not the shared scope


def test_an_unprofiled_run_reads_the_historical_scope():
    from sentinel.db.repositories import DEFAULT_TREND_SCOPE
    from sentinel.request import trend_scope_of

    assert trend_scope_of(_settings()) == DEFAULT_TREND_SCOPE


def test_a_request_can_opt_into_pooling_with_another_scope():
    from sentinel.request import trend_scope_of

    s = apply_profile(_settings(),
                      profile_from_dict({**MINIMAL, "trend_scope": "__default__"}))
    assert trend_scope_of(s) == "__default__"


def test_a_custom_taxonomy_is_allowed_in_its_own_scope():
    """Phase 18 lifts the Phase 13 restriction: namespacing makes this safe."""
    own = apply_profile(_settings(), profile_from_dict({**MINIMAL, "topics": ["x", "y"]}))
    assert assert_trend_safe(own) is True


def test_a_custom_taxonomy_is_still_refused_in_a_shared_scope():
    """The one collision no key can disambiguate: two taxonomies, one namespace."""
    shared = apply_profile(_settings(), profile_from_dict(
        {**MINIMAL, "topics": ["x", "y"], "trend_scope": "__default__"}))
    assert assert_trend_safe(shared) is False


def test_trend_write_allowed_without_any_profile():
    assert assert_trend_safe(_settings()) is True


# --------------------------------------------------------------------------- #
# Profile is a frozen value object
# --------------------------------------------------------------------------- #
def test_profile_is_immutable():
    p = profile_from_dict(MINIMAL)
    with pytest.raises(Exception):
        p.theme = "something else"  # type: ignore[misc]


def test_profile_can_be_constructed_directly():
    p = RequestProfile(slug="s", theme="t")
    assert p.language == "fr" and p.report_type == "deep_research"


def test_requests_dir_does_not_shadow_http_library():
    """The repo-root `requests/` folder must never shadow the HTTP library.

    It is safe only because it has no __init__.py (a namespace-package portion
    loses to a real installed package). Adding one would silently break every
    collector when running from the repo root.
    """
    import requests as http_requests

    from sentinel.request.profile import REQUESTS_DIR

    assert hasattr(http_requests, "get"), "`requests` is not the HTTP library!"
    assert "site-packages" in http_requests.__file__.replace("\\", "/")
    assert not (REQUESTS_DIR / "__init__.py").exists(), (
        "requests/__init__.py would shadow the `requests` HTTP library — remove it"
    )
