"""Classification-response parsing + canonical-tag validation (spec BF-03).

The LLM is instructed to pick tags strictly from the canonical list, but we never
trust that: :func:`parse_classify_response` drops any tag not on the list
(case-insensitively), which is what keeps trend counting reliable — the LLM
cannot introduce variant labels ("multimodal AI" vs "multi-modal agents").
"""

from __future__ import annotations

from typing import Iterable

from ..logging_conf import get_logger
from .parsing import extract_json_object

logger = get_logger("analyze.classify")


def parse_classify_response(
    text: str,
    index_to_url: dict[str, str],
    canonical_tags: Iterable[str],
) -> dict[str, list[str]]:
    """Map a batched classification response to ``{url: [validated tags]}``.

    Tags are matched case-insensitively against ``canonical_tags`` and returned in
    their canonical casing. Invented/rephrased tags are dropped (and logged).
    """
    canon = {tag.lower(): tag for tag in canonical_tags}
    data = extract_json_object(text)
    out: dict[str, list[str]] = {}
    for key, tags in data.items():
        url = index_to_url.get(str(key))
        if not url:
            continue
        valid: list[str] = []
        if isinstance(tags, list):
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                canonical = canon.get(tag.strip().lower())
                if canonical is None:
                    logger.debug("Dropping non-canonical tag %r", tag)
                elif canonical not in valid:
                    valid.append(canonical)
        out[url] = valid
    return out
