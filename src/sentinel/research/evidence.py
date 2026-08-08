"""Evidence store — every fact the report is allowed to rest on.

Each piece of evidence gets a short **evidence id** (``A1``, ``B7``). Those ids,
never URLs, are what the model is allowed to cite. That single rule is what makes
a fabricated citation *structurally impossible*: the model cannot invent an id
that resolves, and anything unresolvable is dropped before rendering.

Two evidence kinds, matching the citation tiers (see :mod:`.citations`):

``A`` — an article Sentinel collected itself (the existing corpus).
``B`` — a web source the grounded search actually retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

from ..logging_conf import get_logger
from .grounding import GroundedSource

logger = get_logger("research.evidence")

TIER_CORPUS = "A"
TIER_WEB = "B"


@dataclass(frozen=True)
class Evidence:
    """One citable item."""

    eid: str
    tier: str                 # 'A' (collected article) | 'B' (retrieved web source)
    url: str
    title: str = ""
    publisher: str = ""
    snippet: str = ""
    published_at: str | None = None

    def label(self) -> str:
        """Human-readable attribution for the report's source list."""
        return self.publisher or urlparse(self.url).netloc or self.url


def _publisher_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


class EvidenceStore:
    """Collects evidence and hands out stable ids.

    Deduplicates by URL so the same source cited twice keeps one id.
    """

    def __init__(self) -> None:
        self._by_eid: dict[str, Evidence] = {}
        self._eid_by_url: dict[str, str] = {}
        self._counters: dict[str, int] = {TIER_CORPUS: 0, TIER_WEB: 0}

    # -- construction ----------------------------------------------------- #
    def _next_eid(self, tier: str) -> str:
        self._counters[tier] = self._counters.get(tier, 0) + 1
        return f"{tier}{self._counters[tier]}"

    def _add(self, tier: str, url: str, **fields: Any) -> Evidence | None:
        url = (url or "").strip()
        if not url:
            return None
        if url in self._eid_by_url:                      # already known
            return self._by_eid[self._eid_by_url[url]]
        evidence = Evidence(eid=self._next_eid(tier), tier=tier, url=url, **fields)
        self._by_eid[evidence.eid] = evidence
        self._eid_by_url[url] = evidence.eid
        return evidence

    def add_article(self, article: dict) -> Evidence | None:
        """Register a collected article as Tier A evidence."""
        return self._add(
            TIER_CORPUS,
            article.get("url") or "",
            title=(article.get("title") or "").strip(),
            publisher=(article.get("source") or "") or _publisher_from_url(article.get("url") or ""),
            snippet=(article.get("summary") or article.get("snippet") or "").strip()[:400],
            published_at=article.get("published_at"),
        )

    def add_articles(self, articles: Iterable[dict]) -> list[Evidence]:
        return [e for e in (self.add_article(a) for a in articles) if e is not None]

    def add_grounded(self, source: GroundedSource) -> Evidence | None:
        """Register a retrieved web source as Tier B evidence.

        Stores the **durable publisher URL** when resolution succeeded, since the
        grounding redirect shells can expire, and always keeps the publisher name
        so attribution survives even a dead link.
        """
        return self._add(
            TIER_WEB,
            source.best_url,
            title=source.title,
            publisher=source.publisher or _publisher_from_url(source.best_url),
        )

    def add_grounded_sources(self, sources: Iterable[GroundedSource]) -> list[Evidence]:
        return [e for e in (self.add_grounded(s) for s in sources) if e is not None]

    # -- access ------------------------------------------------------------ #
    def get(self, eid: str) -> Evidence | None:
        return self._by_eid.get((eid or "").strip().upper())

    def known(self, eid: str) -> bool:
        return self.get(eid) is not None

    def all(self) -> list[Evidence]:
        return list(self._by_eid.values())

    def by_tier(self, tier: str) -> list[Evidence]:
        return [e for e in self._by_eid.values() if e.tier == tier]

    def publishers(self) -> list[str]:
        seen: list[str] = []
        for e in self._by_eid.values():
            name = e.label()
            if name and name not in seen:
                seen.append(name)
        return seen

    def prompt_block(self, max_items: int | None = None) -> str:
        """Render the evidence for a prompt: ids + what they are, no instructions.

        The writer model sees only these ids and must cite them verbatim.
        """
        lines: list[str] = []
        for e in list(self._by_eid.values())[:max_items]:
            bits = [f"[{e.eid}] {e.label()}"]
            if e.title:
                bits.append(f"— {e.title}")
            if e.snippet:
                bits.append(f": {e.snippet[:220]}")
            lines.append(" ".join(bits))
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._by_eid)

    def summary(self) -> str:
        return (f"{len(self)} evidence item(s): "
                f"{len(self.by_tier(TIER_CORPUS))} corpus (A), "
                f"{len(self.by_tier(TIER_WEB))} web (B)")
