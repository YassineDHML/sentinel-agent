# Sentinel — AI SaaS B2B Strategic Intelligence

Sentinel is a weekly, autonomous AI **workflow** that watches the AI SaaS B2B market:
it collects articles from RSS feeds and free APIs, deduplicates and filters them, uses
an LLM (Google Gemini, with a Groq fallback) to summarize, classify, and detect trends,
tracks those trends over time in a persistent Supabase database, renders an HTML report,
and emails it — all on a free GitHub Actions cron.

Full specification (source of truth): [`docs/cahier_des_charges_sentinel.md`](docs/cahier_des_charges_sentinel.md).
**New to the codebase? Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — a
detailed walkthrough of how the whole system works.

> Non-commercial internship MVP. 100% free tier / open source. No paid APIs.

## Status

**The weekly watch (v1) is complete and running.** Collection, persistence, LLM
analysis, trends, reporting, delivery, the orchestrating pipeline and the GitHub
Actions workflows (weekly run + Supabase keep-alive) are all implemented, tested and
deployed.

**Parameterized deep research (v2) — both capabilities delivered.** The team
specified two new capabilities (see the v4 amendment, §11 of the
[cahier des charges](docs/cahier_des_charges_sentinel.md)); both now run end to end:

- **request profiles** — any theme, language and region via `requests/<slug>.yaml`
  (`--profile`), with the weekly report verified byte-identical;
- **grounded web search** — confirmed working on the free tier, reaching McKinsey,
  BCG, Deloitte, Gartner, Forrester, IDC, WHO and OECD publications;
- **three-tier citation policy** enforced in code, so named sources and forward
  projections are possible without weakening the anti-hallucination guarantee;
- **capability 1** — the ~3000-word deep-research report (`python -m sentinel.research`);
- **capability 2** — the competitor comparison report (`python -m sentinel.competitors`);
- **per-request scheduling** — each request declares its own cadence (weekly /
  monthly / quarterly) and a daily dispatcher delivers it exactly once per period.

