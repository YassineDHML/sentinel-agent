"""Google-Search grounding — the only real web-search capability available to us.

The deep-research report must cite named research houses (McKinsey, BCG, Gartner,
IDC…). Our collected news corpus cannot supply those, so the model is given
Google Search as a *tool* and we harvest the sources it actually consulted.

This is the single place that touches the SDK's tool API. Everything downstream
consumes :class:`GroundedResult`.

MEASURED BEHAVIOUR (verified live against ``gemini-flash-latest`` →
``gemini-3.6-flash``, Aug 2026). These facts shape the code below:

* **Grounding works on the free tier.** A single call returned 14 sources
  including ``mckinsey.com``, ``bcg.com``, ``idc.com``, ``nih.gov``, ``nber.org``.
* **Publisher identity arrives in ``web.title``, NOT ``web.domain``** — ``domain``
  was ``None`` on every chunk observed. :func:`_publisher_of` reads both.
* **Grounding and JSON mode are mutually exclusive.** Requesting
  ``response_mime_type="application/json"`` alongside the search tool returns a
  response with no candidates (or a 400). This is why research is two-phase:
  ACQUIRE grounded prose → then WRITE structured JSON *without* tools.
* **``thinking_budget=0`` is rejected** by this model; a small positive budget is
  accepted and stops extended thinking from consuming the whole output allowance
  (one call spent 2,298 of 3,290 tokens on thinking and truncated).
* **``time_range_filter`` works only at second granularity** — microseconds raise
  ``Granularity of nano is not supported``. :func:`_interval` strips them.
* **Citation URIs are ``vertexaisearch.cloud.google.com`` redirect shells** that
  resolve to the real publisher URL, so we resolve them for durable links.
* **Searching is model-decided.** It will answer from memory if it thinks it can,
  returning no grounding metadata at all — callers must treat that as
  "no evidence", never as "trust the text".
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from ..logging_conf import get_logger

logger = get_logger("research.grounding")

# Host of the grounding redirect shells.
GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"

# Small positive budget: 0 is rejected, and leaving it unset lets extended
# thinking eat the output allowance.
DEFAULT_THINKING_BUDGET = 256
DEFAULT_MAX_OUTPUT_TOKENS = 4000


@dataclass(frozen=True)
class GroundedSource:
    """One web source the model actually consulted."""

    uri: str                      # the grounding redirect shell (may expire)
    publisher: str = ""           # e.g. "mckinsey.com" — durable attribution
    title: str = ""
    resolved_url: str = ""        # real publisher URL, when resolution succeeded

    @property
    def best_url(self) -> str:
        """Prefer the durable publisher URL over the expiring redirect."""
        return self.resolved_url or self.uri


@dataclass
class GroundedResult:
    """Outcome of a grounded generation."""

    text: str
    sources: list[GroundedSource] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)     # searches the model ran
    finish_reason: str | None = None
    total_tokens: int | None = None
    thought_tokens: int | None = None

    @property
    def grounded(self) -> bool:
        """True only if the model really searched and returned sources."""
        return bool(self.sources)

    @property
    def truncated(self) -> bool:
        return (self.finish_reason or "").upper().endswith("MAX_TOKENS")

    def publishers(self) -> list[str]:
        seen: list[str] = []
        for s in self.sources:
            if s.publisher and s.publisher not in seen:
                seen.append(s.publisher)
        return seen


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _publisher_of(web: Any) -> str:
    """Extract publisher identity from a grounding chunk.

    The API populates ``domain`` in some versions and ``title`` in others (every
    chunk we observed had ``domain=None`` and the domain string in ``title``), so
    read both rather than trusting either.
    """
    domain = (getattr(web, "domain", None) or "").strip()
    if domain:
        return domain
    return (getattr(web, "title", None) or "").strip()


def _interval(types_mod: Any, start: _dt.datetime, end: _dt.datetime) -> Any:
    """Build a time-range Interval with microseconds stripped.

    Sub-second precision is rejected: ``Granularity of nano is not supported``.
    """
    return types_mod.Interval(
        start_time=start.replace(microsecond=0), end_time=end.replace(microsecond=0)
    )


def _extract(response: Any, *, resolve_urls: bool) -> GroundedResult:
    """Turn a raw SDK response into a :class:`GroundedResult`. Never raises."""
    text = getattr(response, "text", None) or ""
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(response, "candidates", None) or []

    finish_reason = None
    meta = None
    if candidates:
        raw = getattr(candidates[0], "finish_reason", None)
        if raw is not None:
            finish_reason = getattr(raw, "name", None) or str(raw)
        meta = getattr(candidates[0], "grounding_metadata", None)

    sources: list[GroundedSource] = []
    queries: list[str] = []
    if meta is not None:
        queries = list(getattr(meta, "web_search_queries", None) or [])
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web is None:
                continue          # image/maps/retrieved_context chunks
            uri = (getattr(web, "uri", None) or "").strip()
            if not uri:
                continue
            sources.append(GroundedSource(
                uri=uri,
                publisher=_publisher_of(web),
                title=(getattr(web, "title", None) or "").strip(),
            ))

    if resolve_urls and sources:
        sources = _resolve_all(sources)

    return GroundedResult(
        text=text,
        sources=sources,
        queries=queries,
        finish_reason=finish_reason,
        total_tokens=getattr(usage, "total_token_count", None),
        thought_tokens=getattr(usage, "thoughts_token_count", None),
    )


def _resolve_all(sources: list[GroundedSource]) -> list[GroundedSource]:
    """Resolve redirect shells to durable publisher URLs (best-effort)."""
    from ..collect.resolve import follow_redirect

    out: list[GroundedSource] = []
    for s in sources:
        resolved = ""
        try:
            resolved = follow_redirect(s.uri) or ""
        except Exception as exc:  # noqa: BLE001 - resolution is a nicety, never fatal
            logger.debug("Could not resolve grounding URI: %s", exc)
        out.append(GroundedSource(uri=s.uri, publisher=s.publisher,
                                  title=s.title, resolved_url=resolved))
    resolved_count = sum(1 for s in out if s.resolved_url)
    logger.info("Grounding: resolved %d/%d source URL(s) to publisher links.",
                resolved_count, len(out))
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def grounded_generate(
    prompt: str,
    model: str,
    *,
    api_key: str | None = None,
    since: _dt.datetime | None = None,
    until: _dt.datetime | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking_budget: int | None = DEFAULT_THINKING_BUDGET,
    resolve_urls: bool = True,
    client: Any | None = None,
) -> GroundedResult:
    """Generate with Google Search grounding and harvest the sources consulted.

    Args:
        prompt: The research question. **Instruct the model to search explicitly** —
            searching is model-decided and it will otherwise answer from memory.
        model: Model id, from config (never hardcoded).
        since / until: Optional publication window ({D}); microseconds are stripped.
        max_output_tokens: Output allowance. Extended thinking is charged against
            it, so leave generous headroom.
        thinking_budget: Small positive cap; ``0`` is rejected by the model, and
            ``None`` omits the setting entirely.
        resolve_urls: Resolve redirect shells to durable publisher URLs.
        client: Injected SDK client (tests pass a fake).

    Returns:
        A :class:`GroundedResult`. If the model chose not to search, ``sources``
        is empty and ``grounded`` is ``False`` — callers must not treat the text
        as evidence-backed in that case.

    Raises:
        Propagates SDK errors (quota, auth, invalid argument) to the caller, which
        decides whether to degrade.
    """
    from google.genai import types  # lazy: keeps this importable without the SDK

    if client is None:
        from google import genai

        from ..config import get_secret

        client = genai.Client(api_key=api_key or get_secret("GEMINI_API_KEY"))

    search_kwargs: dict[str, Any] = {}
    if since is not None and until is not None:
        search_kwargs["time_range_filter"] = _interval(types, since, until)

    config_kwargs: dict[str, Any] = {
        "tools": [types.Tool(google_search=types.GoogleSearch(**search_kwargs))],
        "max_output_tokens": max_output_tokens,
    }
    # NOTE: response_mime_type MUST NOT be set here — JSON mode and the search
    # tool cannot coexist. Structured output happens in a separate, ungrounded call.
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

    response = client.models.generate_content(
        model=model, contents=prompt, config=types.GenerateContentConfig(**config_kwargs)
    )
    result = _extract(response, resolve_urls=resolve_urls)

    if not result.grounded:
        logger.warning(
            "Grounded call returned NO sources — the model answered without searching. "
            "Its text must not be treated as evidence-backed."
        )
    else:
        logger.info("Grounding: %d source(s) from %s | queries=%s",
                    len(result.sources), ", ".join(result.publishers()[:5]) or "?",
                    result.queries[:3])
    if result.truncated:
        logger.warning("Grounded response hit the output ceiling (thinking used %s tokens).",
                       result.thought_tokens)
    return result
