"""Prompts for the deep-research report — the surface to iterate on.

Three prompt families, matching the two-phase architecture:

* :func:`build_acquire_prompt` — grounded, prose. Instructs the model to *search*
  (searching is model-decided; without an explicit instruction it answers from
  memory and returns no sources at all).
* :func:`build_section_prompt` — ungrounded, JSON. Writes one section from the
  evidence, citing evidence ids only.
* :func:`build_synthesis_prompt` — the executive summary, written **last** from the
  finished body so it cannot contradict it.

The model never sees a URL and never writes one: it cites ``[B7]``-style ids that
:mod:`.citations` resolves. That is what makes an invented citation impossible.
"""

from __future__ import annotations

from .contracts import BULLET_KINDS, PROSPECTIVE_KINDS

LANGUAGE_NAMES = {"en": "English", "fr": "French"}


def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get((code or "en").lower(), code or "English")


# --------------------------------------------------------------------------- #
# Phase 1 — ACQUIRE (grounded, prose, no JSON mode)
# --------------------------------------------------------------------------- #
ACQUIRE_INSTRUCTIONS = """\
You are a senior strategic-intelligence analyst.

**Use web search** to gather what has actually been published about the subject below.
Do not answer from memory: search, then report what the sources say.

SUBJECT: {theme}
{scope}

Prioritise, in this order:
1. Primary research from consulting firms (McKinsey, BCG, Bain, Deloitte, PwC) and
   analyst firms (Gartner, Forrester, IDC).
2. Institutional and public data (government bodies, WHO, OECD, EU, national statistics).
3. Credible sector press reporting concrete facts, figures and named companies.

For each finding, state: the source organisation, what it found, and any figures with
their period. Cover: key developments, strong trends, early or weak signals,
technological / economic / regulatory / societal disruptions, the competitive landscape
and named players, and concrete real-world use cases.

Write dense factual prose. No preamble, no recommendations yet."""


def build_acquire_prompt(theme: str, *, sector: str | None = None,
                         geo_zone: str = "world", months_back: int = 12) -> str:
    """Build a grounded research query for one facet of the subject."""
    scope_bits = []
    if sector:
        scope_bits.append(f"SECTOR FOCUS: {sector}")
    if geo_zone and geo_zone != "world":
        scope_bits.append(f"GEOGRAPHIC PRIORITY: {geo_zone.replace('_', ' ')}")
    scope_bits.append(f"PERIOD: the last {months_back} months")
    return ACQUIRE_INSTRUCTIONS.format(theme=theme, scope="\n".join(scope_bits))


ACQUIRE_FACETS: tuple[tuple[str, str], ...] = (
    ("landscape", "key developments, strong trends and weak signals"),
    ("disruptions", "technological, economic, regulatory and societal disruptions, "
                    "plus concrete use cases and real examples"),
    ("competition", "the competitive landscape, named players and their moves, "
                    "market sizing and adoption figures"),
)


def build_facet_prompt(theme: str, facet_hint: str, **scope: object) -> str:
    """A narrower acquire prompt, so several calls cover different ground."""
    base = build_acquire_prompt(theme, **scope)  # type: ignore[arg-type]
    return f"{base}\n\nFOCUS THIS SEARCH ON: {facet_hint}"


# --------------------------------------------------------------------------- #
# Phase 2 — WRITE (ungrounded, JSON mode, evidence ids only)
# --------------------------------------------------------------------------- #
_RULES = """\
CITATION RULES — these are enforced by code, not suggestions:
- Cite ONLY the evidence identifiers listed above (e.g. "A3", "B7"). NEVER write a URL.
- Every block MUST carry at least one identifier in "evidence", or it is discarded.
- An identifier you did not see in the list will not resolve and the block is discarded.
- For a forward-looking block (kind starting with "projection" or "scenario"), set
  "prospective": true, give a "basis" explaining what it extrapolates from, and list the
  supporting identifiers in "basis_evidence". It is published as a labelled hypothesis.
- Never present a forecast as established fact.

WRITING RULES:
- Write in {language}. Tone: direct, concrete, for a CEO and executive committee.
- No filler, no vague generalities, no restating the question.
- "paragraphs" holds plain text only — no markdown, no HTML, no bullet characters.
- Do NOT write the identifiers inside the prose (no "[B1]" in the sentence itself);
  they belong only in the "evidence" field. The reader sees publisher names instead.
- Ground every statement in the evidence provided; add nothing from your own knowledge."""

SECTION_INSTRUCTIONS = """\
You are writing one section of a strategic-intelligence report on:
{theme}

EVIDENCE AVAILABLE (cite by identifier):
{evidence}

{context}
Produce {n_blocks} blocks for this section. Allowed "kind" values, and what each means:
{kinds}

Length: each prose block should be about {words_per_block} words ({paragraphs} paragraph(s)).
Bullet-style blocks ("items") should be one crisp sentence each.

{rules}

Return ONLY this JSON object:
{{"blocks": [
  {{"kind": "...", "heading": "...", "paragraphs": ["..."], "items": [],
    "evidence": ["B1"], "prospective": false, "basis": "", "basis_evidence": [],
    "confidence": "reported", "horizon": ""}}
]}}"""

