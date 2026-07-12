"""Summary-response parsing (spec BF-03).

The prompt construction lives in ``prompts.py``; this module turns a batched
LLM response back into a ``{url: summary}`` mapping. Prompt-per-batch calling is
orchestrated by :class:`~sentinel.analyze.llm.LLMClient`.
"""

from __future__ import annotations

from ..logging_conf import get_logger
from .parsing import extract_json_object

logger = get_logger("analyze.summarize")


def parse_summary_response(text: str, index_to_url: dict[str, str]) -> dict[str, str]:
    """Map a batched summary response (``{"1": "...", ...}``) to ``{url: summary}``.

    Unknown indices and blank/non-string summaries are ignored.
    """
    data = extract_json_object(text)
    out: dict[str, str] = {}
    for key, value in data.items():
        url = index_to_url.get(str(key))
        if url and isinstance(value, str) and value.strip():
            out[url] = value.strip()
    return out
