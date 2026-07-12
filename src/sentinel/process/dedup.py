"""Deduplication (spec BF-02).

Two layers:

* **Exact, by URL** — within the current batch (:func:`dedup_by_url`). Exact dedup
  against the DB across runs is handled at persistence time by the ``UNIQUE(url)``
  constraint + idempotent upsert (see the wiring in ``process/__init__.py``).
* **Best-effort, cross-source, by normalized-title similarity**
  (:func:`dedup_by_title`) — the same story from multiple outlets has different
  URLs but near-identical titles. Titles are normalized (lowercased, punctuation
  stripped, whitespace collapsed) and compared with a difflib ratio against a
  configurable threshold; near-duplicates are grouped and one representative kept.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from ..logging_conf import get_logger

logger = get_logger("process.dedup")

DEFAULT_THRESHOLD = 0.85
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_title(title: Any) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for comparison."""
    if not title:
        return ""
    text = _PUNCT.sub(" ", str(title).lower())
    return _WS.sub(" ", text).strip()


def title_similarity(a: Any, b: Any) -> float:
    """Similarity ratio (0..1) between two titles after normalization."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def dedup_by_url(articles: list[dict]) -> list[dict]:
    """Drop exact URL duplicates within the batch, keeping first occurrence."""
    seen: set[str] = set()
    out: list[dict] = []
    for article in articles:
        url = article.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(article)
    if len(out) != len(articles):
        logger.info("URL dedup: %d -> %d article(s).", len(articles), len(out))
    return out


def group_by_title(
    articles: list[dict], threshold: float = DEFAULT_THRESHOLD
) -> list[list[dict]]:
    """Group articles by normalized-title similarity (greedy, first-seen wins).

    Returns a list of groups; each group's first element is its representative.
    Uses difflib's cheap ``real_quick_ratio``/``quick_ratio`` as upper-bound
    prefilters to avoid the full ratio computation on obvious non-matches.
    """
    reps: list[str] = []            # normalized title of each group's representative
    groups: list[list[dict]] = []
    matcher = difflib.SequenceMatcher(autojunk=False)

    for article in articles:
        nt = normalize_title(article.get("title"))
        matched = None
        if nt:
            matcher.set_seq2(nt)
            for i, rep in enumerate(reps):
                if not rep:
                    continue
                if rep == nt:
                    matched = i
                    break
                matcher.set_seq1(rep)
                # cheap upper bounds first; both are >= the true ratio
                if matcher.real_quick_ratio() < threshold:
                    continue
                if matcher.quick_ratio() < threshold:
                    continue
                if matcher.ratio() >= threshold:
                    matched = i
                    break
        if matched is None:
            reps.append(nt)
            groups.append([article])
        else:
            groups[matched].append(article)
    return groups


def dedup_by_title(
    articles: list[dict], threshold: float = DEFAULT_THRESHOLD
) -> tuple[list[dict], list[list[dict]]]:
    """Collapse title near-duplicates.

    Returns ``(deduped, duplicate_groups)`` where ``deduped`` keeps one
    representative per group and ``duplicate_groups`` lists only the groups that
    actually merged more than one article (for logging/insight).
    """
    groups = group_by_title(articles, threshold)
    deduped = [group[0] for group in groups]
    duplicate_groups = [group for group in groups if len(group) > 1]
    if duplicate_groups:
        merged = sum(len(g) for g in duplicate_groups) - len(duplicate_groups)
        logger.info(
            "Title dedup: merged %d near-duplicate(s) across %d group(s); %d -> %d article(s).",
            merged,
            len(duplicate_groups),
            len(articles),
            len(deduped),
        )
    return deduped, duplicate_groups
