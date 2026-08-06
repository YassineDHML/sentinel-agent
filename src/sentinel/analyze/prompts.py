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

# Defaults reproduce the historical single-theme prompts exactly, so an
# unparameterized run sends byte-identical text.
DEFAULT_THEME = "the AI SaaS B2B sector"
DEFAULT_LANGUAGE = "English"

# Language code -> name used inside prompts.
LANGUAGE_NAMES: dict[str, str] = {"en": "English", "fr": "French"}


def language_name(code: str | None) -> str:
    """Prompt-friendly language name for a code, defaulting to English."""
    if not code:
        return DEFAULT_LANGUAGE
    return LANGUAGE_NAMES.get(str(code).lower(), str(code))

# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #
SUMMARY_INSTRUCTIONS = """\
You are a strategic-intelligence analyst covering {theme}.
For each numbered article below, write a factual summary of 3 to 5 short lines in {language}.

Rules:
- Ground every statement ONLY in the provided TITLE and TEXT. Do not add outside
  knowledge, speculation, or facts not present in the text.
- If the text is thin, summarize only what is present; never invent details.
- Plain text only: no preamble, no markdown, no bullet characters.

Return ONLY a JSON object mapping each article number (as a string) to its summary
string. Example: {{"1": "First summary...", "2": "Second summary..."}}"""

# --------------------------------------------------------------------------- #
# Classify
# --------------------------------------------------------------------------- #
CLASSIFY_INSTRUCTIONS_HEADER = """\
You are classifying news about {theme} into a FIXED taxonomy.
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


def build_summary_prompt(
    items: list[tuple[int, dict]],
    *,
    theme: str = DEFAULT_THEME,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Build the batched summarization prompt for ``(index, article)`` pairs.

    ``theme`` and ``language`` default to the historical values, so calling this
    with only ``items`` produces exactly the original prompt.
    """
    instructions = SUMMARY_INSTRUCTIONS.format(theme=theme, language=language)
    return f"{instructions}\n\nARTICLES:\n{_article_block(items)}"


def build_classify_prompt(
    items: list[tuple[int, dict]],
    canonical_tags: list[str],
    *,
    theme: str = DEFAULT_THEME,
) -> str:
    """Build the batched classification prompt, embedding the canonical tag list."""
    tag_list = "\n".join(f"- {tag}" for tag in canonical_tags)
    header = CLASSIFY_INSTRUCTIONS_HEADER.format(theme=theme)
    return (
        f"{header}\n{tag_list}\n\n"
        f"{CLASSIFY_INSTRUCTIONS_FOOTER}\n\nARTICLES:\n{_article_block(items)}"
    )
