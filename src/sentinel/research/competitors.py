"""Competitor comparison report (capability 2).

Produces, from our own company profile: 5–10 competitors ranked by threat level,
a dossier per competitor (offer, positioning, target, strengths, weaknesses, risks
to us, angles of attack), and a strategic synthesis naming the three most dangerous
with a recommended action plan.

Structure mirrors the deep-research report and reuses its machinery — grounded
acquisition, the evidence store, and the three-tier citation policy — with two
differences that suit this report:

* **One grounded call per competitor.** Keeps each JSON response small (so
  truncation is unlikely and cheap), and makes a failed competitor a graceful skip
  rather than a lost report.
* **Numbers are not hedged, they are omitted.** Market share or traction without a
  citation is simply not rendered. The spec invites "argued hypotheses", so those
  are allowed — but only as Tier C: anchored to real evidence and visibly labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..logging_conf import get_logger
from .citations import Claim, CitationRegistry, ValidationReport
from .company import CompanyProfile
from .evidence import EvidenceStore
from .grounding import GroundedResult, grounded_generate
from .parse import salvage_json_blocks, strip_inline_citations
from .prompts import language_name

logger = get_logger("research.competitors")

# Closed vocabularies — the model picks from these, never invents a value.
THREAT_LEVELS: tuple[str, ...] = ("high", "medium", "low")
POSITIONINGS: tuple[str, ...] = (
    "premium", "low-cost", "niche", "tech", "generalist", "enterprise", "self-serve",
)
DEFAULT_THREAT = "medium"

MAX_COMPETITORS = 10
MIN_COMPETITORS = 5
DOSSIER_MAX_TOKENS = 6000
DISCOVERY_MAX_TOKENS = 3000
SYNTHESIS_MAX_TOKENS = 4000
WRITER_THINKING_BUDGET = 512

_THREAT_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
@dataclass
class CompetitorDossier:
    """One competitor, fully analysed and citation-validated."""

    name: str
    threat_level: str = DEFAULT_THREAT
    positioning: str = ""
    description: str = ""
    main_offer: str = ""
    marketing_positioning: str = ""
    target_customer: str = ""
    strengths: list[Claim] = field(default_factory=list)
    weaknesses: list[Claim] = field(default_factory=list)
    risks: list[Claim] = field(default_factory=list)
    opportunities: list[Claim] = field(default_factory=list)
    traction: Claim | None = None          # omitted entirely when unsourced
    sources: list[dict[str, str]] = field(default_factory=list)

    @property
    def threat_rank(self) -> int:
        return _THREAT_ORDER.get(self.threat_level, 1)

    @property
    def evidence_count(self) -> int:
        return len(self.sources)


@dataclass
class CompetitorReport:
    """The assembled comparison report."""

    company: str
    language: str = "fr"
    period_label: str = ""
    dossiers: list[CompetitorDossier] = field(default_factory=list)
    top_threats: list[Claim] = field(default_factory=list)
    action_plan: list[Claim] = field(default_factory=list)
    integrity: Any = None

    def by_threat(self, level: str) -> list[CompetitorDossier]:
        return [d for d in self.dossiers if d.threat_level == level]


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
DISCOVERY_PROMPT = """\
Using web search, identify the companies that compete with the business described below.

{company}

Find between {min_n} and {max_n} of the most relevant competitors — direct (same offer,
same buyers) and indirect (a different route to the same customer need). Prefer
companies that are genuinely active in this market today.

For each, give the company name and one line on what it sells. Search for real
companies; do not invent names."""

DOSSIER_PROMPT = """\
Using web search, research the competitor "{name}" as a rival to the company below.

{company}

Find and report, citing what you find:
- what the company is, its main offer, marketing positioning, and target customer
- its strengths: competitive advantages, differentiation, perceived quality
- any published figures on market share, traction, funding or customer numbers
- its weaknesses: product limits, commercial or positioning problems, recurring
  negative customer feedback, structural gaps

