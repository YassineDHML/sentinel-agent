"""Configuration loading and validation for Sentinel.

Two distinct sources, deliberately kept separate:

* **Non-secret settings** live in ``config.yaml`` (actors, categories, relevance
  keywords, canonical topic tags, model names, recipients, ...). Loaded and
  validated into the typed :class:`Settings` object.
* **Secrets** (API keys, DB credentials, Gmail app password, Slack webhook) live
  in the environment — ``.env`` locally, GitHub Secrets in CI. Never in YAML.
  Access them via :func:`get_secret` / :func:`require_secrets`.

Run ``python -m sentinel.config`` to print a validated summary of the current
config plus which secrets are present in the environment (see the ``__main__``
block at the bottom).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .logging_conf import get_logger

logger = get_logger("config")

# config.py lives at <repo>/src/sentinel/config.py → repo root is two parents up
# from the package directory.
_PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PACKAGE_DIR.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# Every secret Sentinel may use, loaded from the environment (never from YAML).
# Not all are required in every phase — callers declare what they need via
# require_secrets().
SECRET_NAMES: tuple[str, ...] = (
    "GEMINI_API_KEY",       # LLM (primary)
    "GROQ_API_KEY",         # LLM (fallback)
    "GNEWS_API_KEY",        # news discovery
    "PRODUCTHUNT_TOKEN",    # Product Hunt GraphQL API
    "SUPABASE_URL",         # persistence
    "SUPABASE_KEY",         # persistence
    "GMAIL_ADDRESS",        # email delivery
    "GMAIL_APP_PASSWORD",   # email delivery (app password, not the account password)
    "SLACK_WEBHOOK_URL",    # optional Slack delivery
)

# Soft bounds for the canonical topic list (spec BF-03: ~15-25 tags). Outside
# this range we warn but don't fail — it's a quality signal, not an error.
_TOPICS_MIN, _TOPICS_MAX = 15, 25


class ConfigError(RuntimeError):
    """Raised when configuration is missing or structurally invalid."""


# --------------------------------------------------------------------------- #
# Typed settings objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AppConfig:
    name: str
    report_language: str
    timezone: str
    weeks_history: int
    log_level: str


@dataclass(frozen=True)
class LLMConfig:
    gemini_model: str
    groq_model: str
    batch_size: int


@dataclass(frozen=True)
class Actor:
    name: str
    category: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    actor: str | None = None


@dataclass(frozen=True)
class ReportConfig:
    recipients: list[str]
    subject_prefix: str


@dataclass(frozen=True)
class Settings:
    """Fully validated, read-only view of ``config.yaml``."""

    app: AppConfig
    llm: LLMConfig
    categories: list[str]
    actors: list[Actor]
    relevance_keywords: list[str]
    relevance_exclude: list[str]
    topics: list[str]
    report: ReportConfig
    sources: dict[str, bool]
    feeds: list[Feed]
    discovery_queries: list[str]
    raw: dict[str, Any]  # full parsed YAML, for forward-compatibility


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require(mapping: dict[str, Any], key: str, path: str) -> Any:
    """Return ``mapping[key]`` or raise :class:`ConfigError` if missing/None."""
    if not isinstance(mapping, dict) or key not in mapping or mapping[key] is None:
        raise ConfigError(f"Missing required config key: '{path}'")
    return mapping[key]


def _require_type(value: Any, expected: type | tuple[type, ...], path: str) -> Any:
    if not isinstance(value, expected):
        names = (
            expected.__name__
            if isinstance(expected, type)
            else "/".join(t.__name__ for t in expected)
        )
        raise ConfigError(f"Config key '{path}' must be {names}, got {type(value).__name__}")
    return value


def _build_actors(raw_actors: list[Any], valid_categories: set[str]) -> list[Actor]:
    actors: list[Actor] = []
    for i, entry in enumerate(raw_actors):
        path = f"actors[{i}]"
        _require_type(entry, dict, path)
        name = _require_type(_require(entry, "name", f"{path}.name"), str, f"{path}.name")
        category = _require_type(
            _require(entry, "category", f"{path}.category"), str, f"{path}.category"
        )
        if category not in valid_categories:
            raise ConfigError(
                f"{path}.category '{category}' is not one of the declared categories: "
                f"{sorted(valid_categories)}"
            )
        aliases = entry.get("aliases") or []
        _require_type(aliases, list, f"{path}.aliases")
        actors.append(Actor(name=name, category=category, aliases=[str(a) for a in aliases]))
    return actors


def _build_feeds(raw_feeds: list[Any]) -> list[Feed]:
    feeds: list[Feed] = []
    for i, entry in enumerate(raw_feeds):
        path = f"feeds[{i}]"
        _require_type(entry, dict, path)
        name = _require_type(_require(entry, "name", f"{path}.name"), str, f"{path}.name")
        url = _require_type(_require(entry, "url", f"{path}.url"), str, f"{path}.url")
        actor = entry.get("actor")
        feeds.append(Feed(name=name, url=url, actor=str(actor) if actor else None))
    return feeds


# --------------------------------------------------------------------------- #
# Public loaders
# --------------------------------------------------------------------------- #
def load_settings(
    config_path: str | Path | None = None,
    *,
    load_env: bool = True,
) -> Settings:
    """Load and validate ``config.yaml`` into a :class:`Settings` object.

    Args:
        config_path: Override path to the YAML file. Defaults to ``<repo>/config.yaml``.
        load_env: If ``True`` (default), also load ``<repo>/.env`` into the
            environment so secrets are available via :func:`get_secret`. Existing
            environment variables are **not** overridden (CI secrets win).

    Raises:
        ConfigError: If the file is missing or a required key is absent/invalid.
    """
    if load_env:
        load_dotenv(DEFAULT_ENV_PATH, override=False)

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config.yaml not found at: {path}")

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    _require_type(data, dict, "<root>")

    # --- app --------------------------------------------------------------- #
    app_raw = _require_type(_require(data, "app", "app"), dict, "app")
    app = AppConfig(
        name=str(_require(app_raw, "name", "app.name")),
        report_language=str(app_raw.get("report_language", "en")),
        timezone=str(app_raw.get("timezone", "Europe/Paris")),
        weeks_history=int(app_raw.get("weeks_history", 4)),
        log_level=str(app_raw.get("log_level", "INFO")),
    )

    # --- llm --------------------------------------------------------------- #
    llm_raw = _require_type(_require(data, "llm", "llm"), dict, "llm")
    llm = LLMConfig(
        gemini_model=str(_require(llm_raw, "gemini_model", "llm.gemini_model")),
        groq_model=str(_require(llm_raw, "groq_model", "llm.groq_model")),
        batch_size=int(llm_raw.get("batch_size", 8)),
    )

    # --- categories / actors ---------------------------------------------- #
    categories = [
        str(c) for c in _require_type(_require(data, "categories", "categories"), list, "categories")
    ]
    if not categories:
        raise ConfigError("'categories' must not be empty")

    actors = _build_actors(
        _require_type(_require(data, "actors", "actors"), list, "actors"),
        set(categories),
    )
    if not actors:
        raise ConfigError("'actors' must not be empty")

    # --- relevance --------------------------------------------------------- #
    relevance_raw = _require_type(_require(data, "relevance", "relevance"), dict, "relevance")
    relevance_keywords = [
        str(k)
        for k in _require_type(
            _require(relevance_raw, "keywords", "relevance.keywords"), list, "relevance.keywords"
        )
    ]
    relevance_exclude = [str(k) for k in (relevance_raw.get("exclude") or [])]

    # --- topics (canonical tags) ------------------------------------------ #
    topics = [str(t) for t in _require_type(_require(data, "topics", "topics"), list, "topics")]
    if not topics:
        raise ConfigError("'topics' must not be empty")
    if not (_TOPICS_MIN <= len(topics) <= _TOPICS_MAX):
        logger.warning(
            "Canonical topic list has %d tags; spec recommends %d-%d for reliable trend counting.",
            len(topics),
            _TOPICS_MIN,
            _TOPICS_MAX,
        )

    # --- report ------------------------------------------------------------ #
    report_raw = _require_type(_require(data, "report", "report"), dict, "report")
    report = ReportConfig(
        recipients=[
            str(r)
            for r in _require_type(
                _require(report_raw, "recipients", "report.recipients"),
                list,
                "report.recipients",
            )
        ],
        subject_prefix=str(report_raw.get("subject_prefix", "Sentinel Weekly")),
    )

    # --- sources (toggles) ------------------------------------------------- #
    sources = {str(k): bool(v) for k, v in (data.get("sources") or {}).items()}

    # --- feeds + discovery queries (optional; used by the collectors) ------ #
    feeds = _build_feeds(_require_type(data.get("feeds") or [], list, "feeds"))
    discovery_raw = data.get("discovery") or {}
    _require_type(discovery_raw, dict, "discovery")
    discovery_queries = [str(q) for q in (discovery_raw.get("queries") or [])]

    logger.debug(
        "Loaded config: %d categories, %d actors, %d topics, %d recipients",
        len(categories),
        len(actors),
        len(topics),
        len(report.recipients),
    )

    return Settings(
        app=app,
        llm=llm,
        categories=categories,
        actors=actors,
        relevance_keywords=relevance_keywords,
        relevance_exclude=relevance_exclude,
        topics=topics,
        report=report,
        sources=sources,
        feeds=feeds,
        discovery_queries=discovery_queries,
        raw=data,
    )


# --------------------------------------------------------------------------- #
# Secret access
# --------------------------------------------------------------------------- #
def get_secret(name: str, *, required: bool = True, default: str | None = None) -> str | None:
    """Read a secret from the environment.

    Args:
        name: Environment variable name (see :data:`SECRET_NAMES`).
        required: If ``True`` and the value is missing/empty, raise.
        default: Value to return when not required and unset.

    Raises:
        ConfigError: If ``required`` and the variable is unset or empty.
    """
    if name not in SECRET_NAMES:
        logger.warning("Requested unknown secret name '%s' (not in SECRET_NAMES).", name)
    value = os.environ.get(name) or None
    if value is None:
        if required:
            raise ConfigError(f"Missing required secret environment variable: {name}")
        return default
    return value


def missing_secrets(names: list[str] | tuple[str, ...]) -> list[str]:
    """Return the subset of ``names`` that are absent or empty in the environment."""
    return [n for n in names if not os.environ.get(n)]


def require_secrets(names: list[str] | tuple[str, ...]) -> None:
    """Raise :class:`ConfigError` listing every missing secret in ``names``.

    Call this at the start of a phase/operation that needs specific credentials,
    so failures surface immediately with a clear message rather than deep in an
    integration.
    """
    missing = missing_secrets(names)
    if missing:
        raise ConfigError(f"Missing required secret(s): {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# CLI: python -m sentinel.config
# --------------------------------------------------------------------------- #
def _main() -> int:
    from .logging_conf import setup_logging

    setup_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Config invalid: %s", exc)
        return 1

    lines = [
        "",
        "=== Sentinel configuration ===",
        f"app        : {settings.app.name} | lang={settings.app.report_language} | "
        f"tz={settings.app.timezone} | history={settings.app.weeks_history}w | "
        f"log={settings.app.log_level}",
        f"llm        : gemini={settings.llm.gemini_model} | groq={settings.llm.groq_model} | "
        f"batch={settings.llm.batch_size}",
        f"categories : {', '.join(settings.categories)}",
        f"actors     : {', '.join(a.name for a in settings.actors)} ({len(settings.actors)})",
        f"topics     : {len(settings.topics)} canonical tags",
        f"keywords   : {len(settings.relevance_keywords)} include / "
        f"{len(settings.relevance_exclude)} exclude",
        f"sources    : {', '.join(k for k, v in settings.sources.items() if v) or '(none enabled)'}",
        f"feeds      : {len(settings.feeds)} RSS feed(s)",
        f"discovery  : {len(settings.discovery_queries)} keyword query(ies)",
        f"recipients : {len(settings.report.recipients)} configured",
        "",
        "=== Secrets present in environment ===",
    ]
    present = [n for n in SECRET_NAMES if os.environ.get(n)]
    absent = [n for n in SECRET_NAMES if not os.environ.get(n)]
    lines.append(f"present ({len(present)}): {', '.join(present) or '(none)'}")
    lines.append(f"absent  ({len(absent)}): {', '.join(absent) or '(none)'}")
    lines.append("")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
