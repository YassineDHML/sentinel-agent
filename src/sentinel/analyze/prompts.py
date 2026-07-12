"""LLM prompt templates for the token-light tasks (summarize, classify).

Kept as named module-level constants + small builders so prompts can be iterated
on in one place (spec S3 calls out prompt iteration as the delicate part). Both
tasks are **batched**: one call handles several articles, keyed by number.
"""

from __future__ import annotations

from typing import Any

# Per-article body is capped so a batch stays well within token limits and the
# ~10-15 RPM budget isn't blown by oversized single calls.
MAX_ARTICLE_CHARS = 1500

# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #
SUMMARY_INSTRUCTIONS = """\
You are a strategic-intelligence analyst covering the AI SaaS B2B sector.
For each numbered article below, write a factual summary of 3 to 5 short lines in English.

Rules:
- Ground every statement ONLY in the provided TITLE and TEXT. Do not add outside
  knowledge, speculation, or facts not present in the text.
- If the text is thin, summarize only what is present; never invent details.
- Plain text only: no preamble, no markdown, no bullet characters.

Return ONLY a JSON object mapping each article number (as a string) to its summary
string. Example: {"1": "First summary...", "2": "Second summary..."}"""

# --------------------------------------------------------------------------- #
# Classify
# --------------------------------------------------------------------------- #
CLASSIFY_INSTRUCTIONS_HEADER = """\
You are classifying AI-sector news into a FIXED taxonomy.
Assign each numbered article zero or more topics chosen STRICTLY from this canonical list:"""

CLASSIFY_INSTRUCTIONS_FOOTER = """\
Rules:
- Use ONLY tags from the list above, copied verbatim. Do NOT invent, rephrase, or
  translate tags.
- Assign a tag only when it clearly applies. If none apply, use an empty list.

Return ONLY a JSON object mapping each article number (as a string) to a JSON array
of tags. Example: {"1": ["product launch"], "2": []}"""


def _article_block(items: list[tuple[int, dict]]) -> str:
    """Render numbered articles for a prompt (title + source/actor + capped text)."""
    lines: list[str] = []
    for idx, article in items:
        body = (article.get("content") or article.get("snippet") or "").strip()
        if len(body) > MAX_ARTICLE_CHARS:
            body = body[:MAX_ARTICLE_CHARS] + "..."
        lines.append(f"[{idx}] TITLE: {article.get('title') or '(no title)'}")
        if article.get("source"):
            lines.append(f"    SOURCE: {article['source']}")
        if article.get("actor"):
            lines.append(f"    ACTOR: {article['actor']}")
        lines.append(f"    TEXT: {body or '(no body text available; rely on the title only)'}")
    return "\n".join(lines)


def build_summary_prompt(items: list[tuple[int, dict]]) -> str:
    """Build the batched summarization prompt for ``(index, article)`` pairs."""
    return f"{SUMMARY_INSTRUCTIONS}\n\nARTICLES:\n{_article_block(items)}"


def build_classify_prompt(items: list[tuple[int, dict]], canonical_tags: list[str]) -> str:
    """Build the batched classification prompt, embedding the canonical tag list."""
    tag_list = "\n".join(f"- {tag}" for tag in canonical_tags)
    return (
        f"{CLASSIFY_INSTRUCTIONS_HEADER}\n{tag_list}\n\n"
        f"{CLASSIFY_INSTRUCTIONS_FOOTER}\n\nARTICLES:\n{_article_block(items)}"
    )
