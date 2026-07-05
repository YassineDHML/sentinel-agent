"""Supabase keep-alive entrypoint.

The Supabase free tier pauses after 7 days of inactivity (spec BF-04). A separate
lightweight GitHub Actions workflow runs this every 3-4 days to reset the idle
counter. It performs a trivial ``SELECT 1`` (via the ``ping()`` SQL function) and
exits ``0`` on success, non-zero on failure.

Run locally with:  ``python -m sentinel.db.keepalive``
"""

from __future__ import annotations

from ..config import ConfigError, require_secrets
from ..logging_conf import get_logger, setup_logging
from .client import SupabaseDB

logger = get_logger("db.keepalive")

REQUIRED_SECRETS = ("SUPABASE_URL", "SUPABASE_KEY")


def ping() -> bool:
    """Connect and run the ``SELECT 1`` healthcheck. Returns True on success."""
    require_secrets(REQUIRED_SECRETS)
    db = SupabaseDB.connect()
    return db.healthcheck()


def main() -> int:
    """CLI entrypoint. Returns a process exit code (0 = alive, 1 = failed)."""
    setup_logging()
    try:
        ok = ping()
    except ConfigError as exc:
        logger.error("Keep-alive misconfigured: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - keep-alive must never crash the workflow
        logger.error("Keep-alive errored: %s", exc)
        return 1

    if ok:
        logger.info("Keep-alive OK — Supabase reachable (SELECT 1).")
        return 0
    logger.error("Keep-alive failed — Supabase healthcheck returned false.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
