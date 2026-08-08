# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Sentinel is

An automated strategic-intelligence pipeline for the **AI SaaS B2B** sector. It is a weekly, autonomous
AI **workflow** (deterministic pipeline — *not* an autonomous agent): it collects articles about
AI-tooling companies, deduplicates and filters them, uses an LLM to summarize/classify/analyze, tracks
trends over time in a persistent database, renders an HTML report, and emails it. It runs on GitHub
Actions on a weekly cron. Everything is 100% free tier / open source. Non-commercial internship MVP.

**The full specification is `docs/cahier_des_charges_sentinel.md` (French) — it is the source of truth.**
Read it before making non-trivial changes.

## How this project is built

Development is **incremental, one phase at a time**, per the approved build plan. Do not scaffold the
whole app at once. At the end of each phase: summarize, give run/verify steps, then stop and wait for
the user before starting the next phase. If scope is ambiguous, ask — don't invent it.

Phases 0–10 delivered the v1 weekly watch. Phases 11+ adapt it to the team's **v4 amendment**
(`docs/cahier_des_charges_sentinel.md` §11): two parameterized capabilities — a ~3000-word
deep-research report and a competitor report. Two rules govern that work:

- **The v1 weekly watch must not change.** It is in production. A golden-file test
  (`tests/test_report_snapshot.py`) compares its rendered HTML byte-for-byte; every v2 feature is
  additive and inert unless a request profile is supplied.
- **New behaviour is opt-in.** `apply_profile(settings, None) is settings` — no profile means no
  change, and defaults reproduce the original prompts and source URLs exactly.

## Architecture (the big picture)

Four sequential stages, with Supabase as persistent cross-run memory:

```
COLLECT → PROCESS → ANALYZE → DELIVER
   │         │          │         └─ deliver/ : Gmail SMTP + Slack webhook
   │         │          └─ analyze/ : LLM summarize/classify/trends  +  report/ : Jinja2 HTML
   │         └─ process/ : relevance filter, dedup (URL + title similarity), canonical tagging
   └─ collect/ : rss, hackernews (Algolia), producthunt (GraphQL), googlenews (RSS), gnews, fulltext
                              │
                         db/ (Supabase / PostgreSQL) ── articles · trends · reports  (PERSISTENT)
```

Package lives under `src/sentinel/`; `pipeline.py` is the orchestration entrypoint. Non-obvious pieces
that require reading several files to understand:

- **Supabase is the memory, not a cache.** GitHub runners are ephemeral, so all state (article history,
  weekly trend counts, past reports) lives in Supabase. Trend detection (new vs *accelerating*) reads
  prior weeks back out of the DB — the pipeline is not stateless between runs.
- **Supabase free tier pauses after 7 days idle.** A weekly cron can hit a paused DB. Mitigated two ways:
  a separate lightweight keep-alive GitHub Actions workflow pings `SELECT 1` every 3–4 days, **and** the
  DB client uses retry-with-backoff to survive a cold start. Both are required; don't remove one.
- **LLM fallback is tiered, not a mirror.** Gemini (Flash family) is primary. Groq
  (`llama-3.3-70b-versatile`, ~12k TPM) is a fallback for **token-light tasks only** (summaries,
  classification). Historical-context **trend analysis is Gemini-only** — never dump raw history into
  Groq. If Gemini is down, degrade gracefully to Groq summaries + a *compressed* trend digest.
- **LLM calls are batched.** Multiple articles per request to respect Gemini's ~10–15 RPM — never one
  call per article.
- **Topics are a fixed canonical list** (in `config.yaml`, ~15–25 tags). The LLM classifies each article
  into 0..n of these tags; it does not invent labels. This is what makes trend counting reliable.
- **Dedup is two-layered:** exact by URL (DB `UNIQUE` constraint) + best-effort cross-source dedup via
  normalized-title similarity (the same story from multiple outlets has different URLs).
- **Anti-hallucination is a hard requirement.** Every claim/opportunity in the report must be grounded
  in cited source articles fed to the LLM in context.
