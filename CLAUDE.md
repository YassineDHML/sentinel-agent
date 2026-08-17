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

Phases 0–10 delivered the v1 weekly watch. Phases 11–18 adapted it to the team's **v4 amendment**
(`docs/cahier_des_charges_sentinel.md` §11): two parameterized capabilities — a ~3000-word
deep-research report and a competitor report — plus per-request scheduling and per-request trend
memory. All of it is delivered; see `docs/ARCHITECTURE.md` §13 for the remaining known limitations.
Two rules govern that work:

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
  Only keys the profile sets are overridden.
- **Trends are namespaced per request** (`UNIQUE(request_slug, week, topic)`), and **the scope lives on
  the repository**, not in call signatures: `TrendRepository(db, scope=...)` filters every read *and*
  write, so `trends.py` / `report/builder.py` / `pipeline.py` / `backfill.py` needed no changes. A
  request defaults to its own scope (`trend_scope or slug`) — sharing is opt-in, so you can never
  forget to opt out. `'__default__'` is the historical single-theme watch and owns every pre-existing
  row; `requests/weekly_ai_saas.yaml` points at it explicitly, or running the production watch with
  `--profile` would start an empty second history and mark every topic NEW. `assert_trend_safe` now
  refuses exactly one thing: a **custom taxonomy** writing into a **shared scope** (no key can
  disambiguate two taxonomies in one namespace). The `week` column holds a period key of any cadence
  and keeps its name on purpose — renaming a live production column is the change the no-regression
  rule forbids.
- **Weak signals are a derived view, not a table.** `detect_weak_signals()` is a pure function over the
  trend counts ("keeps coming back but never gets big"). A second store would add a write path that can
  drift out of sync for no gain.
- **Grounding and JSON mode cannot be combined** (verified live — the API rejects it). Deep research is
  therefore two-phase: ACQUIRE grounded prose to gather evidence, then WRITE structured JSON *without*
  tools. Also verified: publisher identity arrives in `web.title` (not `web.domain`), `thinking_budget=0`
  is rejected, `time_range_filter` needs second granularity, and **the model decides whether to search** —
  no grounding metadata means no evidence, never "trust the text".
- **Citations are three-tier and enforced in code** (`research/citations.py`): A = collected article,
  B = a web source actually retrieved, C = projection anchored to A/B **and** visibly labelled a
  hypothesis. **The model emits evidence ids, never URLs**, so a fabricated link is structurally
  impossible. Rejected claims are counted, not reworded.
- **Scheduling has no `next_run_at`.** A request is due when we are inside a period of its cadence
  and the `runs` ledger has no row for `(slug, period_key)`. That is why a missed day self-heals, a
  double dispatch is a no-op, and a `failed` run retries *tomorrow* rather than next period.
  `schedule/due.py` is pure; `schedule/runs.py` owns the claim protocol (INSERT guarded by
  `UNIQUE(request_slug, period_key)`, plus a compare-and-swap UPDATE to reclaim a `failed` row or a
  `running` row abandoned by a killed CI job). Claims pass `retry_on=retry_unless_unique` because
  `SupabaseDB.execute` otherwise retries the *expected* collision five times with backoff.
- **`requests/weekly_ai_saas.yaml` carries `scheduled: false`** — `weekly.yml` already runs the
  production watch; two owners would mean two Monday emails. A test pins the flag.

## Deploying: schema and code are coupled, and they deploy differently

**`db/schema.sql` is applied by hand in the Supabase SQL editor; code reaches production only by
`git push`.** Two different release channels for one contract — so a schema change that renames or
re-keys anything must be **pushed with (or before) the DDL is run**, never after.

This has already bitten once. Phase 18 re-keyed `trends` from `UNIQUE(topic, week)` to
`UNIQUE(request_slug, week, topic)`. The DDL was run while the code sat in unpushed local commits, so
CI kept running the old `on_conflict="topic,week"` against a constraint that no longer existed and
the weekly run died with `42P10` for two consecutive weeks. Local verification passed the whole time,
because locally the code *was* current.

Therefore, after any schema change: check `git status -sb` for `ahead`, and verify against
**`origin/main`** — `git show origin/main:<file>` — not just the working tree. "Committed" is not
"deployed". A traceback whose line numbers don't match your local file is the tell.

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
- Deep research / competitors: `python -m sentinel.research --profile requests/<slug>.yaml --dry-run`
  · `python -m sentinel.competitors --dry-run`
- Scheduling: `python -m sentinel.schedule --dry-run` (plan only — generates and sends nothing;
  stricter than the other CLIs' `--dry-run`, which still produce a local report)

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
  request/             # request profiles ({A}..{F}), settings overlay, locale, onboarding
  research/            # grounding, evidence, citations, deep research, competitors
  schedule/            # per-request cadence: due-ness, runs ledger, daily dispatcher
  report/              # jinja2 builders (weekly · research · competitor)
  deliver/             # email (gmail smtp), slack webhook
  pipeline.py          # orchestration / entrypoint
templates/             # jinja2 html
requests/ · profiles/  # one YAML per report request · company + onboarding profiles
scripts/backfill.py    # seed historical trends
tests/
.github/workflows/     # weekly.yml + dispatch.yml + keepalive.yml
config.yaml · .env.example · requirements.txt · README.md
```
