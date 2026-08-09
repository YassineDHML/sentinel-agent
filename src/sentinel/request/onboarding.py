"""Organisation-level defaults for report requests.

The specification defines {B} as *"the report language (default: the one from
on-boarding)"*. Until Phase 13 that default was a literal in the
:class:`~sentinel.request.profile.RequestProfile` dataclass, which is the wrong
home for it: it is a property of the organisation, not of the code.

``profiles/onboarding.yaml`` holds those org-wide answers once, and every request
that omits a key inherits it. A request that sets the key always wins.

Loading is **fail-soft**: a missing or empty file yields the shipped defaults, so
nothing here can break a run. The file is optional by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, _require_type
from ..logging_conf import get_logger

logger = get_logger("request.onboarding")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"
DEFAULT_ONBOARDING_FILE = PROFILES_DIR / "onboarding.yaml"


@dataclass(frozen=True)
class Onboarding:
    """Org-wide answers a request may inherit.

    Every field mirrors a :class:`RequestProfile` field and carries **exactly the
    value that field defaulted to before this module existed**, so an absent
    ``profiles/onboarding.yaml`` is indistinguishable from the historical
    behaviour.
    """

    language: str = "fr"                                   # {B}
    geo_zone: str = "world"                                # {C}
    objective: str = "growth"                              # {F}
    horizon_past_months: int = 12                          # {D}
    horizon_future_years: int = 3                          # {D}
    recipients: list[str] = field(default_factory=list)
    subject_prefix: str | None = None
    company_ref: str | None = None

    def describe(self) -> str:
        bits = [f"language={self.language}", f"zone={self.geo_zone}",
                f"objective={self.objective}"]
        if self.recipients:
            bits.append(f"recipients={len(self.recipients)}")
        return " · ".join(bits)


#: The defaults used when no onboarding file is present.
DEFAULTS = Onboarding()


def onboarding_from_dict(data: dict[str, Any]) -> Onboarding:
    """Validate a parsed mapping into an :class:`Onboarding`.

    Raises:
        ConfigError: If the file is structurally wrong (a present-but-broken file
            is an error; an *absent* file is not — see :func:`load_onboarding`).
    """
    _require_type(data, dict, "<root>")

    def _str(key: str, default: str | None) -> str | None:
        value = data.get(key)
        return str(value).strip() if value not in (None, "") else default

    def _int(key: str, default: int) -> int:
        value = data.get(key)
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"onboarding '{key}' must be an integer") from exc

    recipients = data.get("recipients") or []
    _require_type(recipients, list, "recipients")

    horizon = data.get("horizon") or {}
    _require_type(horizon, dict, "horizon")

    return Onboarding(
        language=str(_str("language", DEFAULTS.language)).lower(),
        geo_zone=str(_str("geo_zone", DEFAULTS.geo_zone)).lower(),
        objective=str(_str("objective", DEFAULTS.objective)).lower(),
        horizon_past_months=_int_from(horizon, "past_months", DEFAULTS.horizon_past_months),
        horizon_future_years=_int_from(horizon, "future_years", DEFAULTS.horizon_future_years),
        recipients=[str(r).strip() for r in recipients if str(r).strip()],
        subject_prefix=_str("subject_prefix", None),
        company_ref=_str("company_ref", None),
    )


def _int_from(mapping: dict[str, Any], key: str, default: int) -> int:
    value = mapping.get(key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"onboarding 'horizon.{key}' must be an integer") from exc


def load_onboarding(path: str | Path | None = None) -> Onboarding:
    """Load org defaults from ``profiles/onboarding.yaml``.

    A missing file is normal and returns :data:`DEFAULTS`. A file that exists but
    cannot be parsed is *not* silently ignored — that would hide a typo that
    silently changes every report's language — it raises.
    """
    p = Path(path) if path else DEFAULT_ONBOARDING_FILE
    if not p.exists():
        logger.debug("No onboarding file at %s; using built-in defaults.", p)
        return DEFAULTS
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    onboarding = onboarding_from_dict(data)
    logger.debug("Loaded onboarding defaults — %s", onboarding.describe())
    return onboarding