Report only what the sources support. If something is not documented, say so rather
than guessing."""

STRUCTURE_PROMPT = """\
Turn the research below into a structured competitor dossier for "{name}".

OUR COMPANY (the report is written from our point of view):
{company}

RESEARCH FINDINGS:
{findings}

EVIDENCE AVAILABLE (cite by identifier):
{evidence}

Write in {language}. Be concrete and decision-oriented; no academic padding.

RULES — enforced by code:
- Cite ONLY the identifiers listed above (e.g. "B3"). NEVER write a URL, and never
  put an identifier inside the prose itself.
- Every item needs at least one identifier in its "evidence", or it is discarded.
- "threat_level" must be exactly one of: {threats}
- "positioning" must be exactly one of: {positionings}
- Report "traction" ONLY if a source gives a figure. If none does, omit the field —
  do not estimate, do not hedge.
- An argued hypothesis is allowed only with "prospective": true and supporting
  identifiers in "basis_evidence"; it will be published labelled as a hypothesis.

Return ONLY this JSON object:
{{"blocks": [
  {{"kind": "profile", "threat_level": "high", "positioning": "tech",
    "description": "...", "main_offer": "...", "marketing_positioning": "...",
    "target_customer": "...", "evidence": ["B1"]}},
  {{"kind": "strength", "text": "...", "evidence": ["B2"]}},
  {{"kind": "weakness", "text": "...", "evidence": ["B3"]}},
  {{"kind": "risk", "text": "...", "horizon": "short|mid|long", "evidence": ["B1"]}},
  {{"kind": "opportunity", "text": "...", "evidence": ["B2"]}},
  {{"kind": "traction", "text": "...", "evidence": ["B4"]}}
]}}"""

SYNTHESIS_PROMPT = """\
Write the strategic synthesis closing a competitor report for the company below.

{company}

COMPETITORS ANALYSED (with their assessed threat level):
{competitors}

EVIDENCE AVAILABLE (cite by identifier):
{evidence}

Write in {language}. Produce:
- exactly 3 "top_threat" blocks: the three most dangerous competitors and WHY each
  is dangerous to us specifically;
- 4 to 6 "action" blocks: concrete recommended moves to counter them.

RULES — enforced by code: cite only the identifiers above, never a URL, never an
identifier inside the prose. Every block needs at least one identifier.

