"""Report requests: the {A}..{F} parameters supplied per report.

A :class:`~sentinel.request.profile.RequestProfile` is loaded from
``requests/<slug>.yaml`` and overlaid onto the global settings with
:func:`~sentinel.request.overlay.apply_profile`, so every existing pipeline stage
runs against per-request settings without any signature change.
"""

from .locale import GEO_ZONES, SUPPORTED_LANGUAGES, Locale, LocaleError, resolve_locale
from .onboarding import Onboarding, load_onboarding
from .overlay import apply_profile, assert_trend_safe, profile_of, theme_of
from .profile import (
    CADENCES,
    OBJECTIVES,
    REPORT_TYPES,
    RequestProfile,
    list_profiles,
    load_profile,
    load_profiles,
    profile_from_dict,
)

__all__ = [
    "RequestProfile",
    "load_profile",
    "load_profiles",
    "profile_from_dict",
    "list_profiles",
    "Onboarding",
    "load_onboarding",
    "apply_profile",
    "profile_of",
    "theme_of",
    "assert_trend_safe",
    "resolve_locale",
    "Locale",
    "LocaleError",
    "GEO_ZONES",
    "SUPPORTED_LANGUAGES",
    "REPORT_TYPES",
    "OBJECTIVES",
    "CADENCES",
]
