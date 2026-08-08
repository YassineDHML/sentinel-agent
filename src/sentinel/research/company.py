"""The company profile — who "we" are.

The competitor report is written *from our point of view*: it ranks threats to us
and proposes how we should respond. That requires knowing what we sell, to whom,
and how we position — which until now existed only as the literal string "Welyne"
hardcoded inside a prompt.

Loaded from ``profiles/company.yaml`` so it is git-reviewable and editable without
touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, _require, _require_type
from ..logging_conf import get_logger

logger = get_logger("research.company")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"
DEFAULT_COMPANY_FILE = PROFILES_DIR / "company.yaml"


@dataclass(frozen=True)
class CompanyProfile:
    """Our own company, as the analyst needs to understand it."""

    name: str
    one_liner: str = ""
    sector: str = ""
    offer: list[str] = field(default_factory=list)
    positioning: str = ""
    target_customers: list[str] = field(default_factory=list)
    geographies: list[str] = field(default_factory=list)
    price_posture: str = ""
    differentiators: list[str] = field(default_factory=list)
    known_competitors: list[str] = field(default_factory=list)
    excluded_names: list[str] = field(default_factory=list)
    language: str = "fr"

    def prompt_block(self) -> str:
        """Render the profile for an LLM prompt."""
        lines = [f"COMPANY: {self.name}"]
        if self.one_liner:
            lines.append(f"WHAT WE DO: {self.one_liner}")
        if self.sector:
            lines.append(f"SECTOR: {self.sector}")
        if self.offer:
            lines.append("OFFER: " + "; ".join(self.offer))
        if self.positioning:
            lines.append(f"POSITIONING: {self.positioning}")
        if self.target_customers:
            lines.append("TARGET CUSTOMERS: " + "; ".join(self.target_customers))
        if self.geographies:
            lines.append("MARKETS: " + ", ".join(self.geographies))
        if self.price_posture:
            lines.append(f"PRICE POSTURE: {self.price_posture}")
        if self.differentiators:
            lines.append("WE DIFFERENTIATE ON: " + "; ".join(self.differentiators))
        return "\n".join(lines)

    def describe(self) -> str:
        return f"{self.name} — {self.one_liner or self.sector or 'no description'}"


def profile_from_dict(data: dict[str, Any]) -> CompanyProfile:
    """Validate a parsed mapping into a :class:`CompanyProfile`."""
    _require_type(data, dict, "<root>")
    name = str(_require(data, "name", "name")).strip()
    if not name:
        raise ConfigError("company profile 'name' must not be empty")

    def _list(key: str) -> list[str]:
        value = data.get(key) or []
        _require_type(value, list, key)
        return [str(v).strip() for v in value if str(v).strip()]

    return CompanyProfile(
        name=name,
        one_liner=str(data.get("one_liner", "")).strip(),
        sector=str(data.get("sector", "")).strip(),
        offer=_list("offer"),
        positioning=str(data.get("positioning", "")).strip(),
        target_customers=_list("target_customers"),
        geographies=_list("geographies"),
        price_posture=str(data.get("price_posture", "")).strip(),
        differentiators=_list("differentiators"),
        known_competitors=_list("known_competitors"),
        excluded_names=_list("excluded_names"),
        language=str(data.get("language", "fr")).lower(),
    )


def load_company(path: str | Path | None = None) -> CompanyProfile:
    """Load the company profile. Defaults to ``profiles/company.yaml``."""
    p = Path(path) if path else DEFAULT_COMPANY_FILE
    if not p.exists():
        raise ConfigError(
            f"company profile not found: {p}. The competitor report needs to know who "
            f"'we' are — copy profiles/company.yaml and fill it in."
        )
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    profile = profile_from_dict(data)
    logger.info("Loaded company profile — %s", profile.describe())
    return profile
