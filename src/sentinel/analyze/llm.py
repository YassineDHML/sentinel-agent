"""LLM abstraction: Gemini primary + tiered Groq fallback (spec BF-03).

Design constraints from the spec:
* **Gemini (Flash family) is primary**; model name comes from config, never hardcoded.
* **Groq is a tiered fallback for token-light tasks ONLY** (summaries, classification).
  It is NOT used for historical-context trend analysis (that stays Gemini-only,
  built in a later phase). This module only implements the light tasks, so Groq is
  a valid fallback here.
* **Batching**: several articles per call to respect Gemini's ~10-15 RPM.
* **Rate-limit handling**: exponential backoff per provider; if the primary is
  exhausted, fall back to the secondary; a batch that still fails is skipped
  (logged) rather than aborting the whole run.

Provider SDKs (`google-genai`, `groq`) are imported lazily inside the provider
classes, so this module imports without the SDKs installed and tests inject fakes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import get_secret
from ..logging_conf import get_logger
from .classify import parse_classify_response
from .prompts import build_classify_prompt, build_summary_prompt
from .summarize import parse_summary_response

logger = get_logger("analyze.llm")

# Retry defaults (per provider).
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 60.0


class LLMError(RuntimeError):
    """Raised when all configured providers fail for a single generation."""


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so tests can patch it out."""
    time.sleep(seconds)


def is_rate_limit(exc: BaseException) -> bool:
    """Best-effort, provider-agnostic detection of a rate-limit error.

    Matches by exception class name (Groq's ``RateLimitError``, Gemini's
    ``RESOURCE_EXHAUSTED``) or an HTTP 429 code/status attribute.
    """
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "resourceexhausted" in name:
        return True
    for attr in ("code", "status_code", "status"):
        if getattr(exc, attr, None) == 429:
            return True
    return False


class Provider(Protocol):
    """A text-in/text-out LLM backend."""

    name: str

    def generate(self, prompt: str) -> str: ...


@dataclass
class LLMResponse:
    """A generation result with the metadata long-form work needs.

    ``finish_reason`` lets callers distinguish a *truncated* response (hit the
    output-token ceiling) from a merely-unparseable one, and ``grounding`` carries
    the provider's citation metadata when a search tool was used.
    """

    text: str
    finish_reason: str | None = None
    grounding: Any | None = None
    usage: Any | None = None

    @property
    def truncated(self) -> bool:
        return (self.finish_reason or "").upper().endswith("MAX_TOKENS")