Return ONLY this JSON object:
{{"blocks": [
  {{"kind": "top_threat", "text": "...", "evidence": ["B1"]}},
  {{"kind": "action", "text": "...", "evidence": ["B2"]}}
]}}"""


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
Writer = Callable[[str], str]


def _default_writer(model: str) -> Writer:
    from ..analyze.llm import GeminiProvider, LLMClient

    provider = GeminiProvider(model, json_mode=True, max_output_tokens=DOSSIER_MAX_TOKENS,
                              thinking_budget=WRITER_THINKING_BUDGET)
    return LLMClient(provider, fallback=None, min_interval_seconds=4.0).generate


def discover_competitors(
    company: CompanyProfile,
    model: str,
    *,
    grounded_fn: Callable[..., GroundedResult] = grounded_generate,
    store: EvidenceStore | None = None,
    max_n: int = MAX_COMPETITORS,
) -> tuple[list[str], EvidenceStore]:
    """Find competitor names: the declared ones plus whatever search surfaces.

    Declared competitors always survive, so a rival the team already tracks can
    never be dropped by a search that happened to miss it.
    """
    store = store or EvidenceStore()
    names: list[str] = list(company.known_competitors)

    prompt = DISCOVERY_PROMPT.format(
        company=company.prompt_block(), min_n=MIN_COMPETITORS, max_n=max_n)
    try:
        result = grounded_fn(prompt, model, max_output_tokens=DISCOVERY_MAX_TOKENS)
        store.add_grounded_sources(result.sources)
        names += _names_from_text(result.text)
    except Exception as exc:  # noqa: BLE001 - declared competitors still carry the report
        logger.warning("Competitor discovery failed (%s); using declared list only.", exc)

    excluded = {n.lower() for n in [*company.excluded_names, company.name]}
    unique: list[str] = []
    for n in names:
        clean = n.strip(" .:-–—*#").strip()
        if clean and clean.lower() not in excluded and clean.lower() not in {u.lower() for u in unique}:
            unique.append(clean)
    logger.info("Competitors to analyse (%d): %s", len(unique[:max_n]), ", ".join(unique[:max_n]))
    return unique[:max_n], store


def _names_from_text(text: str) -> list[str]:
    """Pull candidate company names out of the discovery prose."""
    import re

    names: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # "1. Name — what they do" / "- **Name**: ..." / "* Name - ..."
        m = re.match(r"^(?:[-*\u2022]|\d+[.)])\s*\**\s*([^\n:\u2014\u2013*]{2,60})\**\s*[:\u2014\u2013-]",
                     line)
        if m:
            candidate = m.group(1).strip()
            if 1 < len(candidate) <= 60 and not candidate.lower().startswith(("http", "the ")):
                names.append(candidate)
    return names


def _claims(payload: list[dict], kind: str, registry: CitationRegistry,
            report: ValidationReport, section: str) -> list[Claim]:
    """Validate every block of one kind into citation-checked claims."""
    out: list[Claim] = []
    for raw in payload:
        if str(raw.get("kind", "")) != kind:
            continue
        text = strip_inline_citations(str(raw.get("text", "")))
        if not text:
            continue
        claim = registry.validate_claim(
            text, raw.get("evidence") or [],
            prospective=bool(raw.get("prospective")),
            confidence=str(raw.get("confidence", "reported")),
            basis=str(raw.get("basis", "")),
            basis_eids=raw.get("basis_evidence"),
            section=section, report=report,
        )
        if claim is not None:
            out.append(claim)
    return out


def build_dossier(
    name: str,
    company: CompanyProfile,
    model: str,
    *,
    writer: Writer,
    registry: CitationRegistry,
    store: EvidenceStore,
    report: ValidationReport,
    grounded_fn: Callable[..., GroundedResult] = grounded_generate,
) -> CompetitorDossier | None:
    """Research and structure one competitor. Returns ``None`` if it cannot be sourced."""
    # 1. grounded research on this competitor
    try:
        found = grounded_fn(DOSSIER_PROMPT.format(name=name, company=company.prompt_block()),
                            model, max_output_tokens=DOSSIER_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Research on %r failed: %s", name, exc)
        return None
    if not found.grounded:
        logger.warning("No sources found for %r; skipping rather than guessing.", name)
        return None

    before = {e.eid for e in store.all()}
    store.add_grounded_sources(found.sources)
    new_evidence = [e for e in store.all() if e.eid not in before]

    # 2. structure it, citing only what was found
    prompt = STRUCTURE_PROMPT.format(
        name=name, company=company.prompt_block(), findings=found.text[:6000],
        evidence=store.prompt_block(), language=language_name(company.language),
        threats=", ".join(THREAT_LEVELS), positionings=", ".join(POSITIONINGS),
    )
    try:
        payload = salvage_json_blocks(writer(prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Structuring %r failed: %s", name, exc)
        return None
    if not payload:
        logger.warning("No parseable dossier for %r.", name)
        return None

    profile_block = next((b for b in payload if str(b.get("kind")) == "profile"), {})
    threat = str(profile_block.get("threat_level", DEFAULT_THREAT)).lower()
    if threat not in THREAT_LEVELS:
        logger.debug("Unknown threat level %r for %s; defaulting.", threat, name)
        threat = DEFAULT_THREAT
    positioning = str(profile_block.get("positioning", "")).lower()
    if positioning and positioning not in POSITIONINGS:
        positioning = ""

    traction = _claims(payload, "traction", registry, report, f"{name}/traction")
    dossier = CompetitorDossier(
        name=name,
        threat_level=threat,
        positioning=positioning,
        description=strip_inline_citations(str(profile_block.get("description", ""))),
        main_offer=strip_inline_citations(str(profile_block.get("main_offer", ""))),
        marketing_positioning=strip_inline_citations(
            str(profile_block.get("marketing_positioning", ""))),
        target_customer=strip_inline_citations(str(profile_block.get("target_customer", ""))),
        strengths=_claims(payload, "strength", registry, report, f"{name}/strengths"),
        weaknesses=_claims(payload, "weakness", registry, report, f"{name}/weaknesses"),
        risks=_claims(payload, "risk", registry, report, f"{name}/risks"),
        opportunities=_claims(payload, "opportunity", registry, report, f"{name}/opportunities"),
        # An unsourced market-share figure is omitted, never hedged.
        traction=traction[0] if traction else None,
        sources=[{"url": e.url, "label": e.label(), "title": e.title, "tier": e.tier}
                 for e in new_evidence],
    )
    logger.info("Dossier %-22s threat=%-6s %d strength(s), %d weakness(es), %d risk(s)",
                name, threat, len(dossier.strengths), len(dossier.weaknesses), len(dossier.risks))
    return dossier


def run_competitor_report(
    company: CompanyProfile,
    model: str,
    *,
    writer: Writer | None = None,
    grounded_fn: Callable[..., GroundedResult] = grounded_generate,
    max_competitors: int = MAX_COMPETITORS,
    period_label: str = "",
) -> CompetitorReport:
    """Produce the full competitor comparison report."""
    from .contracts import ResearchIntegrity

    integrity = ResearchIntegrity()
    writer = writer or _default_writer(model)

    names, store = discover_competitors(company, model, grounded_fn=grounded_fn,
                                        max_n=max_competitors)
    integrity.llm_calls += 1
    registry = CitationRegistry(store)
    validation = ValidationReport()

    dossiers: list[CompetitorDossier] = []
    for name in names:
        dossier = build_dossier(name, company, model, writer=writer, registry=registry,
                                store=store, report=validation, grounded_fn=grounded_fn)
        integrity.llm_calls += 2
        if dossier is not None:
            dossiers.append(dossier)

    if not dossiers:
        integrity.degradations.append("no competitor could be sourced")
        logger.error("No competitor dossiers could be produced.")

    # Ranked most dangerous first — that is the order an executive reads in.
    dossiers.sort(key=lambda d: (d.threat_rank, -d.evidence_count))

    top_threats: list[Claim] = []
    action_plan: list[Claim] = []
    if dossiers:
        listing = "\n".join(f"  - {d.name} (threat: {d.threat_level}) — {d.main_offer[:90]}"
                            for d in dossiers)
        prompt = SYNTHESIS_PROMPT.format(
            company=company.prompt_block(), competitors=listing,
            evidence=store.prompt_block(), language=language_name(company.language))
        try:
            payload = salvage_json_blocks(writer(prompt))
            integrity.llm_calls += 1
            top_threats = _claims(payload, "top_threat", registry, validation, "synthesis")
            action_plan = _claims(payload, "action", registry, validation, "synthesis")
        except Exception as exc:  # noqa: BLE001
            integrity.degradations.append(f"strategic synthesis failed: {exc}")
            logger.error("Synthesis failed: %s", exc)

    from .evidence import TIER_CORPUS, TIER_WEB

    integrity.evidence_total = len(store)
    integrity.evidence_web = len(store.by_tier(TIER_WEB))
    integrity.evidence_corpus = len(store.by_tier(TIER_CORPUS))
    integrity.publishers = store.publishers()
    integrity.grounding_enabled = len(store) > 0
    integrity.claims_accepted = len(validation.accepted)
    integrity.claims_dropped = validation.dropped_total
    integrity.unresolvable_citations = validation.unknown_evidence_ids

    logger.info("Competitor report: %d dossier(s), %d top threat(s), %d action(s) | %s | %s",
                len(dossiers), len(top_threats), len(action_plan),
                store.summary(), validation.summary())

    return CompetitorReport(
        company=company.name, language=company.language, period_label=period_label,
        dossiers=dossiers, top_threats=top_threats, action_plan=action_plan,
        integrity=integrity,
    )
