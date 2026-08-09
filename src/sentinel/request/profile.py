"""Request profiles — the {A}..{F} parameters of a report request.

The team's specification asks the user for six variables via a request form:

===== ======================= ==================================================
{A}   theme                   free text, the subject of the watch/report
{B}   language                report language (default from onboarding)
{C}   geographic zone         world / europe / france / north_america / ...
{D}   time horizon            retrospective months + projection years
{E}   sector focus            optional industry lens (health, retail, ...)
{F}   priority objective      growth / innovation / risk_reduction / ...
===== ======================= ==================================================

A profile is a **frozen** value object loaded from ``requests/<slug>.yaml``. It is
deliberately file-based for now: git-reviewable, diffable, testable offline, and
upgradeable to a database table later without changing call sites.

A profile carries only *intent*. Turning it into effective settings for the
pipeline is :func:`sentinel.request.overlay.apply_profile`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, Feed, _require, _require_type
from ..logging_conf import get_logger
from .locale import GEO_ZONES, SUPPORTED_LANGUAGES, Locale, resolve_locale
from .onboarding import DEFAULTS as ONBOARDING_DEFAULTS
from .onboarding import Onboarding, load_onboarding

logger = get_logger("request.profile")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REQUESTS_DIR = REPO_ROOT / "requests"

# What kind of deliverable a request produces.
REPORT_TYPES: tuple[str, ...] = ("weekly_watch", "deep_research", "competitor_scan")

# {F} — the executive's priority objective. Closed set so prompts can rely on it.
OBJECTIVES: tuple[str, ...] = (
    "growth",
    "innovation",
    "risk_reduction",
    "differentiation",
    "investment",
    "ma",
    "other",
)

CADENCES: tuple[str, ...] = ("weekly", "monthly", "quarterly", "once")

DEFAULT_WORD_TARGET = 3000
DEFAULT_WORD_TOLERANCE = 250


@dataclass(frozen=True)
class RequestProfile:
    """One report request: the {A}..{F} parameters plus delivery/scheduling intent."""

    slug: str
    theme: str                                   # {A}
    report_type: str = "deep_research"
    language: str = "fr"                         # {B}
    geo_zone: str = "world"                      # {C}
    horizon_past_months: int = 12                # {D} retrospective
    horizon_future_years: int = 3                 # {D} projection
    sector: str | None = None                     # {E}
    objective: str = "growth"                     # {F}

    # scheduling / delivery
    cadence: str = "monthly"
    # Whether the daily dispatcher may pick this request up. False keeps the
    # request runnable by hand (--profile) without ever firing on a schedule.
    scheduled: bool = True
    recipients: list[str] = field(default_factory=list)
    subject_prefix: str | None = None

    # length target (deep research)
    word_target: int = DEFAULT_WORD_TARGET
    word_tolerance: int = DEFAULT_WORD_TOLERANCE

    # optional pipeline overrides — omitted keys keep their config.yaml values
    discovery_queries: list[str] | None = None
    relevance_keywords: list[str] | None = None
    relevance_exclude: list[str] | None = None
    topics: list[str] | None = None
    feeds: list[Feed] | None = None
    benchmark_houses: list[str] = field(default_factory=list)

    # Which trend history this request contributes to. ``None`` means "my own"
    # (the slug). Set it to pool with another request — or to ``__default__`` to
    # contribute to the historical single-theme watch. See `trend_scope_of`.
    trend_scope: str | None = None

    # capability 2: which company profile to compare against
    company_ref: str | None = None

    @property
    def locale(self) -> Locale:
        """Source-side language/region parameters for this request."""
        return resolve_locale(self.language, self.geo_zone)

    @property
    def has_custom_taxonomy(self) -> bool:
        """True when the profile overrides the canonical topic list.

        Significant for trend storage: since trends are namespaced per request, a
        custom taxonomy is safe in its *own* scope but never in a shared one — see
        :func:`sentinel.request.overlay.assert_trend_safe`.
        """
        return self.topics is not None

    @property
    def effective_trend_scope(self) -> str:
        """The trend namespace this request reads and writes.

        Defaults to the slug, so **a new request gets its own history by
        construction** — you have to opt *in* to sharing, never remember to opt
        out. That is the whole safety property of the namespacing.
        """
        return self.trend_scope or self.slug

    def describe(self) -> str:
        """One-line human summary, for logs and report footers."""
        bits = [f"{self.slug}: {self.theme!r}", f"lang={self.language}", f"zone={self.geo_zone}"]
        if self.sector:
            bits.append(f"sector={self.sector}")
        bits.append(f"objective={self.objective}")
        bits.append(f"horizon=-{self.horizon_past_months}m/+{self.horizon_future_years}y")
        bits.append(f"cadence={self.cadence}{'' if self.scheduled else ' (unscheduled)'}")
        return " · ".join(bits)


# --------------------------------------------------------------------------- #
# Loading + validation
# --------------------------------------------------------------------------- #
def _str_list(value: Any, path: str) -> list[str] | None:
    if value is None:
        return None
    _require_type(value, list, path)
    return [str(v) for v in value]


def _build_feeds(raw: Any, path: str) -> list[Feed] | None:
    if raw is None:
        return None
    _require_type(raw, list, path)
    feeds: list[Feed] = []
    for i, entry in enumerate(raw):
        p = f"{path}[{i}]"
        _require_type(entry, dict, p)
        name = str(_require(entry, "name", f"{p}.name"))
        url = str(_require(entry, "url", f"{p}.url"))
        actor = entry.get("actor")
        feeds.append(Feed(name=name, url=url, actor=str(actor) if actor else None))
    return feeds


def profile_from_dict(
    data: dict[str, Any],
    *,
    slug: str | None = None,
    defaults: Onboarding | None = None,
) -> RequestProfile:
    """Validate a parsed mapping into a :class:`RequestProfile`.

    Args:
        data: The parsed YAML mapping.
        slug: Fallback slug when the file doesn't set one (usually the filename).
        defaults: Org-wide answers for keys the request omits (see
            :mod:`sentinel.request.onboarding`). ``None`` uses the built-in
            defaults, which are byte-identical to the pre-onboarding behaviour —
            so every existing caller and test is unaffected.

    Raises:
        ConfigError: On a missing required key or an out-of-enum value.
    """
    _require_type(data, dict, "<root>")
    base = defaults or ONBOARDING_DEFAULTS
    resolved_slug = str(data.get("slug") or slug or "")
    if not resolved_slug:
        raise ConfigError("request profile needs a 'slug' (or a filename to derive it from)")

    theme = str(_require(data, "theme", "theme")).strip()
    if not theme:
        raise ConfigError("request profile 'theme' must not be empty")

    report_type = str(data.get("report_type", "deep_research"))
    if report_type not in REPORT_TYPES:
        raise ConfigError(f"report_type {report_type!r} must be one of {list(REPORT_TYPES)}")

    language = str(data.get("language", base.language)).lower()
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(
            f"language {language!r} not supported; expected one of {list(SUPPORTED_LANGUAGES)}"
        )

    geo_zone = str(data.get("geo_zone", base.geo_zone)).lower()
    if geo_zone not in GEO_ZONES:
        raise ConfigError(f"geo_zone {geo_zone!r} must be one of {list(GEO_ZONES)}")

    objective = str(data.get("objective", base.objective)).lower()
    if objective not in OBJECTIVES:
        raise ConfigError(f"objective {objective!r} must be one of {list(OBJECTIVES)}")

    cadence = str(data.get("cadence", "monthly")).lower()
    if cadence not in CADENCES:
        raise ConfigError(f"cadence {cadence!r} must be one of {list(CADENCES)}")

    horizon = data.get("horizon") or {}
    _require_type(horizon, dict, "horizon")
    past_months = int(horizon.get("past_months", base.horizon_past_months))
    future_years = int(horizon.get("future_years", base.horizon_future_years))
    if past_months <= 0:
        raise ConfigError("horizon.past_months must be positive")
    if future_years < 0:
        raise ConfigError("horizon.future_years must not be negative")

    word_target = int(data.get("word_target", DEFAULT_WORD_TARGET))
    word_tolerance = int(data.get("word_tolerance", DEFAULT_WORD_TOLERANCE))
    if word_target < 200:
        raise ConfigError("word_target is implausibly small (< 200)")
    if word_tolerance < 0:
        raise ConfigError("word_tolerance must not be negative")

    topics = _str_list(data.get("topics"), "topics")
    if topics is not None and not topics:
        raise ConfigError("'topics' override must not be an empty list (omit the key instead)")

    company_ref = data.get("company_ref") or base.company_ref
    if report_type == "competitor_scan" and not company_ref:
        raise ConfigError("report_type 'competitor_scan' requires a 'company_ref'")

    sector = data.get("sector")
    profile = RequestProfile(
        slug=resolved_slug,
        theme=theme,
        report_type=report_type,
        language=language,
        geo_zone=geo_zone,
        horizon_past_months=past_months,
        horizon_future_years=future_years,
        sector=str(sector).strip() if sector else None,
        objective=objective,
        cadence=cadence,
        scheduled=bool(data.get("scheduled", True)),
        recipients=_str_list(data.get("recipients"), "recipients") or list(base.recipients),
        subject_prefix=(str(data["subject_prefix"]) if data.get("subject_prefix")
                        else base.subject_prefix),
        word_target=word_target,
        word_tolerance=word_tolerance,
        discovery_queries=_str_list(data.get("discovery_queries"), "discovery_queries"),
        relevance_keywords=_str_list(data.get("relevance_keywords"), "relevance_keywords"),
        relevance_exclude=_str_list(data.get("relevance_exclude"), "relevance_exclude"),
        topics=topics,
        feeds=_build_feeds(data.get("feeds"), "feeds"),
        benchmark_houses=_str_list(data.get("benchmark_houses"), "benchmark_houses") or [],
        company_ref=str(company_ref) if company_ref else None,
        trend_scope=(str(data["trend_scope"]).strip() if data.get("trend_scope") else None),
    )
    # fail fast on an unusable language/zone combination rather than at query time
    profile.locale
    return profile


def load_profile(path: str | Path, *, defaults: Onboarding | None = None) -> RequestProfile:
    """Load and validate a request profile from a YAML file.

    The slug defaults to the filename stem when the file doesn't set one, and any
    key the file omits falls back to ``profiles/onboarding.yaml``.
    """
    p = Path(path)
    if not p.exists():
        # allow a bare slug: load_profile("ai_healthcare_fr")
        candidate = REQUESTS_DIR / f"{p.name}.yaml"
        if candidate.exists():
            p = candidate
        else:
            raise ConfigError(f"request profile not found: {path}")
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    profile = profile_from_dict(data, slug=p.stem, defaults=defaults or load_onboarding())
    logger.info("Loaded request profile — %s", profile.describe())
    return profile


def load_profiles(
    directory: str | Path | None = None, *, defaults: Onboarding | None = None
) -> list[RequestProfile]:
    """Load every request profile in ``directory``, skipping unreadable ones.

    One malformed file must not stop the scheduler from dispatching the others,
    so a broken profile is logged and dropped rather than raised.
    """
    shared = defaults or load_onboarding()
    profiles: list[RequestProfile] = []
    for path in list_profiles(directory):
        try:
            profiles.append(load_profile(path, defaults=shared))
        except (ConfigError, yaml.YAMLError) as exc:
            logger.error("Skipping unusable request profile %s: %s", path.name, exc)
    return profiles


def list_profiles(directory: str | Path | None = None) -> list[Path]:
    """Return the available request-profile files, sorted."""
    d = Path(directory) if directory else REQUESTS_DIR
    return sorted(d.glob("*.yaml")) if d.exists() else []
