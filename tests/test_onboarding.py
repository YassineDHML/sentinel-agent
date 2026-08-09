"""Tests for org-wide request defaults and the shipped request files.

The theme running through this module: **introducing an onboarding file must not
change what an existing request produces.** The defaults are inert by construction
and pinned here so a future edit to ``profiles/onboarding.yaml`` cannot quietly
re-language every report.
"""

from __future__ import annotations

import pytest
import yaml

from sentinel.config import ConfigError
from sentinel.request.onboarding import (
    DEFAULTS,
    Onboarding,
    load_onboarding,
    onboarding_from_dict,
)
from sentinel.request.profile import (
    REQUESTS_DIR,
    load_profile,
    load_profiles,
    profile_from_dict,
)

MINIMAL = {"slug": "x", "theme": "A theme"}


# --------------------------------------------------------------------------- #
# The defaults are inert
# --------------------------------------------------------------------------- #
def test_built_in_defaults_reproduce_the_pre_onboarding_behaviour():
    """These four values were literals in RequestProfile before this module."""
    assert (DEFAULTS.language, DEFAULTS.geo_zone, DEFAULTS.objective) == ("fr", "world", "growth")
    assert (DEFAULTS.horizon_past_months, DEFAULTS.horizon_future_years) == (12, 3)
    assert DEFAULTS.recipients == [] and DEFAULTS.subject_prefix is None


def test_a_profile_built_without_defaults_is_unchanged():
    p = profile_from_dict(MINIMAL)
    assert (p.language, p.geo_zone, p.objective) == ("fr", "world", "growth")
    assert p.recipients == [] and p.subject_prefix is None


def test_a_missing_onboarding_file_is_normal_not_an_error(tmp_path):
    assert load_onboarding(tmp_path / "nope.yaml") is DEFAULTS


def test_a_present_but_broken_onboarding_file_raises(tmp_path):
    """Silently ignoring a typo here would change every report's language."""
    path = tmp_path / "onboarding.yaml"
    path.write_text("horizon:\n  past_months: not-a-number\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_onboarding(path)


# --------------------------------------------------------------------------- #
# Inheritance
# --------------------------------------------------------------------------- #
def test_a_request_inherits_every_omitted_key():
    defaults = Onboarding(language="en", geo_zone="europe", objective="ma",
                          horizon_past_months=6, horizon_future_years=5,
                          recipients=["ceo@x.com"], subject_prefix="Veille")
    p = profile_from_dict(MINIMAL, defaults=defaults)

    assert (p.language, p.geo_zone, p.objective) == ("en", "europe", "ma")
    assert (p.horizon_past_months, p.horizon_future_years) == (6, 5)
    assert p.recipients == ["ceo@x.com"] and p.subject_prefix == "Veille"


def test_a_request_always_beats_the_default():
    defaults = Onboarding(language="en", recipients=["ceo@x.com"])
    p = profile_from_dict({**MINIMAL, "language": "fr", "recipients": ["team@x.com"]},
                          defaults=defaults)
    assert p.language == "fr" and p.recipients == ["team@x.com"]


def test_an_inherited_company_ref_satisfies_a_competitor_request():
    defaults = Onboarding(company_ref="profiles/company.yaml")
    p = profile_from_dict({**MINIMAL, "report_type": "competitor_scan"}, defaults=defaults)
    assert p.company_ref == "profiles/company.yaml"


def test_a_competitor_request_still_needs_a_company_from_somewhere():
    with pytest.raises(ConfigError, match="company_ref"):
        profile_from_dict({**MINIMAL, "report_type": "competitor_scan"})


def test_onboarding_ignores_unknown_keys():
    assert onboarding_from_dict({"language": "en", "future_feature": 1}).language == "en"


# --------------------------------------------------------------------------- #
# The shipped files
# --------------------------------------------------------------------------- #
def test_every_shipped_request_profile_loads():
    profiles = load_profiles()
    assert len(profiles) == len(list(REQUESTS_DIR.glob("*.yaml")))


def test_the_shipped_onboarding_file_keeps_the_language_default():
    """Guards the one field that would silently change every report."""
    assert load_onboarding().language == "fr"


def test_the_production_weekly_watch_is_not_dispatched():
    """Tripwire. weekly.yml owns it; a scheduled duplicate = two Monday emails."""
    profile = load_profile(REQUESTS_DIR / "weekly_ai_saas.yaml")
    assert profile.scheduled is False
    assert profile.cadence == "weekly"


def test_the_weekly_watch_profile_still_declares_english():
    """An org default of `fr` must not re-language the production report."""
    assert load_profile(REQUESTS_DIR / "weekly_ai_saas.yaml").language == "en"


def test_the_weekly_watch_profile_shares_the_historical_trend_scope():
    """Tripwire. Its own scope would mean an empty history and everything NEW."""
    from sentinel.db.repositories import DEFAULT_TREND_SCOPE

    profile = load_profile(REQUESTS_DIR / "weekly_ai_saas.yaml")
    assert profile.effective_trend_scope == DEFAULT_TREND_SCOPE


def test_every_other_request_keeps_its_own_trend_scope():
    """Sharing is opt-in; only the production watch may point at __default__."""
    from sentinel.db.repositories import DEFAULT_TREND_SCOPE

    for profile in load_profiles():
        if profile.slug == "weekly_ai_saas":
            continue
        assert profile.effective_trend_scope != DEFAULT_TREND_SCOPE, profile.slug
        assert profile.effective_trend_scope == profile.slug


def test_the_weekly_watch_profile_overrides_nothing_that_config_owns():
    """Its whole point: running with it must equal running without it."""
    profile = load_profile(REQUESTS_DIR / "weekly_ai_saas.yaml")
    assert profile.topics is None and profile.feeds is None
    assert profile.relevance_keywords is None and profile.discovery_queries is None
    assert profile.recipients == []


def test_scheduled_requests_declare_a_recurring_cadence():
    for profile in load_profiles():
        if profile.scheduled:
            assert profile.cadence != "once", f"{profile.slug} can never fire"


def test_a_malformed_profile_is_skipped_not_fatal(tmp_path):
    (tmp_path / "good.yaml").write_text("theme: fine\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("theme: null\n", encoding="utf-8")

    profiles = load_profiles(tmp_path)

    assert [p.slug for p in profiles] == ["good"]


def test_request_directory_yaml_is_well_formed():
    for path in REQUESTS_DIR.glob("*.yaml"):
        with path.open(encoding="utf-8") as fh:
            assert isinstance(yaml.safe_load(fh), dict), path.name
