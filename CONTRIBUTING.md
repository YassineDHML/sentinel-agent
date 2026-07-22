# Contributing & Handoff Guide

This is the practical guide for anyone taking over or extending Sentinel after the
internship. For *how the system works*, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
first; for *requirements*, the spec is [`docs/cahier_des_charges_sentinel.md`](docs/cahier_des_charges_sentinel.md).

## Getting productive in 15 minutes

1. Read `docs/ARCHITECTURE.md` (§2 the big picture, §5 stage-by-stage, §6 degradation).
2. Set up locally (see the README "Setup"): venv, `pip install -r requirements.txt`,
   `pip install -e .`, copy `.env`, run `schema.sql` in Supabase.
3. Run `pytest` — 148 tests, no secrets needed. If they pass, your environment is good.
4. Run `python -m sentinel.pipeline --dry-run --limit 5` to watch the whole flow end
   to end without writing anything.
5. Poke one stage: `python -m sentinel.collect.rss`, `... .process`, `... .report`.

## Golden rules (how this codebase is meant to evolve)

- **Secrets only in the environment.** Never hardcode a key; read it via
  `config.get_secret`. Non-secret settings go in `config.yaml`, loaded through
  `config.load_settings`. `.env` is git-ignored — keep it that way.
- **Model names come from config**, never hardcoded (`config.yaml → llm`). They change
  often; a hardcoded id will 404 one day.
- **Every external call goes behind a thin wrapper**, so tests can mock it. DB →
  `SupabaseDB`; LLM → provider classes; HTTP → `collect/base.py` helpers; email/Slack →
  their `deliver/` modules.
- **Tests never touch the network, DB, or an LLM.** `tests/conftest.py` strips secret
  env vars before every test to enforce this. Mock at the wrapper boundary; use the
  saved fixtures in `tests/fixtures/`. `pytest` must stay runnable on a machine with
  zero secrets.
- **Fail gracefully.** A new source, LLM task, or delivery channel must degrade (log +
  continue / return empty) rather than crash the weekly run. See the degradation table
  in `ARCHITECTURE.md` §6 for the expected behavior of each failure.
- **Strict `==` version pins** in `requirements.txt`. Bump deliberately (and re-run the
  suite), never casually.
- **Keep prompts in `analyze/prompts.py`** (and the deep-analysis prompt in
  `deep_analysis.py`). That's the one place to iterate on analysis quality.

## Common extension tasks

**Add a monitored actor / feed / keyword** — edit `config.yaml` (`actors`, `feeds`,
`relevance`, `discovery.queries`). No code change.

**Add a canonical topic** — add it to `config.yaml → topics`. The LLM will start using
it; historical weeks simply won't have it. Keep the list ~15–25 tags (spec BF-03).

**Add a collection source** — create `collect/<source>.py` exposing
`collect_<source>(...) -> list[dict]`, using `collect/base.py::make_article` for the
normalized shape and `@graceful` for isolation. Add a fixture + parser test. Wire it
into `pipeline.py`'s `sources` dict and add a `sources.<name>` toggle in `config.yaml`.

**Change the report layout** — edit `templates/report.html` (inline styles only — it's
email HTML) and, if you add data, `report/builder.py::build_report_context`. Add an
assertion in `tests/test_report.py` (renders from fixtures, no LLM).

**Tune trend sensitivity** — the thresholds are named constants in `analyze/trends.py`
(`ACCEL_FACTOR`, `MIN_ACCEL_COUNT`, `DEFAULT_WEEKS_BACK`).

## Running the test suite

```bash
pytest                       # all
pytest tests/test_pipeline.py -q
pytest -k dedup              # by keyword
```

## Operational handoff checklist

- [ ] GitHub Secrets are set (see README "Deployment"). Rotate them out of the previous
      owner's accounts.
- [ ] `config.yaml → report.recipients` points at the right people.
- [ ] The weekly + keep-alive workflows are enabled in the repo's Actions tab.
- [ ] Confirm the keep-alive has kept Supabase awake (check its recent runs).
- [ ] After takeover, trigger a manual `workflow_dispatch` run and read the artifact log.

## Known TODOs (good first issues)

- Resolve Google News **redirect URLs** to the real publisher URL before full-text
  fetch/dedup (currently those items are snippet/title only). See `collect/googlenews.py`.
- Cross-**run** title dedup against DB history (currently dedup is within-batch + exact
  URL across runs).
- Raise the weekly `--limit` in `weekly.yml` if you want fuller reports (watch the
  Gemini daily quota).