KIND_GLOSSARY: dict[str, str] = {
    "key_takeaway": "a single decisive fact the executive must know",
    "top_opportunity": "a major business opportunity revealed by the evidence",
    "critical_risk": "a risk that must be watched now",
    "strategic_decision": "a decision or trade-off the executive should consider",
    "evolution": "a key development observed over the period",
    "heavy_trend": "a well-established, durable trend",
    "weak_signal": "an early or marginal signal that could matter later",
    "disruption_tech": "a technological rupture",
    "disruption_economic": "an economic rupture",
    "disruption_regulatory": "a regulatory rupture",
    "disruption_societal": "a societal rupture",
    "competitive_dynamics": "how the competitive landscape is shifting",
    "key_player": "what a specific named player is doing",
    "use_case": "a concrete, real deployment with its outcome",
    "sector_impact": "impact specific to the sector in focus",
    "projection": "where this is heading over the stated horizon",
    "scenario_optimistic": "the favourable scenario",
    "scenario_central": "the most likely scenario",
    "scenario_degraded": "the adverse scenario",
    "opportunity": "an actionable business opportunity",
    "risk_strategic": "a strategic risk",
    "risk_operational": "an operational risk",
    "risk_regulatory": "a regulatory or compliance risk",
    "risk_reputational": "a reputational risk",
    "implication_business_model": "impact on the business model",
    "implication_organisation": "impact on the organisation",
    "implication_skills": "impact on skills and talent",
    "implication_investments": "impact on investment priorities",
    "objective_alignment": "how this aligns with the executive's priority objective",
}


def _kind_lines(kinds: tuple[str, ...]) -> str:
    return "\n".join(f'  - "{k}": {KIND_GLOSSARY.get(k, k)}' for k in kinds)


def build_section_prompt(
    *,
    theme: str,
    kinds: tuple[str, ...],
    n_blocks: int,
    words_per_block: int,
    evidence_block: str,
    language: str = "fr",
    objective: str | None = None,
    sector: str | None = None,
    horizon_years: int = 3,
    written_so_far: list[str] | None = None,
    key_figures: list[str] | None = None,
) -> str:
    """Build the JSON-mode prompt for one report section."""
    context_lines: list[str] = []
    if sector:
        context_lines.append(f"SECTOR FOCUS: {sector}")
    if objective:
        context_lines.append(f"THE EXECUTIVE'S PRIORITY OBJECTIVE: {objective}")
    context_lines.append(f"PROJECTION HORIZON: {horizon_years} years")
    if key_figures:
        # A shared ledger of numbers keeps §1 from contradicting §2.
        context_lines.append("FIGURES YOU MAY USE (use these exact values, invent no others):")
        context_lines.extend(f"  - {f}" for f in key_figures[:14])
    if written_so_far:
        context_lines.append("ALREADY COVERED ELSEWHERE (do not repeat):")
        context_lines.extend(f"  - {h}" for h in written_so_far[:18])

    paragraphs = max(1, round(words_per_block / 105))
    bullets_only = all(k in BULLET_KINDS for k in kinds)
    return SECTION_INSTRUCTIONS.format(
        theme=theme,
        evidence=evidence_block,
        context="\n".join(context_lines) + "\n" if context_lines else "",
        n_blocks=n_blocks,
        kinds=_kind_lines(kinds),
        words_per_block=words_per_block if not bullets_only else 38,
        paragraphs=paragraphs if not bullets_only else 1,
        rules=_RULES.format(language=language_name(language)),
    )


SYNTHESIS_INSTRUCTIONS = """\
You are writing the OPERATIONAL SYNTHESIS that opens a strategic-intelligence report on:
{theme}

It is written LAST, from the finished body below, and must not contradict it. A busy
executive should grasp the essentials in under five minutes.

THE REPORT'S FINDINGS:
{body}

EVIDENCE AVAILABLE (cite by identifier):
{evidence}

Produce {n_blocks} short, sharp bullet blocks:
  - between 5 and 10 "key_takeaway" blocks (the decisive facts)
  - "top_opportunity" blocks (the major business opportunities)
  - "critical_risk" blocks (what must be watched)
  - "strategic_decision" blocks (the decisions or trade-offs to weigh)

Each block: one crisp sentence in "items", no "paragraphs".

{rules}

Return ONLY this JSON object:
{{"blocks": [
  {{"kind": "key_takeaway", "heading": "", "paragraphs": [], "items": ["..."],
    "evidence": ["B2"], "prospective": false, "basis": "", "basis_evidence": [],
    "confidence": "reported", "horizon": ""}}
]}}"""


def build_synthesis_prompt(
    *, theme: str, body_summary: list[str], evidence_block: str,
    n_blocks: int = 16, language: str = "fr",
) -> str:
    """Build the §1 prompt. Called last, fed the finished body."""
    return SYNTHESIS_INSTRUCTIONS.format(
        theme=theme,
        body="\n".join(f"  - {line}" for line in body_summary[:40]) or "  (no body produced)",
        evidence=evidence_block,
        n_blocks=n_blocks,
        rules=_RULES.format(language=language_name(language)),
    )
