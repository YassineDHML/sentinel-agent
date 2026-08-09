"""Supabase connection helper with retry-and-backoff.

The Supabase free tier **pauses after 7 days of inactivity**; a paused project
takes ~30 s to wake, during which requests fail. Two mitigations (spec BF-04):

* a separate keep-alive workflow pings the DB every 3-4 days (see ``keepalive.py``);
* **and** every query here is wrapped in exponential-backoff retry, so a cold
  start costs a retry rather than a failed run.

The actual ``supabase`` SDK (2.31.0) is imported lazily inside
:func:`create_supabase_client`, so this module (and the repositories/tests built
on it) can be imported and unit-tested without the package installed or any
credentials present.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from ..config import get_secret
from ..logging_conf import get_logger

if TYPE_CHECKING:  # only for type hints; never imported at runtime
    from supabase import Client

logger = get_logger("db.client")

# Retry defaults — tuned so total wait comfortably covers a ~30 s cold start:
# 1 + 2 + 4 + 8 = 15 s of sleeps across 5 attempts (capped per-step at max_delay).
DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0


def _sleep(seconds: float) -> None:
    """Indirection over ``time.sleep`` so tests can patch it out."""
    time.sleep(seconds)


def create_supabase_client(
    url: str | None = None,
    key: str | None = None,
    *,
    options: Any | None = None,
) -> "Client":
    """Construct a Supabase client from explicit args or the environment.

    Args:
        url: Supabase project URL. Defaults to the ``SUPABASE_URL`` secret.
        key: Supabase API key. Defaults to the ``SUPABASE_KEY`` secret.
        options: Optional ``SyncClientOptions`` passed straight to the SDK.

    Raises:
        ConfigError: If a needed secret is absent (via :func:`get_secret`).
    """
    from supabase import create_client  # lazy: SDK not needed to import this module

    url = url or get_secret("SUPABASE_URL")
    key = key or get_secret("SUPABASE_KEY")
    if options is not None:
        return create_client(url, key, options)
    return create_client(url, key)


class SupabaseDB:
    """Thin wrapper around a Supabase client that adds retry to every execute.

    Repositories receive an instance of this class and build their queries with
    :meth:`table` / :meth:`rpc`, then run them through :meth:`execute` so the
    backoff policy applies uniformly.
    """

    def __init__(
        self,
        client: "Client",
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
    ) -> None:
        self._client = client
        self._attempts = max(1, attempts)
        self._base_delay = base_delay
        self._max_delay = max_delay

    @classmethod
    def connect(
        cls,
        url: str | None = None,
        key: str | None = None,
        *,
        options: Any | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
    ) -> "SupabaseDB":
        """Create a client from env/args and wrap it. See :func:`create_supabase_client`."""
        client = create_supabase_client(url, key, options=options)
        return cls(client, attempts=attempts, base_delay=base_delay, max_delay=max_delay)

    @property
    def client(self) -> "Client":
        return self._client

    def table(self, name: str):
        """Return a table query builder (``client.table(name)``)."""
        return self._client.table(name)

    def rpc(self, fn: str, params: dict[str, Any] | None = None):
        """Return an RPC query builder (``client.rpc(fn, params)``)."""
        return self._client.rpc(fn, params)

    def execute(self, query: Any, *, retry_on: Callable[[Exception], bool] | None = None):
        """Run ``query.execute()`` with exponential-backoff retry.

        Args:
            query: A postgrest/RPC request builder exposing ``.execute()``.
            retry_on: Predicate deciding whether a given exception is worth
                retrying. ``None`` (the default) retries everything, which is the
                historical behaviour and the right policy for a cold start. Pass
                :func:`sentinel.db.errors.retry_unless_unique` for a query whose
                failure may be *deterministic* — a unique-constraint violation
                fails identically on every attempt, so retrying it just burns
                15 s of backoff and logs five misleading warnings.

        Returns:
            The SDK ``APIResponse`` (has ``.data`` and ``.count``).

        Raises:
            The last underlying exception if all attempts fail, or immediately if
            ``retry_on`` rejects it.
        """
        delay = self._base_delay
        last_exc: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return query.execute()
            except Exception as exc:  # noqa: BLE001 - cold start can surface many error types
                last_exc = exc
                if retry_on is not None and not retry_on(exc):
                    logger.debug("Supabase query failed with a non-retryable error: %s", exc)
                    raise
                if attempt >= self._attempts:
                    logger.error("Supabase query failed after %d attempt(s): %s", attempt, exc)
                    raise
                wait = min(delay, self._max_delay)
                logger.warning(
                    "Supabase query attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    self._attempts,
                    exc,
                    wait,
                )
                _sleep(wait)
                delay *= 2
        # Unreachable (loop either returns or raises), but keeps type-checkers happy.
        assert last_exc is not None
        raise last_exc

    def healthcheck(self) -> bool:
        """Run the ``ping()`` SQL function ("SELECT 1"). Returns True on success."""
        try:
            resp = self.execute(self.rpc("ping"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Supabase healthcheck failed: %s", exc)
            return False
        logger.debug("Supabase healthcheck OK (ping -> %r)", getattr(resp, "data", None))
        return True
