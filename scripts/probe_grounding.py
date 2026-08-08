"""Probe Google-Search grounding — the free-tier capability check.

Read-only: makes one grounded call and prints what came back. Use it to confirm
grounding still works (quota, model changes, policy changes) before relying on it,
or to sanity-check a research theme's sourcing.

    python scripts/probe_grounding.py "AI in healthcare"
    python scripts/probe_grounding.py "AI in healthcare" --months-back 12
    python scripts/probe_grounding.py --profile requests/ai_healthcare_fr.yaml

Requires GEMINI_API_KEY. Costs one Gemini call against the free-tier quota.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from sentinel.config import ConfigError, load_settings, require_secrets
from sentinel.logging_conf import get_logger, setup_logging
from sentinel.research import EvidenceStore, grounded_generate

logger = get_logger("probe.grounding")

# The model decides whether to search, so the instruction must be explicit —
# otherwise it answers from memory and returns no sources at all.
PROMPT = """\
Using web search, report what major consulting and analyst firms (McKinsey, BCG, Bain,
Deloitte, PwC, Gartner, Forrester, IDC) and institutional sources have recently published
about: {theme}

Name each firm and what it reported. Prefer primary published research over commentary."""


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python scripts/probe_grounding.py")
    parser.add_argument("theme", nargs="?", default=None, help="research theme to probe")
    parser.add_argument("--profile", default=None, help="take the theme from a request profile")
    parser.add_argument("--months-back", type=int, default=None,
                        help="restrict results to the last N months")
    parser.add_argument("--no-resolve", action="store_true",
                        help="skip resolving redirect URIs to publisher URLs (faster)")
    parser.add_argument("--max-tokens", type=int, default=4000)
    args = parser.parse_args()

    setup_logging()
    try:
        settings = load_settings()
        theme = args.theme
        if args.profile:
            from sentinel.request import load_profile

            theme = theme or load_profile(args.profile).theme
        if not theme:
            parser.error("provide a theme or --profile")
        require_secrets(["GEMINI_API_KEY"])
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    since = until = None
    if args.months_back:
        until = dt.datetime.now(dt.timezone.utc)
        since = until - dt.timedelta(days=30 * args.months_back)

    print(f"\nmodel : {settings.llm.gemini_model}")
    print(f"theme : {theme}")
    print(f"window: {f'last {args.months_back} months' if args.months_back else 'unrestricted'}")
    print("-" * 72)

    try:
        result = grounded_generate(
            PROMPT.format(theme=theme),
            settings.llm.gemini_model,
            since=since, until=until,
            max_output_tokens=args.max_tokens,
            resolve_urls=not args.no_resolve,
        )
    except Exception as exc:  # noqa: BLE001 - the point of the probe is to report failure
        print(f"\nGROUNDING FAILED: {type(exc).__name__}: {exc}")
        print("\nIf this is a quota/permission error, grounding is not usable on this key —\n"
              "fall back to corpus-only sourcing (profile setting) for research reports.")
        return 1

    print(f"\ngrounded      : {result.grounded}")
    print(f"finish_reason : {result.finish_reason}"
          + ("  [TRUNCATED — raise --max-tokens]" if result.truncated else ""))
    print(f"tokens        : {result.total_tokens} total, {result.thought_tokens} spent thinking")

    if result.queries:
        print("\nsearches the model actually ran:")
        for q in result.queries:
            print(f"   - {q}")

    if not result.grounded:
        print("\n!! No sources returned: the model answered from memory.")
        print("   Its text is NOT evidence-backed and must not be cited.")
        return 1

    store = EvidenceStore()
    store.add_grounded_sources(result.sources)
    print(f"\n{store.summary()}")
    print("\nsources (evidence id | publisher | resolved URL):")
    for e in store.all():
        url = e.url if len(e.url) <= 78 else e.url[:75] + "..."
        print(f"   [{e.eid}] {e.label():<26} {url}")

    print("\n--- answer preview ---")
    print((result.text or "")[:700])
    print("\nOK — grounding is available and returning citable sources.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