class GeminiProvider:
    """Google Gemini (google-genai). Model id comes from config.

    Generation knobs all default to ``None`` and are omitted from the request when
    unset, so the emitted ``GenerateContentConfig`` is identical to the original
    (``response_mime_type`` only) unless a caller opts in. ``tools`` exists so the
    research layer can pass search grounding without another SDK touch-point.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        json_mode: bool = True,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[Any] | None = None,
        thinking_budget: int | None = None,
        system_instruction: str | None = None,
    ) -> None:
        self.name = "gemini"
        self._model = model
        self._api_key = api_key
        self._json_mode = json_mode
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._tools = tools
        self._thinking_budget = thinking_budget
        self._system_instruction = system_instruction
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # lazy

            self._client = genai.Client(api_key=self._api_key or get_secret("GEMINI_API_KEY"))
        return self._client

    def _build_config(self) -> Any | None:
        """Assemble a ``GenerateContentConfig``, or ``None`` if nothing is set."""
        from google.genai import types  # lazy

        kwargs: dict[str, Any] = {}
        if self._json_mode:
            kwargs["response_mime_type"] = "application/json"
        if self._max_output_tokens is not None:
            kwargs["max_output_tokens"] = self._max_output_tokens
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._tools is not None:
            kwargs["tools"] = self._tools
        if self._system_instruction is not None:
            kwargs["system_instruction"] = self._system_instruction
        if self._thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=self._thinking_budget)
        return types.GenerateContentConfig(**kwargs) if kwargs else None

    def _call(self, prompt: str) -> Any:
        return self._get_client().models.generate_content(
            model=self._model, contents=prompt, config=self._build_config()
        )

    def generate(self, prompt: str) -> str:
        return self._call(prompt).text or ""

    def generate_detailed(self, prompt: str) -> LLMResponse:
        """Generate and return text **plus** finish reason / grounding / usage.

        Used by the long-form research path, which must detect truncation and
        harvest grounding citations. Defensive about response shape so a mocked or
        partial response can't raise.
        """
        resp = self._call(prompt)
        finish_reason = None
        grounding = None
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            first = candidates[0]
            raw_reason = getattr(first, "finish_reason", None)
            if raw_reason is not None:
                finish_reason = getattr(raw_reason, "name", None) or str(raw_reason)
            grounding = getattr(first, "grounding_metadata", None)
        return LLMResponse(
            text=getattr(resp, "text", None) or "",
            finish_reason=finish_reason,
            grounding=grounding,
            usage=getattr(resp, "usage_metadata", None),
        )


class GroqProvider:
    """Groq (OpenAI-compatible). Fallback for token-light tasks only."""

    def __init__(self, model: str, *, api_key: str | None = None, json_mode: bool = True,
                 temperature: float = 0.2) -> None:
        self.name = "groq"
        self._model = model
        self._api_key = api_key
        self._json_mode = json_mode
        self._temperature = temperature
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from groq import Groq  # lazy

            self._client = Groq(api_key=self._api_key or get_secret("GROQ_API_KEY"))
        return self._client

    def generate(self, prompt: str) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._temperature,
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._get_client().chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


class LLMClient:
    """Batches light tasks over a primary provider with a fallback + backoff."""

    def __init__(
        self,
        primary: Provider,
        fallback: Provider | None = None,
        *,
        batch_size: int = 8,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        min_interval_seconds: float = 0.0,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.batch_size = max(1, batch_size)
        self._max_attempts = max(1, max_attempts)
        self._base_delay = base_delay
        self._max_delay = max_delay
        # Floor on the gap between successive calls. Defaults to 0 (no pacing) so
        # existing behaviour is unchanged; the research path sets it to protect the
        # free-tier RPM budget when a single report makes many calls.
        self._min_interval = max(0.0, min_interval_seconds)
        self._last_call_at: float | None = None

    def _pace(self) -> None:
        """Sleep just long enough to respect ``min_interval_seconds``."""
        if self._min_interval <= 0:
            return
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < self._min_interval:
                _sleep(self._min_interval - elapsed)
        self._last_call_at = time.monotonic()

    @classmethod
    def from_settings(cls, settings: Any) -> "LLMClient":
        """Build from config: Gemini primary, Groq fallback if a key is present."""
        primary = GeminiProvider(settings.llm.gemini_model)
        fallback: Provider | None = None
        if get_secret("GROQ_API_KEY", required=False):
            fallback = GroqProvider(settings.llm.groq_model)
        return cls(primary, fallback, batch_size=settings.llm.batch_size)

    # -- generation ------------------------------------------------------- #
    def _generate_once(self, provider: Provider, prompt: str) -> str:
        """Call one provider with exponential backoff on transient errors."""
        delay = self._base_delay
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._pace()
                return provider.generate(prompt)
            except Exception as exc:  # noqa: BLE001 - provider/SDK errors are varied
                if attempt >= self._max_attempts:
                    raise
                rate_limited = is_rate_limit(exc)
                wait = min(self._max_delay, delay * (2 if rate_limited else 1))
                logger.warning(
                    "%s attempt %d/%d failed%s (%s); retrying in %.1fs",
                    provider.name, attempt, self._max_attempts,
                    " [rate-limited]" if rate_limited else "", exc, wait,
                )
                _sleep(wait)
                delay *= 2
        raise RuntimeError("unreachable")  # loop returns or raises

    def generate(self, prompt: str) -> str:
        """Generate via primary; on failure, fall back to the secondary provider."""
        try:
            return self._generate_once(self.primary, prompt)
        except Exception as primary_exc:  # noqa: BLE001
            if self.fallback is None:
                raise LLMError(f"{self.primary.name} failed and no fallback: {primary_exc}") from primary_exc
            logger.warning(
                "Primary %s failed (%s); falling back to %s.",
                self.primary.name, primary_exc, self.fallback.name,
            )
            try:
                return self._generate_once(self.fallback, prompt)
            except Exception as fallback_exc:  # noqa: BLE001
                raise LLMError(
                    f"both providers failed (primary={self.primary.name}, "
                    f"fallback={self.fallback.name}): {fallback_exc}"
                ) from fallback_exc

    # -- batching --------------------------------------------------------- #
    def _batches(self, articles: list[dict]) -> list[list[dict]]:
        return [articles[i : i + self.batch_size] for i in range(0, len(articles), self.batch_size)]

    PARSE_RETRIES = 1  # re-ask a batch once if its response is unparseable

    def _generate_parsed(self, prompt: str, parse: Any) -> Any:
        """Generate + parse, re-asking once on an unparseable response."""
        last_exc: ValueError | None = None
        for attempt in range(self.PARSE_RETRIES + 1):
            text = self.generate(prompt)
            try:
                return parse(text)
            except ValueError as exc:
                last_exc = exc
                if attempt < self.PARSE_RETRIES:
                    logger.warning("Unparseable LLM response (%s); re-asking once.", exc)
        assert last_exc is not None
        raise last_exc

    def summarize_batch(self, articles: list[dict]) -> dict[str, str]:
        """Return ``{url: summary}`` for the given articles (batched calls)."""
        results: dict[str, str] = {}
        batches = self._batches(articles)
        for n, batch in enumerate(batches, 1):
            items = list(enumerate(batch, 1))
            index_to_url = {str(i): a["url"] for i, a in items}
            prompt = build_summary_prompt(items)
            try:
                results.update(self._generate_parsed(
                    prompt, lambda text: parse_summary_response(text, index_to_url)))
            except (LLMError, ValueError) as exc:
                logger.error("Summary batch %d/%d failed; skipping: %s", n, len(batches), exc)
        logger.info("Summarized %d/%d article(s).", len(results), len(articles))
        return results

    def classify_batch(self, articles: list[dict], canonical_tags: list[str]) -> dict[str, list[str]]:
        """Return ``{url: [canonical tags]}`` for the given articles (batched calls)."""
        results: dict[str, list[str]] = {}
        batches = self._batches(articles)
        for n, batch in enumerate(batches, 1):
            items = list(enumerate(batch, 1))
            index_to_url = {str(i): a["url"] for i, a in items}
            prompt = build_classify_prompt(items, canonical_tags)
            try:
                results.update(self._generate_parsed(
                    prompt,
                    lambda text: parse_classify_response(text, index_to_url, canonical_tags)))
            except (LLMError, ValueError) as exc:
                logger.error("Classify batch %d/%d failed; skipping: %s", n, len(batches), exc)
        logger.info("Classified %d/%d article(s).", len(results), len(articles))
        return results
