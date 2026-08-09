# Report requests

One YAML file per report request, supplying the {A}..{F} parameters the team's
specification asks the user for. See `docs/ARCHITECTURE.md` §8b.

```bash
python -m sentinel.pipeline --dry-run --profile requests/ai_healthcare_fr.yaml
```

| File | Type | Cadence | What it is |
|---|---|---|---|
| `weekly_ai_saas.yaml` | weekly watch | weekly *(unscheduled)* | The production weekly watch, expressed as a profile (overrides nothing — `config.yaml` stays authoritative for it) |
| `ai_healthcare_fr.yaml` | deep research | monthly | Capability 1: a different theme, language (fr) and geography (france) |
| `competitors_quarterly.yaml` | competitor scan | quarterly | Capability 2: ranked rivals + action plan, driven by `profiles/company.yaml` |

## Scheduling

`cadence:` says how often a request should be produced; `scheduled:` says whether the
daily dispatcher may produce it. The two are separate on purpose — `scheduled: false`
parks a request without deleting it or losing its cadence, and it stays runnable by
hand.

```bash
python -m sentinel.schedule --dry-run        # what is due today
python -m sentinel.schedule --only <slug>    # just this one
```

Due-ness is derived from the calendar and the `runs` ledger: a request is due when we
are inside a period of its cadence and no run exists for it yet. So a missed day
self-heals, a double dispatch is a no-op, and a failure retries tomorrow.

`weekly_ai_saas.yaml` sets `scheduled: false` because `.github/workflows/weekly.yml`
already runs the production watch every Monday — two owners would mean two emails. A
test pins that flag.

## Trend history

Each request keeps its **own** trend history, so several themes can be watched at once
without corrupting each other's counts. The namespace defaults to the request's slug —
sharing is opt-in, never something you can forget to opt out of:

```yaml
trend_scope: __default__     # contribute to the historical single-theme watch
trend_scope: retail_watch    # or pool with another request
```

`weekly_ai_saas.yaml` is the one file that sets it, because it *is* the production watch
and must read the history the unprofiled pipeline has always written.

One combination is still refused: a request that overrides `topics` **and** writes into a
scope it doesn't own. Two taxonomies in one namespace can't be told apart, so trends are
simply not persisted for that run (the report is still produced). Remove `trend_scope`
and it works.

Seed a scope's history from past weeks with:

```bash
python scripts/backfill.py --weeks 6 --scope <slug>
```

## Inherited defaults

Any key a request omits falls back to [`../profiles/onboarding.yaml`](../profiles/onboarding.yaml)
(the organisation's answers: {B} language, {C} zone, {F} objective, recipients, default
company profile), and recipients fall back once more to `config.yaml`. A key set in the
request always wins.

## ⚠️ Never add an `__init__.py` to this directory

This folder is named `requests`, the same as the HTTP library the collectors use.
It is safe **only because it has no `__init__.py`**: a directory without one is a mere
*namespace package portion*, which always loses to a real installed package. Adding
`__init__.py` would make it a regular package that shadows the `requests` library when
running from the repo root — silently breaking every collector.

`tests/test_request.py::test_requests_dir_does_not_shadow_http_library` guards this.
