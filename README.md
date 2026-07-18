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

Built incrementally, one phase at a time. **Currently: Phase 0 — project skeleton.**
Collection, persistence, LLM analysis, reporting, and delivery are not implemented yet.

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

   > Some pinned SDK versions (Supabase, Google Gemini, Groq) are flagged `VERIFY`
   > in `requirements.txt` — reconfirm the current names/versions before installing.

3. **Configure non-secret settings.** Edit [`config.yaml`](config.yaml) — monitored
   actors, categories, relevance keywords, canonical topic tags, model names, and
   report recipients live here.

4. **Configure secrets.** Copy `.env.example` to `.env` and fill in real values.
   `.env` is git-ignored and must never be committed. (In CI these come from GitHub
   Secrets instead.)
   ```bash
   cp .env.example .env
   ```

5. **Verify the config loads.** After the editable install above:
   ```bash
   python -m sentinel.config
   ```
   This prints a validated summary of `config.yaml` and shows which secrets are
   present in your environment.

   > Not using `pip install -e .`? The package uses a `src/` layout, so prefix the
   > command with the path instead — PowerShell: `$env:PYTHONPATH = "src"; python -m sentinel.config`
   > · bash: `PYTHONPATH=src python -m sentinel.config`.

## Running the tests

```bash
pytest
```

`pytest` is configured (in `pyproject.toml`) to find the `src/` layout automatically.
Tests mock all external calls — they never hit live APIs or require secrets.

## Project layout

See [`CLAUDE.md`](CLAUDE.md) for the architecture overview and the full target
directory structure.
