"""Locale resolution for {B} language × {C} geographic zone.

News sources need language/region parameters that the collectors previously
hardcoded (``googlenews.py`` pinned ``hl=en-US&gl=US&ceid=US:en``). A request
profile supplies a language and a geographic zone; this module turns that pair
into the concrete parameters each source expects.

**The default (``en`` + ``world``) reproduces the historical values byte-for-byte**,
so an unparameterized run is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

# Closed set of geographic zones a request may target ({C}).
GEO_ZONES: tuple[str, ...] = (
    "world",
    "europe",
    "france",
    "north_america",
    "asia",
    "middle_east",
    "africa",
    "latam",
)

# Languages we can generate a report in ({B}). Extend deliberately: each addition
# needs a HEADINGS entry in sentinel.report.i18n too.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "fr")

# Google News `hl` per language. 'en' -> 'en-US' preserves the original URL.
_HL: dict[str, str] = {"en": "en-US", "fr": "fr", "de": "de", "es": "es", "it": "it"}

# Country used when the zone doesn't name one (e.g. "world"): the language's home.
_LANGUAGE_HOME: dict[str, str] = {"en": "US", "fr": "FR", "de": "DE", "es": "ES", "it": "IT"}

# Representative country per zone, chosen per language where the zone spans many.
# Google News has no "continent" scope, so a zone maps to its most representative
# edition; the query text itself carries the rest of the geographic intent.
_ZONE_COUNTRY: dict[str, dict[str, str]] = {
    "world": {},                                    # -> language home
    "europe": {"en": "GB", "fr": "FR", "de": "DE", "es": "ES", "it": "IT"},
    "france": {"*": "FR"},
    "north_america": {"*": "US"},
    "asia": {"*": "SG"},
    "middle_east": {"*": "AE"},
    "africa": {"*": "ZA"},
    "latam": {"es": "MX", "*": "MX"},
}


class LocaleError(ValueError):
    """Raised for an unknown language or geographic zone."""


@dataclass(frozen=True)
class Locale:
    """Resolved source parameters for a (language, zone) pair."""

    language: str          # 'en'
    country: str           # 'US'
    hl: str                # Google News host language, e.g. 'en-US'

    @property
    def ceid(self) -> str:
        """Google News edition id, e.g. ``US:en``."""
        return f"{self.country}:{self.language}"

    def googlenews_params(self) -> dict[str, str]:
        """``hl``/``gl``/``ceid`` for the Google News RSS search URL."""
        return {"hl": self.hl, "gl": self.country, "ceid": self.ceid}

    def gnews_params(self, *, include_country: bool) -> dict[str, str]:
        """GNews API params. ``country`` is only sent when a zone was requested.

        Omitting ``country`` for the default zone keeps the request identical to the
        historical one (which sent ``lang`` only).
        """
        params = {"lang": self.language}
        if include_country:
            params["country"] = self.country.lower()
        return params


def resolve_locale(language: str = "en", geo_zone: str = "world") -> Locale:
    """Resolve a (language, zone) pair to concrete source parameters.

    Raises:
        LocaleError: If the language or zone is unknown.
    """
    lang = (language or "en").strip().lower()
    zone = (geo_zone or "world").strip().lower()

    if zone not in GEO_ZONES:
        raise LocaleError(f"unknown geo_zone {geo_zone!r}; expected one of {list(GEO_ZONES)}")
    if lang not in _HL:
        raise LocaleError(f"unsupported language {language!r} for source locale; known: {sorted(_HL)}")

    mapping = _ZONE_COUNTRY[zone]
    country = mapping.get(lang) or mapping.get("*") or _LANGUAGE_HOME.get(lang, "US")
    return Locale(language=lang, country=country, hl=_HL[lang])


def zone_is_global(geo_zone: str) -> bool:
    """True when the zone imposes no country restriction (``world``)."""
    return (geo_zone or "world").strip().lower() == "world"
