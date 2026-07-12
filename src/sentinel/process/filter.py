"""Relevance filtering (spec BF-02).

Keeps only in-scope articles: an article is relevant if it mentions a monitored
actor (name or alias) **or** matches a relevance keyword, and does not match an
exclusion term (exclusions win). Matching is word-boundary aware so short tokens
like "AI" don't match inside "email"/"said", and multi-word phrases like
"artificial intelligence" match as a phrase.

As a side effect, :meth:`RelevanceFilter.filter` attributes a matched actor to the
article (``article["actor"]``) when the collector didn't already set one — useful
for the competitive-watch section later.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..logging_conf import get_logger

logger = get_logger("process.filter")


def _compile_term(term: str) -> re.Pattern:
    """Compile a case-insensitive, word-boundary-aware matcher for ``term``.

    Boundaries treat ``[a-z0-9]`` as "word" characters, so "AI" won't match
    "email" but "GPT-4" still matches (the hyphen is a boundary).
    """
    escaped = re.escape(term.strip().lower())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


class RelevanceFilter:
    """Precompiles keyword/actor/exclude matchers and applies them to articles."""

    def __init__(
        self,
        keywords: Iterable[str],
        actors: Iterable[Any],
        exclude: Iterable[str] | None = None,
    ) -> None:
        self._keywords = [_compile_term(k) for k in keywords if k.strip()]
        self._excludes = [_compile_term(e) for e in (exclude or []) if e.strip()]
        # Each actor -> (canonical name, [compiled name+alias patterns]).
        self._actors: list[tuple[str, list[re.Pattern]]] = []
        for actor in actors:
            terms = [actor.name, *(getattr(actor, "aliases", None) or [])]
            self._actors.append((actor.name, [_compile_term(t) for t in terms if t.strip()]))

    @classmethod
    def from_settings(cls, settings: Any) -> "RelevanceFilter":
        return cls(settings.relevance_keywords, settings.actors, settings.relevance_exclude)

    @staticmethod
    def _text(article: dict) -> str:
        parts = [article.get("title") or "", article.get("snippet") or "", article.get("content") or ""]
        return re.sub(r"\s+", " ", " ".join(parts).lower())

    @staticmethod
    def _any(patterns: list[re.Pattern], text: str) -> bool:
        return any(p.search(text) for p in patterns)

    def match_actor(self, text: str) -> str | None:
        """Return the canonical name of the first actor mentioned, or ``None``."""
        for name, patterns in self._actors:
            if self._any(patterns, text):
                return name
        return None

    def is_excluded(self, text: str) -> bool:
        return self._any(self._excludes, text)

    def is_relevant(self, article: dict) -> bool:
        """True if the article is in-scope (keyword or actor match, not excluded)."""
        text = self._text(article)
        if self.is_excluded(text):
            return False
        return self._any(self._keywords, text) or self.match_actor(text) is not None

    def filter(self, articles: list[dict]) -> list[dict]:
        """Return the in-scope subset, attributing a matched actor where missing."""
        kept: list[dict] = []
        for article in articles:
            text = self._text(article)
            if self.is_excluded(text):
                continue
            actor = self.match_actor(text)
            if actor is not None or self._any(self._keywords, text):
                enriched = dict(article)
                if actor and not enriched.get("actor"):
                    enriched["actor"] = actor
                kept.append(enriched)
        logger.info("Relevance filter: kept %d / %d article(s).", len(kept), len(articles))
        return kept
