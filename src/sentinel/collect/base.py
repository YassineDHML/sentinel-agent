"""Shared helpers for the collection layer.

Every collector returns a list of **normalized raw article dicts** with a common
shape (see :func:`make_article`). This module also provides:

* :func:`graceful` — a decorator that turns any collector-level exception into a
  logged warning + empty list, so one dead source never breaks the whole run
  (spec BF-07: graceful degradation).
* thin HTTP helpers with a shared session, a descriptive User-Agent, and timeouts.
* date normalization (:func:`to_iso`) handling feedparser structs, datetimes, and
  ISO strings.

No network happens at import time; tests exercise the pure parse/normalize code
on saved fixtures and mock the HTTP helpers.
"""

from __future__ import annotations

import functools
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from ..logging_conf import get_logger

logger = get_logger("collect")

USER_AGENT = "SentinelBot/0.1 (+https://github.com/welyne/sentinel; non-commercial veille MVP)"
DEFAULT_TIMEOUT = 15  # seconds

# Canonical keys present on every normalized article dict.
ARTICLE_KEYS = ("url", "title", "source", "actor", "published_at", "snippet", "content", "collected_at")

_session: requests.Session | None = None


def get_session() -> requests.Session:
    """Return a lazily-created shared ``requests.Session`` with our User-Agent."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_iso(value: Any) -> str | None:
    """Best-effort normalization of a publish date to an ISO 8601 string.

    Accepts ``None``, ``datetime``, a feedparser ``time.struct_time`` (9-tuple,
    assumed UTC), or a string (returned as-is if not ISO-parseable).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # feedparser's *_parsed fields are time.struct_time (or plain 9-tuples)
    if isinstance(value, time.struct_time) or (isinstance(value, tuple) and len(value) >= 6):
        try:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return s  # keep the raw string; downstream can re-parse if needed
    return None


def normalize_url(url: Any) -> str | None:
    """Trim a URL and return ``None`` if it's empty/unusable."""
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip()
    return cleaned or None


def make_article(
    url: Any,
    source: str,
    *,
    title: str | None = None,
    actor: str | None = None,
    published_at: Any = None,
    snippet: str | None = None,
    content: str | None = None,
) -> dict[str, Any] | None:
    """Build a normalized article dict, or ``None`` if the URL is missing.

    Returns a dict with exactly :data:`ARTICLE_KEYS`. ``collected_at`` is stamped
    with the current UTC time (spec BF-01).
    """
    clean_url = normalize_url(url)
    if clean_url is None:
        return None
    return {
        "url": clean_url,
        "title": (title or "").strip() or None,
        "source": source,
        "actor": actor,
        "published_at": to_iso(published_at),
        "snippet": (snippet or "").strip() or None,
        "content": content,
        "collected_at": _now_iso(),
    }


def graceful(source_name: str) -> Callable:
    """Decorator: log and return ``[]`` if the wrapped collector raises.

    Keeps a single failing source from aborting the pipeline (BF-07).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> list:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - deliberately broad; sources are untrusted
                logger.warning("Collector '%s' failed: %s", source_name, exc)
                return []

        return wrapper

    return decorator


def http_get_json(url: str, params: dict | None = None, *, headers: dict | None = None,
                  timeout: int = DEFAULT_TIMEOUT) -> Any:
    """GET ``url`` and return parsed JSON. Raises on HTTP error."""
    resp = get_session().get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def http_post_json(url: str, json_body: dict, *, headers: dict | None = None,
                   timeout: int = DEFAULT_TIMEOUT) -> Any:
    """POST a JSON body to ``url`` and return parsed JSON. Raises on HTTP error."""
    resp = get_session().post(url, json=json_body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def http_get_text(url: str, *, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """GET ``url`` and return the response body as text. Raises on HTTP error."""
    resp = get_session().get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text
