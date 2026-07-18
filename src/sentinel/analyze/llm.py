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


class GeminiProvider:
    """Google Gemini (google-genai). Model id comes from config."""

    def __init__(self, model: str, *, api_key: str | None = None, json_mode: bool = True) -> None:
        self.name = "gemini"
        self._model = model
        self._api_key = api_key
        self._json_mode = json_mode
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # lazy

            self._client = genai.Client(api_key=self._api_key or get_secret("GEMINI_API_KEY"))
        return self._client

    def generate(self, prompt: str) -> str:
        from google.genai import types  # lazy

        config = types.GenerateContentConfig(response_mime_type="application/json") if self._json_mode else None
        resp = self._get_client().models.generate_content(
            model=self._model, contents=prompt, config=config
        )
        return resp.text or ""


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
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.batch_size = max(1, batch_size)
        self._max_attempts = max(1, max_attempts)
        self._base_delay = base_delay
        self._max_delay = max_delay

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
