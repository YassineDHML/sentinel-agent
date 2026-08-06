# Report requests

One YAML file per report request, supplying the {A}..{F} parameters the team's
specification asks the user for. See `docs/ARCHITECTURE.md` §8b.

```bash
python -m sentinel.pipeline --dry-run --profile requests/ai_healthcare_fr.yaml
```

| File | What it is |
|---|---|
| `weekly_ai_saas.yaml` | The production weekly watch, expressed as a profile (overrides nothing — `config.yaml` stays authoritative for it) |
| `ai_healthcare_fr.yaml` | Demonstration: a different theme, language (fr) and geography (france) |

## ⚠️ Never add an `__init__.py` to this directory

This folder is named `requests`, the same as the HTTP library the collectors use.
It is safe **only because it has no `__init__.py`**: a directory without one is a mere
*namespace package portion*, which always loses to a real installed package. Adding
`__init__.py` would make it a regular package that shadows the `requests` library when
running from the repo root — silently breaking every collector.

`tests/test_request.py::test_requests_dir_does_not_shadow_http_library` guards this.