- **Collection prefers RSS + official free APIs** (HN Algolia, Product Hunt GraphQL) over HTML scraping,
  because datacenter IPs (GitHub runners) get blocked. Scraping is last resort and respects robots.txt.
  **GNews (100 req/day, snippets only) is a discovery layer** — full article text is fetched separately
  from the article URL for items that pass the relevance filter. Google News RSS search supplements it.
- **Cron is UTC.** Schedule the weekly workflow so the report lands Monday morning French time (CET/CEST).
- **Request profiles parameterize a run without touching any stage.** Every stage takes one duck-typed
  `settings` object, so `request/overlay.py::apply_profile` hands it a modified copy carrying {A}..{F}.
  Only keys the profile sets are overridden. A profile that overrides `topics` is **refused write access
  to `trends`** — that table is keyed `UNIQUE(topic, week)` *globally*, so two taxonomies would silently
  corrupt each other's counts.
- **Grounding and JSON mode cannot be combined** (verified live — the API rejects it). Deep research is
  therefore two-phase: ACQUIRE grounded prose to gather evidence, then WRITE structured JSON *without*
  tools. Also verified: publisher identity arrives in `web.title` (not `web.domain`), `thinking_budget=0`
  is rejected, `time_range_filter` needs second granularity, and **the model decides whether to search** —
  no grounding metadata means no evidence, never "trust the text".
- **Citations are three-tier and enforced in code** (`research/citations.py`): A = collected article,
  B = a web source actually retrieved, C = projection anchored to A/B **and** visibly labelled a
  hypothesis. **The model emits evidence ids, never URLs**, so a fabricated link is structurally
  impossible. Rejected claims are counted, not reworded.

## Conventions

- Python 3.11+. Dependencies pinned in `requirements.txt`. No paid APIs/services and no new heavy
  dependencies without asking first.
- **Config vs secrets split.** Non-secret settings (actor lists, categories, relevance keywords,
  canonical tags, recipients, model names) live in `config.yaml`. All keys/credentials (Gemini, Groq,
  GNews, Supabase, Gmail, Slack) load from environment variables — `.env` locally, GitHub Secrets in CI.
  `.env.example` holds placeholder names only. **Model names come from `config.yaml`, never hardcoded.**
- Each external integration (DB, each source, LLM, email, Slack) lives behind its own module with a clean
  interface so it can be mocked in isolation.
- Logging via the stdlib `logging` module — no `print` in real logic. One log line per meaningful step.
- Provide a `--dry-run` path where sensible (collect/analyze but don't send email / don't write to DB).
- SDK specifics (Gemini SDK, `supabase-py`, Groq SDK, current Flash model names) may be out of date in
  training data — **do not hallucinate API signatures.** Keep integrations behind thin wrappers and flag
  what needs verifying against the installed version.

## Commands

The package uses a `src/` layout, so it must be importable (e.g. `pip install -e .` or `PYTHONPATH=src`).

- Run tests: `pytest`
- Run a single test: `pytest tests/test_<module>.py::<test_name>`
- Run the pipeline (dry run, no email / no DB writes): `python -m sentinel.pipeline --dry-run`

> Tests mock all external calls — they must never hit live APIs or require secrets.
> Some of the above are the intended interface as modules land phase by phase; verify a module exists
> before assuming its command works.

## Target repo structure (built incrementally, not all at once)

```
docs/cahier_des_charges_sentinel.md    (present — the spec)
src/sentinel/
  config.py            # load config.yaml + env vars, validate
  logging_conf.py
  db/                  # supabase client + repositories (articles, trends, reports)
  collect/             # rss, hackernews, producthunt, googlenews, gnews, fulltext
  process/             # relevance filter, dedup (url + title similarity), tagging
  analyze/             # llm client (gemini primary + groq fallback), summarize, classify, trends
  report/              # jinja2 builder
  deliver/             # email (gmail smtp), slack webhook
  pipeline.py          # orchestration / entrypoint
templates/             # jinja2 html
scripts/backfill.py    # seed historical trends
tests/
.github/workflows/     # weekly.yml + keepalive.yml
config.yaml · .env.example · requirements.txt · README.md
```
