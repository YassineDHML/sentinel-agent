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

Development is **incremental, one phase at a time** (Phase 0 → 10, per the approved build plan). Do not
scaffold the whole app at once. At the end of each phase: summarize, give run/verify steps, then stop
and wait for the user before starting the next phase. If scope is ambiguous, ask — don't invent it.

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
