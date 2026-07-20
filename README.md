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

Built incrementally, one phase at a time. Collection, persistence, LLM analysis,
trends, reporting, delivery, and the orchestrating pipeline are all implemented and
tested; the GitHub Actions workflows (weekly run + Supabase keep-alive) are in place.
Remaining: stabilization and an example report.

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

## Running the tests

```bash
pytest
```

`pytest` is configured (in `pyproject.toml`) to find the `src/` layout automatically.
Tests mock all external calls — they never hit live APIs or require secrets.

## Deployment (GitHub Actions)

Two workflows in [`.github/workflows/`](.github/workflows/) run everything in the
cloud, free:

- **`weekly.yml`** — runs the full pipeline on a weekly cron (`0 6 * * 1` = 06:00
  UTC Monday → 07:00 CET / 08:00 CEST, i.e. Monday morning French time; GitHub cron
  is UTC-only). Installs deps, injects all secrets, runs `python -m sentinel.pipeline`,
  and uploads `run.log` + the generated `output/*.html` as a build artifact.
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

Both workflows expose `workflow_dispatch`: repo → **Actions** → pick *Sentinel weekly
report* (or *Supabase keep-alive*) → **Run workflow**. Use this to smoke-test the
deployment without waiting for the cron. Download the run log/report from the run's
**Artifacts** section.

## Project layout

See [`CLAUDE.md`](CLAUDE.md) for the conventions/architecture overview and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full system walkthrough.
