# Sentinel — How the System Works

> Onboarding guide for new contributors. The business specification (French) is
> [`cahier_des_charges_sentinel.md`](cahier_des_charges_sentinel.md) — it is the source of
> truth for *requirements*. This document explains the *implementation*: what runs, in what
> order, where the data lives, and why it's built this way.

---

## 1. What Sentinel is (in one paragraph)

Sentinel is an **automated weekly strategic-intelligence pipeline** for the AI SaaS B2B
sector. Once a week it collects articles about AI-tooling companies (OpenAI, Anthropic,
Mistral, LangChain, …), filters and deduplicates them, uses an LLM to summarize and
classify each one against a fixed topic list, tracks topic frequencies over time in a
persistent database, asks the LLM for a deeper competitive analysis, renders everything
into an HTML report, and emails it to the team. It is a **deterministic workflow, not an
autonomous agent**: the steps are fixed (A → B → C), and the LLM only acts inside two
narrow, controlled roles (summarize/classify, and the report's narrative analysis).

Everything runs on free tiers: GitHub Actions (compute), Supabase (PostgreSQL storage),
Google Gemini + Groq (LLM), Gmail SMTP + Slack webhook (delivery).

## 2. The big picture

```
                 ┌────────────────────────── GitHub Actions (weekly cron, UTC) ─┐
                 ▼                                                              │
COLLECT ──► PROCESS ──► FULLTEXT ──► ANALYZE ──► TRENDS ──► REPORT ──► DELIVER  │
   │           │            │           │           │          │          │     │
 5 sources   filter +    fetch page   LLM batch   weekly     Jinja2     Gmail   │
 (RSS, HN,   dedup +     text, cache  summarize   counts,    HTML +     SMTP +  │
 PH, GNews,  store new   in DB        + classify  NEW/ACCEL  deep LLM   Slack   │
 GoogleNews)                                      status     analysis           │
                 │                                   │          │               │
                 └────────────── Supabase (PostgreSQL) ─────────┘               │
                        articles · trends · reports                            │
                        (persistent BETWEEN runs — the system's memory)        │
                                                                                │
      keep-alive workflow (SELECT 1 every 3-4 days) ────────────────────────────┘
```

The single entrypoint is **`src/sentinel/pipeline.py`** (`python -m sentinel.pipeline`).
Each stage is also runnable on its own (see §10) — that's how you debug one piece without
running the world.

**The one non-obvious idea to internalize:** GitHub Actions runners are *ephemeral* — the
filesystem is wiped after every run. So **Supabase is not a cache, it's the system's
memory**. Article history, weekly topic counts, and past reports all live there. Trend
detection ("this topic is accelerating") literally means "read the previous 4 weeks back
out of the `trends` table and compare." Delete the DB and Sentinel loses its memory but
not its ability to run.

## 3. Repository layout

```
src/sentinel/
  config.py           # config.yaml + .env loading, typed Settings, secret access
  logging_conf.py     # stdlib logging: one 'sentinel.*' logger namespace
  pipeline.py         # THE entrypoint: wires all stages, graceful degradation
  db/
    schema.sql        # the 3 tables + ping() function (run once in Supabase)
    client.py         # SupabaseDB: supabase-py wrapper + retry/backoff
    repositories.py   # ArticleRepository / TrendRepository / ReportRepository
    keepalive.py      # `SELECT 1` ping, called by the keep-alive workflow
  collect/
    base.py           # normalized article dict, @graceful, HTTP helpers
    rss.py            # feedparser over config feeds (media + vendor blogs)
    hackernews.py     # Algolia HN Search API (official, free, no key)
    producthunt.py    # Product Hunt GraphQL v2 (token required, else skips)
    googlenews.py     # Google News RSS search (no key; redirect URLs — see §5)
    gnews.py          # GNews API — DISCOVERY only (quota-capped, snippets)
    fulltext.py       # robots-aware page fetch + main-text extraction
  process/
    filter.py         # relevance filter (keywords + actors, word-boundary aware)
    dedup.py          # URL dedup + title-similarity dedup
    __init__.py       # process_articles(): filter → dedup → persist new
  analyze/
    llm.py            # LLMClient: Gemini primary + tiered Groq fallback, batching
    prompts.py        # ALL prompt text (iterate here)
    parsing.py        # robust JSON extraction from LLM responses
    summarize.py      # summary response → {url: summary}
    classify.py       # classify response → {url: [canonical tags]} (validated!)
    trends.py         # weekly counts, NEW/ONGOING/ACCELERATING, trend digest
    deep_analysis.py  # Gemini-ONLY narrative analysis with enforced citations
    __init__.py       # analyze_articles(): summarize + classify + persist
  report/
    builder.py        # context assembly + Jinja2 render + archive + local file
  deliver/
    email.py          # Gmail SMTP (app password), HTML email
    slack.py          # optional incoming webhook, no-ops if unconfigured
templates/report.html # the email-friendly report template (inline styles only)
tests/                # 139 tests; ALL external calls mocked (no secrets needed)
config.yaml           # every non-secret setting (see §9)
.env / .env.example   # every secret (never committed)
```

## 4. The data model

Three tables (created by [`db/schema.sql`](../src/sentinel/db/schema.sql), run once in the
Supabase SQL editor):

| Table | Role | Key columns |
|---|---|---|
| `articles` | Permanent memory of every article ever kept | `url` **UNIQUE** (exact-dedup key) · `title` · `source` · `actor` · `topics text[]` · `snippet` (feed teaser, always cheap) · `content` (full text, fetched once then cached) · `summary` (LLM) · `processed` (bool) |
| `trends` | Topic frequency per ISO week | `topic` · `week` (e.g. `"2026-W29"`) · `article_count` · `actors jsonb` · **UNIQUE(topic, week)** so re-running a week overwrites instead of duplicating |
| `reports` | Archive of every generated report | `week` · `generated_at` · `content_html` |

Plus a `ping()` SQL function: supabase-py talks to PostgREST which can't run raw SQL, so
the keep-alive's "`SELECT 1`" is this function called via `rpc("ping")`.

**The normalized article dict** is the currency between stages. Every collector emits
exactly this shape (built by `collect/base.py::make_article`):

```python
{"url", "title", "source", "actor", "published_at", "snippet", "content", "collected_at"}
# after analysis, two more keys: "summary" (str) and "topics" (list[str])
```

An article's life: collected → (maybe) filtered out → stored with `processed=false` →
summarized/classified → `processed=true` + `summary` + `topics` written back → counted
into `trends` → cited in a report.

## 5. Stage by stage

### 5.1 COLLECT — five sources, all behind one interface

Each source is one module exposing `collect_*() -> list[dict]`. All of them are wrapped
in `@graceful`: any exception is logged and returns `[]`, so **one dead source never
kills the run** (requirement BF-07).

| Source | Module | Auth | Notes |
|---|---|---|---|
| RSS feeds | `rss.py` | none | Tech media + official vendor blogs, list in `config.yaml → feeds`. Feeds on a vendor's own blog carry an `actor:` attribution. |
| Hacker News | `hackernews.py` | none | Algolia HN Search API (official). Ask/Show HN posts have no external URL → falls back to the HN discussion page. |
| Product Hunt | `producthunt.py` | `PRODUCTHUNT_TOKEN` | GraphQL v2, newest posts. Skips cleanly if the token is absent. |
| Google News | `googlenews.py` | none | RSS search per keyword (`config.yaml → discovery.queries`). ⚠️ Links are **redirect shells on news.google.com** — robots-blocked, so full text can't be fetched for these. `resolve.py` attempts canonicalization but, as measured, cannot resolve Google's current link format (see §5.3) — these items stay **title/snippet-only**. |
| GNews | `gnews.py` | `GNEWS_API_KEY` | **Discovery layer only**: 100 req/day, 10 articles/req, truncated snippets. It finds candidate URLs; it never provides content (`content=None` by design). |

Why RSS/APIs and not scraping? GitHub runner IPs are datacenter IPs; Cloudflare-protected
sites block them. Feeds and official APIs are reliable from CI; scraping is last resort.

### 5.2 PROCESS — filter, dedup, store

`process/__init__.py::process_articles()` runs three steps and returns per-stage counts:

1. **Relevance filter** (`filter.py`) — keep an article if it matches a config keyword
   *or* mentions a monitored actor (name or alias); drop it if it matches an exclusion
   term (exclusions always win). Matching is **word-boundary aware**: the keyword `AI`
   does *not* match "em**ai**l", but `GPT-4` and the phrase `artificial intelligence`
   match correctly. Side effect: if an actor matched and the collector didn't set one,
   the article is attributed to that actor (feeds the report's competitive-watch section).
2. **Exact URL dedup** (`dedup.py`) — within the batch. *Across runs*, exactness is
   enforced by the DB's `UNIQUE(url)` + idempotent upsert: re-inserting an existing URL
   is a no-op and the pipeline only receives genuinely-new rows back.
3. **Title-similarity dedup** — the same story from different outlets has different URLs
   but near-identical titles. Titles are normalized (lowercase, punctuation stripped) and
   compared with difflib against `config.yaml → process.title_similarity_threshold`
   (0.85). Near-duplicates are grouped; the first-seen article represents the group.
   **Known limitation** (documented in a test): structurally identical headlines that
   differ only by entity/number ("OpenAI raises $1B…" vs "Anthropic raises $2B…") can
   score above 0.85 and over-merge. This layer is best-effort by design; raise the
   threshold if it bites.

New articles are stored with `processed=false`, including their `snippet` (so analysis
always has *some* text even if full-text fetch fails later).

### 5.3 FULLTEXT — fetch once, cache forever

`collect/fulltext.py::fetch_fulltext(url)` fetches the article page and extracts readable
text with BeautifulSoup (prefers `<article>`, strips nav/footer/scripts, requires ≥200
chars). It is deliberately paranoid:

- **robots.txt is respected** (per-host cache; permissive if unreachable);
- **1s polite delay** between requests to the same host;
- **HTTP 429** → retry with backoff, honoring a numeric `Retry-After` header;
- **per-host circuit breaker**: after 3 give-ups from one host (e.g. VentureBeat, which
  hard-blocks datacenter IPs), that host is skipped for the rest of the run;
- it **never raises** — any failure returns `None`.

Fetched text is written back to `articles.content` (`set_content`), so each page is
fetched **at most once ever**. On the next run the text comes from the DB. When fetch
fails, the LLM falls back to the stored snippet; worst case, the title.

**Redirect canonicalization (`collect/resolve.py`).** Aggregator links are redirect
shells, not article URLs, which blocks full-text fetch (robots) and pollutes URL dedup.
`resolve.py` rewrites them to the publisher URL where possible — offline base64 decode
first (free, instant), HTTP redirect-follow as an opt-in fallback. It never raises and
keeps the original URL on failure.

⚠️ **Measured (Aug 2026): neither strategy resolves Google News' current format.**
Offline decode 0/8 live links (Google now emits internal-id payloads, nothing to
decode); HTTP follow 0/5 (302 → ~590 KB JS interstitial still on `news.google.com`,
containing no publisher URL — it's resolved client-side). Resolving today's form would
need Google's undocumented internal batch endpoint, which is too fragile for an
unattended pipeline. Consequences:
- `collect_googlenews(allow_network=False)` is the **default** — the network fallback
  costs ~0.5 s/article (hundreds per run ≈ minutes) for zero measured gain;
- Google News items stay **title/snippet-only** for the LLM. Richer sourcing comes from
  RSS feeds (full text works there) and, for research reports, search grounding.
The module is retained because it is free, instant, correct for the legacy/`/read/`
forms, and is the building block for resolving other redirect shells.

### 5.4 ANALYZE — the tiered LLM layer

This is the most opinionated part of the system. Read
[`analyze/llm.py`](../src/sentinel/analyze/llm.py) alongside this.

**Providers.** `GeminiProvider` (google-genai SDK) is primary; `GroqProvider`
(`llama-3.3-70b-versatile`) is fallback. Model names come from `config.yaml → llm` —
**never hardcoded** (they rot fast; `gemini-flash-latest` is a rolling alias so a retired
dated version can't 404 us).

**The tiering rule (why Groq is not a mirror):** Groq's free tier allows only ~12k tokens
per minute — far too small to ingest a week of articles plus 4 weeks of history. So:

| Task | Tokens | Gemini down → |
|---|---|---|
| Summaries + classification | light (batched, capped input) | **falls back to Groq** ✅ |
| Deep analysis (articles + trend history) | heavy | **skipped entirely** — never sent to Groq ❌ |

The pipeline decides the tier once per run (`pipeline.py::_build_llm`): Gemini key present
→ full mode; only Groq → light tasks only + logged degradation; neither → analysis
skipped, deterministic sections still ship.

**Batching.** Gemini free tier allows ~10–15 requests/minute. One call per article would
throttle immediately, so `LLMClient` packs `batch_size` articles (default 8) into one
prompt, numbered `[1]..[n]`, and asks for a JSON object keyed by number. Per-article input
is capped at 1500 chars (`prompts.py::MAX_ARTICLE_CHARS`).

**Robustness.** Provider calls retry with exponential backoff (longer when the error
looks like a rate limit). If a response is unparseable, the batch is **re-asked once**
(LLMs occasionally emit malformed output); `parsing.py` also tolerates code fences and
merges concatenated JSON objects (`{...}\n{...}` — observed in production). A batch that
still fails is *skipped*, not fatal — those articles stay `processed=false` and are
retried next run.

**Classification is validated, not trusted.** The LLM must pick topics from the canonical
list in `config.yaml → topics` (~20 tags). `classify.py` drops any tag not on the list
(case-insensitive match, canonical casing restored). This is what makes trend counting
reliable: the LLM cannot invent "multi-modal agents" vs "multimodal AI" label variants.

**Prompts live in one place** — [`analyze/prompts.py`](../src/sentinel/analyze/prompts.py)
(and the deep-analysis prompt at the top of `deep_analysis.py`). Iterate there.

### 5.5 TRENDS — the historical memory in action

[`analyze/trends.py`](../src/sentinel/analyze/trends.py). After analysis:

1. `record_week_trends()` counts, per canonical topic, how many of this week's analyzed
   articles carry it (plus which actors), and upserts one row per `(topic, week)`.
2. `compute_trend_statuses()` reads the **previous 4 weeks** back from the `trends` table
   and labels each current topic:
   - **NEW** — zero prior appearances;
   - **ACCELERATING** — count ≥ 3 *and* ≥ 1.5× the prior-week average (both thresholds
     are named constants: `MIN_ACCEL_COUNT`, `ACCEL_FACTOR`);
   - **ONGOING** — everything else. Missing weeks count as zero.
3. `build_trend_digest()` renders a compact text block (status + counts + prior series +
   top actors — **never URLs or article text**). This digest is the *only* historical
   context ever shown to an LLM, which is what makes it safe for Groq's small budget and
   cheap for Gemini.

The NEW/ACCELERATING distinction only means something once several weeks of data exist —
`scripts/backfill.py` (planned, not yet written) will seed past weeks.

### 5.6 REPORT — deterministic skeleton, LLM narrative, enforced citations

[`report/builder.py`](../src/sentinel/report/builder.py) +
[`templates/report.html`](../templates/report.html) (email-safe: tables + inline styles,
no external CSS). Five sections, per the spec:

| # | Section | Produced by |
|---|---|---|
| 1 | Executive summary | LLM (deep analysis) |
| 2 | Competitive watch per actor | LLM (deep analysis) |
| 3 | Trends (accelerating / new / ongoing, with prior counts) | **deterministic** — straight from `trends` data |
| 4 | Opportunities for Welyne | LLM (deep analysis) |
| 5 | Sources consulted (links) | **deterministic** — the week's analyzed articles |

The **deep analysis** (`analyze/deep_analysis.py`) is one Gemini-only call: this week's
analyzed articles (numbered) + the trend digest. **Anti-hallucination is enforced in
code, not just in the prompt**: the model must attach source numbers to every item;
`parse_deep_analysis_response` resolves them to real URLs and **drops any item with zero
valid citations**. An uncited claim cannot physically reach the report. If the deep
analysis fails, sections 1/2/4 show a fallback line and sections 3/5 still ship.

Scope guards: the report covers **analyzed** articles only (those with a summary), and
section 5 caps at 100 links with an "…and N more" note — a 1000-article collection week
can't produce a 1000-link email.

Output goes three places: the `reports` table (archive), `output/<week>.html` (local
inspection), and the delivery stage. A DB archive failure is logged but never blocks the
local file or the email.

### 5.7 DELIVER — email is the product, Slack is a bonus

- [`deliver/email.py`](../src/sentinel/deliver/email.py) — Gmail SMTP-over-SSL (port
  465), stdlib `smtplib`. Sender + **app password** (2FA required — never the account
  password) from env; recipients + subject prefix from config; subject includes the week.
- [`deliver/slack.py`](../src/sentinel/deliver/slack.py) — posts a short highlight
  message (from the deep analysis) to an incoming webhook. **Optional**: no webhook
  configured → clean no-op; post failure → logged, never fatal.

In the pipeline, an **email failure is loud**: logged as an error and, in live mode, the
process exits non-zero so the GitHub Actions run shows red — but the report is already
archived in the DB and on disk, so nothing is lost.

### 5.8 PIPELINE — the conductor

[`pipeline.py`](../src/sentinel/pipeline.py) wires the seven stages, times each one, and
logs one structured line per stage:

```
stage=collect count=2731 elapsed=22.0s
stage=process count=1698 elapsed=5.9s note=filtered=1800 deduped=1698
...
Pipeline finished clean: collect -> process -> fulltext -> analyze -> trends -> report -> deliver
```

Flags: `--dry-run` (no DB writes, no sends — report goes to `output/<week>.dryrun.html`),
`--week` (regenerate a specific week), `--since` (drop articles published before a date —
rerun aid), `--limit` (max articles analyzed per run, default 25, keeps LLM usage
free-tier-safe; leftovers stay `processed=false` and are picked up next run).

Exit codes: `0` = success (possibly degraded — degradations are listed in a final WARNING
line), `1` = fatal (config invalid, DB unreachable in live mode, or email delivery failed).

## 6. The degradation model (memorize this table)

| Failure | Behavior | Run outcome |
|---|---|---|
| One source down | Skipped, logged, others continue | ✅ clean-ish (noted) |
| Full-text fetch fails / host rate-limits | Snippet fallback; circuit breaker skips host | ✅ |
| LLM batch unparseable | One re-ask, then skip batch (articles retry next week) | ✅ |
| Gemini down, Groq up | Summaries/classification via Groq; deep analysis skipped | ✅ degraded |
| No LLM at all | Analysis skipped; trends/sources sections still ship | ✅ degraded |
| Deep analysis fails | Report ships without narrative sections | ✅ degraded |
| DB archive of report fails | Local file + email still happen | ✅ degraded |
| Slack fails / unconfigured | Logged / no-op | ✅ |
| **Email fails (live)** | Loud error, **exit 1** (CI red); report archived anyway | ❌ flagged |
| Supabase cold start | Retry with exponential backoff (see §7) | ✅ usually |

## 7. Supabase specifics: the pause problem

The free tier **pauses a project after 7 days without traffic**, and a weekly cron is the
worst case for that. Two defenses, both required:

1. **Keep-alive**: `python -m sentinel.db.keepalive` runs `SELECT 1` (via the `ping()`
   function) and exits 0/1. A second, tiny GitHub Actions workflow will call it every 3–4
   days (Phase 9).
2. **Retry-with-backoff**: every query goes through `SupabaseDB.execute()` — 5 attempts,
   1→2→4→8s waits — so even a ~30s cold start costs a retry, not a failed run.

## 8. Free-tier budgets the code is shaped around

| Service | Limit | Where it's handled |
|---|---|---|
| Gemini Flash | ~10–15 req/min, 1500/day | batching (8 articles/call), `--limit 25` |
| Groq 70B | **12k tokens/min**, 30 req/min | fallback restricted to light tasks; digest-only history |
| GNews | 100 req/day, 10 articles/req, snippets | discovery-only; ~6 queries/run; 1 req/sec spacing |
| Supabase | 500 MB, pauses after 7 idle days | keep-alive + retry/backoff |
| Gmail | normal account limits | one email/week — negligible |

## 8b. Request profiles — parameterizing a run ({A}..{F})

The original pipeline watched one hardcoded theme. A **request profile** supplies the six
parameters the team's spec asks the user for, and turns the pipeline into a parameterized
engine — without changing a single stage signature.

```
requests/<slug>.yaml  ──load_profile──►  RequestProfile ({A}..{F})
                                              │
                          apply_profile(settings, profile)
                                              ▼
                                   Settings (a frozen copy)
                                              │
              ── unchanged stage calls ───────┴──────────────────
              collect → process → analyze → trends → report → deliver
```

| Var | Profile key | Effect |
|---|---|---|
| {A} theme | `theme` | injected into the summarize / classify / deep-analysis prompts |
| {B} language | `language` | report narrative + template labels + source locale |
| {C} geographic zone | `geo_zone` | Google News `hl`/`gl`/`ceid`, GNews `lang`/`country` |
| {D} time horizon | `horizon.past_months` / `future_years` | retrospective window + projection span |
| {E} sector focus | `sector` | narrows the analysis lens |
| {F} objective | `objective` | orients opportunities/recommendations |

Key properties, each pinned by a test:
- **`apply_profile(settings, None) is settings`** — no profile means literally no change.
- **Only keys the profile sets are overridden**; everything else keeps its `config.yaml`
  value, so the production weekly watch is untouched.
- **The (`en`, `world`) locale reproduces the historical URLs byte-for-byte** — the
  Google News URL and the GNews params are identical to the previously hardcoded ones.
- **Prompt/template defaults reproduce the original text**, so the English report is
  byte-identical (the golden snapshot test is the tripwire).
- **A profile overriding `topics` is refused write access to `trends`** — that table is
  keyed `UNIQUE(topic, week)` *globally*, so two taxonomies would overwrite each other.
  The run still produces its report; only trend persistence is skipped, with a warning.

Labels live in `report/i18n.py` (`HEADINGS[lang]`); the template holds no English.

Run one:
```bash
python -m sentinel.pipeline --dry-run --profile requests/ai_healthcare_fr.yaml
```

## 8c. Deep research: grounded sourcing + the three-tier citation policy

The weekly watch cites only articles it collected. The deep-research report must also
cite **named research houses** (McKinsey, BCG, Gartner, IDC…) and make **forward
projections** — neither of which the original "delete anything uncited" rule permitted.
Both are solved by giving the model a real search tool and replacing the single rule
with three enforced tiers.

### Verified free-tier behaviour (live, Aug 2026 — `gemini-flash-latest` → `gemini-3.6-flash`)

Run `python scripts/probe_grounding.py "<theme>" --months-back 12` to re-verify.

| Question | Answer |
|---|---|
| Is Google-Search grounding available on the **free tier**? | **Yes.** One probe ran 8 searches and returned 31 sources. |
| Does it reach the named research houses? | **Yes** — `mckinsey.com`, `bcg.com`, `deloitte.com`, `gartner.com`, `forrester.com`, `idc.com`, plus `who.int`, `oecd.org`, `nber.org`. |
| Are the citation links usable? | **26 of 31 resolved** to durable publisher URLs (e.g. `bcg.com/publications/2026/how-ai-agents-will-transform-health-care`). |
| Can grounding be combined with JSON mode? | **No** — returns a 400 / no candidates. **This is why research is two-phase.** |
| Does `time_range_filter` work? | **Yes**, but only at second granularity — microseconds raise *"Granularity of nano is not supported"*. |
| Can thinking be disabled? | **No** — `thinking_budget=0` is rejected; a small positive budget is accepted and stops thinking from eating the output allowance (one call spent 2,298 of 3,290 tokens thinking, then truncated). |
| Where is the publisher name? | In **`web.title`** — `web.domain` was `None` on every chunk observed. Read both. |
| Does it always search? | **No.** Searching is model-decided; it will answer from memory and return *no* grounding metadata. That must be treated as "no evidence", never as fact. |

### Two-phase architecture (forced by the JSON-mode incompatibility)

```
ACQUIRE                       EvidenceStore                WRITE
grounded prose calls   ──►    every item gets an id   ──►  ungrounded JSON calls
(search tool ON,              A1,A2… collected article     (JSON mode ON, no tools)
 JSON mode OFF)               B1,B2… retrieved web page    may cite ids ONLY
```

### The three tiers (`research/citations.py`)

| Tier | Meaning | Enforcement |
|---|---|---|
| **A** | Cites a collected article | must resolve to Tier A evidence |
| **B** | Cites a web source the search actually retrieved | must resolve to Tier B evidence |
| **C** | Projection / scenario / argued hypothesis | must be anchored to ≥1 A or B item **and** carries a visible hypothesis label |

Two structural safeguards, not just prompt instructions:
1. **The model never writes a URL** — it emits evidence ids. An invented id resolves to
   nothing and the claim is dropped, so a fabricated link cannot exist.
2. **Publisher names are stored beside links**, so attribution survives an expired
   grounding redirect.

Everything rejected is **counted, not silently reworded** (`ValidationReport`), and those
counts belong in the report's methodology footer so the guarantee stays auditable.
Note the counters use two different units: `unknown_evidence_ids` counts unresolvable
*citations*; `dropped_*` count *claims*. Never sum them.

**Fallback if grounding ever becomes unavailable:** source Tier B from curated
institutional RSS feeds instead — a profile setting, not a rewrite.

## 8d. The deep-research report (~3000 words)

`python -m sentinel.research --profile requests/<slug>.yaml`

### Why it is not one big LLM call

A 3000-word report is ~4,500 output tokens, and extended thinking is charged against
the same allowance. A single call that truncates loses **everything**. So generation is
staged, and every stage degrades instead of aborting:

```
ACQUIRE   3 grounded searches (prose, tools ON)  ──►  EvidenceStore (A*/B* ids)
WRITE     §2 analysis  →  §3 implications  →  §1 synthesis   (JSON, tools OFF)
MEASURE   count words in Python  ──►  TRIM deterministically  ──►  render
```

Four mechanisms make the length reliable (`research/budget.py`):

1. **Structural budgeting, not word instructions.** Python decides how many blocks each
   section gets and how long each should be; the prompt asks for something *countable*
   ("3 paragraphs of ~105 words"), which models obey far better than "write 3000 words".
2. **Deliberate overshoot** (×1.05) — trimming is free, expanding costs another call.
3. **Measurement in Python** — the model's own estimate is never trusted.
4. **Deterministic trimming** with floors: never below 5 key takeaways, and scenarios
   and projections are never dropped.

### Flat blocks — what survives truncation

Every part of the report is a block with a `kind` from a **closed vocabulary**
(`research/contracts.py`), delivered as `{"blocks": [...]}`. If a response is cut off,
`salvage_json_blocks` decodes elements one at a time and keeps every complete one, so a
section truncated at 80% still delivers 80% of its content. A nested schema would lose
all of it. Unknown `kind` values are dropped — the same discipline the canonical topic
list applies to trends.

### §1 is written last

The operational synthesis is *printed* first but *generated* last, from the finished
body. Deriving it from the completed analysis is the strongest guard against the summary
contradicting the report — and it is how a human analyst works. A **key-figures ledger**
extracted during ACQUIRE is injected into every writing call, so a number quoted in §1
matches the one in §2.

### What the reader sees

Three mandatory sections plus a **methodology footer** carrying the real counts (evidence
gathered, searches run, claims retained, claims dropped, unresolvable citations, word
count). Forward-looking blocks render with a visible **HYPOTHESIS** badge and the basis
they extrapolate from, so a projection can never read as established fact.
`templates/research_report.html` is table-based with inline styles only — no scripts, no
external assets — and the model never emits HTML (JSON → context → Jinja, autoescape on).

## 9. Configuration: two files, one rule

**The rule: secrets in the environment, everything else in `config.yaml`.**

- [`config.yaml`](../config.yaml) — actors + aliases, categories, RSS feeds, discovery
  queries, relevance keywords/exclusions, the ~20 canonical topics, LLM model names +
  batch size, dedup threshold, report language + subject + recipients, source on/off
  toggles. Loaded and validated into a frozen typed `Settings` object by
  [`config.py`](../src/sentinel/config.py) — a missing/invalid key fails fast with a
  clear `ConfigError`.
- **`.env`** (local) / **GitHub Secrets** (CI) — the nine secrets, all listed with
  placeholders in [`.env.example`](../.env.example): `GEMINI_API_KEY`, `GROQ_API_KEY`,
  `GNEWS_API_KEY`, `PRODUCTHUNT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `GMAIL_ADDRESS`,
  `GMAIL_APP_PASSWORD`, `SLACK_WEBHOOK_URL`. Code reads them via
  `config.get_secret`/`require_secrets`; `.env` never overrides real env vars, so CI
  secrets win.

## 10. Running things (cheat sheet)

```powershell
# setup (once)
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt && pip install -e .
cp .env.example .env            # then fill in secrets
# + run src/sentinel/db/schema.sql once in the Supabase SQL editor

python -m sentinel.config                 # validate config, show which secrets are set
python -m sentinel.db.keepalive           # SELECT 1 against Supabase (exit 0/1)

# individual stages (debugging)
python -m sentinel.collect.rss            # one source, live
python -m sentinel.collect.fulltext URL   # extract one page's text
python -m sentinel.process                # collect + filter/dedup, dry, with counts
python -m sentinel.analyze --limit 5      # summarize/classify stored articles
python -m sentinel.analyze.trends         # NEW/ACCELERATING demo on fake history
python -m sentinel.report                 # render a demo report to output/
python -m sentinel.report --live          # real report from DB data
python -m sentinel.deliver --dry-run      # delivery without sending
python -m sentinel.deliver --to me@x.com  # real test email to yourself

# the whole thing
python -m sentinel.pipeline --dry-run --limit 5    # end-to-end, no writes/sends
python -m sentinel.pipeline                        # the real weekly run

pytest                                     # 139 tests, all externals mocked, no secrets
```

## 11. Testing philosophy

`tests/` holds 139 tests and **none of them touch the network, the DB, or an LLM**:

- collectors are tested against **saved fixture payloads** (`tests/fixtures/`);
- Supabase is a `MagicMock`/in-memory fake; the SMTP client and Slack webhook are fakes;
- LLM providers are scripted fakes (`FakeProvider`), which is how batching, fallback,
  re-ask, and citation-validation logic are asserted deterministically;
- `tests/test_pipeline.py` runs the **entire pipeline** with every external mocked and
  asserts stage order and each degradation behavior.

If you add an integration, keep this invariant: the SDK call goes behind a thin wrapper,
and tests mock the wrapper. `pytest` must stay runnable on a machine with zero secrets.

## 12. Deliberate design decisions (don't re-litigate casually)

1. **Workflow, not agent** — fixed stages are debuggable and reliable for a weekly batch
   job; the LLM never decides control flow.
2. **Supabase over SQLite** — runners are ephemeral; memory must live off-box.
3. **Tiered fallback, not mirrored** — Groq physically can't hold the historical context
   (12k TPM), so it only ever gets light tasks and the compressed digest.
4. **Canonical topics** — a closed tag list turns fuzzy LLM output into countable data.
5. **Citations enforced in code** — the report can't contain a claim without a source
   URL that actually exists in this week's articles.
6. **Everything degrades, nothing crashes** — a weekly unattended run must produce *the
   best report it can* from whatever survived, and tell you what didn't.
7. **Strict `==` version pins** — reproducibility beats freshness for an unattended
   pipeline; bump versions deliberately.

## 13. Current status & what's left

Built and tested (Phases 0–8): config/logging, persistence + keep-alive, all five
collectors + full-text, filter/dedup, LLM analysis, trends, report, delivery, and the
orchestrating pipeline.

Also delivered: GitHub Actions deployment (`weekly.yml` + `keepalive.yml`), stabilization
and docs, and the historical `scripts/backfill.py`.

**In progress — parameterized report requests (v2).** The team specified two new
capabilities: a periodic ~3000-word deep-research report parameterized by theme /
language / geography / time horizon / sector / objective, and a competitor-comparison
report. Foundations landed first:
- `period.py` — week/month/quarter/ad-hoc reporting periods (byte-compatible with the
  existing ISO-week helpers);
- `collect/dated.py` — arbitrary date-bounded discovery queries;
- `GeminiProvider` generation knobs (`max_output_tokens`, `tools`, `system_instruction`, …)
  and `generate_detailed()` for truncation detection + grounding metadata;
- additive `reports` columns + the `report_sources` citation ledger (A/B/C tiers);
- a golden-file snapshot test guarding the weekly report against regressions.

Remaining: request profiles (YAML) + settings overlay, search grounding + the three-tier
citation policy, the two report generators, and per-request scheduling.

**Known limitation** (measured, not merely unimplemented): Google News links cannot be
resolved to publisher URLs with any free method — see §5.3. Those items remain
title/snippet-only.