Remaining: multi-theme trend memory (letting several themes track trends
independently, which today's globally-keyed `trends` table cannot do).

## Setup

> Requires **Python 3.11+**.

1. **Clone and enter the repo.**

2. **Create a virtual environment and install dependencies.**

   Windows (PowerShell):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install -e .          # puts the `sentinel` package on the import path
   ```

   macOS / Linux:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -e .          # puts the `sentinel` package on the import path
   ```

   > Dependencies are strictly `==`-pinned (reproducibility for an unattended
   > pipeline). All versions have Python 3.11+ wheels.

3. **Configure non-secret settings.** Edit [`config.yaml`](config.yaml) — monitored
   actors, categories, relevance keywords, canonical topic tags, model names, and
   report recipients live here.

4. **Configure secrets.** Copy `.env.example` to `.env` and fill in real values.
   `.env` is git-ignored and must never be committed. (In CI these come from GitHub
   Secrets instead — see [Deployment](#deployment-github-actions).)
   ```bash
   cp .env.example .env
   ```

5. **Create the database schema.** In the Supabase dashboard → **SQL Editor**, paste
   and run the entire contents of [`src/sentinel/db/schema.sql`](src/sentinel/db/schema.sql).
   It creates the `articles`, `trends`, `reports` tables and the `ping()` function,
   and is safe to re-run (idempotent).

6. **Verify the config loads.** After the editable install above:
   ```bash
   python -m sentinel.config          # validated config + which secrets are present
   python -m sentinel.db.keepalive    # confirms Supabase is reachable (SELECT 1)
   ```

   > Not using `pip install -e .`? The package uses a `src/` layout, so prefix the
   > command with the path instead — PowerShell: `$env:PYTHONPATH = "src"; python -m sentinel.config`
   > · bash: `PYTHONPATH=src python -m sentinel.config`.

## Running the pipeline

```bash
python -m sentinel.pipeline --dry-run --limit 5   # end-to-end, no DB writes, no email
python -m sentinel.pipeline --limit 25            # the real weekly run
```

Useful flags: `--week 2026-W28` (regenerate a specific week), `--since 2026-07-01`
(drop older articles on a rerun), `--limit N` (max articles analyzed per run — caps
LLM usage to the free tier; leftovers are analyzed on later runs). Individual stages
are runnable too (`python -m sentinel.collect.rss`, `... .analyze`, `... .report`,
`... .deliver`) — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §10.

## Running the parameterized reports

```bash
python -m sentinel.research --profile requests/ai_healthcare_fr.yaml --dry-run
python -m sentinel.competitors --dry-run
```

Each reads a request file from [`requests/`](requests/) (the {A}..{F} parameters) and
writes an email-ready HTML report to `output/`. `--dry-run` still does the real
research; it just skips the database archive and the email.

## Scheduling (per-request cadence)

Every request declares a `cadence:` — `weekly`, `monthly`, `quarterly` or `once`. A
**daily** dispatcher works out which requests are due and produces exactly one report
per request per period:

```bash
python -m sentinel.schedule --dry-run     # what would run today; generates nothing
python -m sentinel.schedule               # run + deliver everything due
```

Due-ness comes from the calendar plus the `runs` ledger in Supabase, not from a timer,
which gives three properties worth knowing: a **missed day is harmless** (the period is
still current tomorrow), **running twice a day produces one report**, and a **failed
request is retried tomorrow** rather than lost until the next period. Useful flags:
`--only <slug>`, `--force` (re-run a period already delivered), `--date 2026-09-01`
(plan as if it were that day), `--max-runs N` (cap reports per dispatch; the rest are
deferred to the next day, and reported).

> The production weekly watch is deliberately **not** dispatched this way —
> `weekly.yml` already owns it, so `requests/weekly_ai_saas.yaml` carries
> `scheduled: false`. Two owners would mean two Monday emails.

Organisation-wide defaults ({B} language, recipients, default company profile) live in
[`profiles/onboarding.yaml`](profiles/onboarding.yaml); any key a request omits is
inherited from there, then from `config.yaml`.

## Running the tests

```bash
pytest
```

`pytest` is configured (in `pyproject.toml`) to find the `src/` layout automatically.
Tests mock all external calls — they never hit live APIs or require secrets.

## Deployment (GitHub Actions)

Three workflows in [`.github/workflows/`](.github/workflows/) run everything in the
cloud, free:

- **`weekly.yml`** — runs the full pipeline on a weekly cron (`0 6 * * 1` = 06:00
  UTC Monday → 07:00 CET / 08:00 CEST, i.e. Monday morning French time; GitHub cron
  is UTC-only). Installs deps, injects all secrets, runs `python -m sentinel.pipeline`,
  and uploads `run.log` + the generated `output/*.html` as a build artifact.
- **`dispatch.yml`** — runs `python -m sentinel.schedule` **daily** (`30 6 * * *`,
  half an hour after the weekly slot so the two never contend for the same
  per-minute LLM quota) and produces whatever request is due that day. Its
  `workflow_dispatch` inputs expose *dry run*, *only* and *force*.
- **`keepalive.yml`** — runs `python -m sentinel.db.keepalive` every 3 days
  (`0 5 */3 * *`) so the Supabase free tier never hits its 7-day idle pause.

### GitHub Secrets to create

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Create
each of these (values only live here — never in the workflow files or the repo):

| Secret | Required? | Purpose |
|---|---|---|
| `SUPABASE_URL` | ✅ required | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_KEY` | ✅ required | Supabase API key (service_role recommended, server-side) |
| `GEMINI_API_KEY` | ✅ required | Google Gemini (primary LLM) |
| `GMAIL_ADDRESS` | ✅ required | sending Gmail address |
| `GMAIL_APP_PASSWORD` | ✅ required | Gmail **App Password** (needs 2FA; not the account password) |
| `GROQ_API_KEY` | ⭐ recommended | Groq fallback for light LLM tasks |
| `GNEWS_API_KEY` | ○ optional | GNews discovery source (skipped if absent) |
| `PRODUCTHUNT_TOKEN` | ○ optional | Product Hunt source (skipped if absent) |
| `SLACK_WEBHOOK_URL` | ○ optional | Slack notification (skipped if absent) |

> The keep-alive workflow only needs `SUPABASE_URL` + `SUPABASE_KEY`. Optional
> secrets can be left uncreated — the pipeline degrades gracefully around them.

### Trigger a manual run

All three workflows expose `workflow_dispatch`: repo → **Actions** → pick *Sentinel
weekly report*, *Sentinel request dispatcher* or *Supabase keep-alive* → **Run
workflow**. Use this to smoke-test the deployment without waiting for the cron.
Download the run log/report from the run's **Artifacts** section. Start with the
dispatcher's **dry run** input — it prints the plan and sends nothing.

## Configuration reference

All non-secret settings live in [`config.yaml`](config.yaml):

| Key | What it controls |
|---|---|
| `app.name` / `report_language` / `timezone` | report title, narrative language (`en`/`fr`), display timezone |
| `app.weeks_history` | how many prior weeks the trend engine compares against (default 4) |
| `app.log_level` | logging verbosity (env `LOG_LEVEL` overrides) |
| `llm.gemini_model` / `groq_model` | model IDs — **never hardcoded**; `gemini-flash-latest` is a safe rolling alias |
| `llm.batch_size` | articles per LLM call (RPM-safe batching, default 8) |
| `categories` / `actors` | monitored sector categories and companies (+ `aliases` for matching) |
| `relevance.keywords` / `exclude` | in-scope keyword filter / exclusion terms |
| `topics` | the ~20 canonical tags the LLM must classify into (closed list) |
| `sources.*` | per-collector on/off toggles |
| `feeds` | RSS feed list (media + vendor blogs; optional `actor` attribution) |
| `discovery.queries` | keyword queries for Google News / GNews / Hacker News |
| `process.title_similarity_threshold` | cross-source dedup strictness (0–1, default 0.85) |
| `report.subject_prefix` / `recipients` | email subject prefix and recipient list |

Secrets are **never** in this file — they come from the environment (`.env` locally,
GitHub Secrets in CI). See `.env.example` and the [Deployment](#deployment-github-actions)
secrets checklist.

## Free-tier quotas (what the design respects)

| Service | Limit | How the code stays under it |
|---|---|---|
| Gemini Flash | ~10–15 req/min, ~1500/day | batching (`llm.batch_size`), `--limit` cap per run |
| Groq (llama-3.3-70b) | **12k tokens/min**, 30 req/min | fallback for light tasks only; history sent as a compressed digest, never raw |
| GNews | 100 req/day, 10 articles/req, snippets | discovery-only (~6 queries/run, 1 req/sec) |
| Supabase | 500 MB, **pauses after 7 idle days** | keep-alive workflow + retry/backoff on the client |
| Gmail SMTP | normal account sending limits | one email per week |

All of the above are free tiers — **no card, no dollar cost**; the only budget is
rate/quota, and a weekly run uses a tiny fraction of it.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ConfigError: Missing required secret …` | that env var isn't set — add it to `.env` (local) or GitHub Secrets (CI) |
| Gemini `404 … model is no longer available` | the pinned model id was retired — set `llm.gemini_model: gemini-flash-latest` |
| Report emailed but sections 1/2/4 empty | deep analysis was skipped (Gemini down → Groq-only) or no articles were analyzed this week; check the run log |
| `Report … 0 analyzed` | nothing analyzed for this week yet — raise `--limit`, or the analyze stage didn't run |
| Many `429 Too Many Requests` in full-text | a host is rate-limiting datacenter IPs; the circuit breaker skips it after a few tries — harmless, snippet fallback is used |
| Slack `Expecting value: line 1 column 1` | (fixed) Slack returns plain `ok`, not JSON — update to latest |
| Supabase timeout on first weekly run | project was paused; the retry/backoff usually recovers — ensure the keep-alive workflow is enabled |
| `pytest` needs a real key | it shouldn't — tests are hermetic (`tests/conftest.py` strips secrets); mock at the wrapper boundary |

## Project layout & further reading

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full system walkthrough (start here).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — handoff guide, extension recipes, golden rules.
- [`CLAUDE.md`](CLAUDE.md) — conventions/architecture summary for AI-assisted edits.
- [`docs/cahier_des_charges_sentinel.md`](docs/cahier_des_charges_sentinel.md) — the spec (source of truth).
