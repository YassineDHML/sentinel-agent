"""Robust JSON extraction from LLM responses.

LLMs sometimes wrap JSON in ```code fences``` or add stray prose despite
instructions. :func:`extract_json_object` tolerates that by pulling out the
outermost ``{...}`` block before parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first/outermost JSON object found in ``text``.

    Raises:
        ValueError: If ``text`` is empty or contains no parseable JSON object.
    """
    if not text or not text.strip():
        raise ValueError("empty LLM response")
    cleaned = _FENCE.sub("", text).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in LLM response")
    try:
        obj = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in LLM response: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object at the top level")
    return obj
