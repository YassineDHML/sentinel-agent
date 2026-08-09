"""Recognising PostgreSQL errors through the Supabase SDK.

The retry policy in :mod:`sentinel.db.client` retries *everything*, which is right
for a cold start but wrong for a **deterministic** failure: a unique-constraint
violation will fail identically on all five attempts, costing 15 s of backoff and
emitting alarming warnings for what is in fact the expected outcome of a claim.

The scheduler relies on that distinction (see
:meth:`sentinel.schedule.runs.RunRepository.claim`), so it lives here rather than
inline.

Verified live (supabase 2.31.0, Aug 2026) by re-inserting an existing
``articles.url``::

    type      postgrest.exceptions.APIError
    .code     '23505'
    .message  'duplicate key value violates unique constraint "articles_url_key"'
    .details  'Key (url)=(https://…) already exists.'

So both probes below match on a real collision. A missing table is
``PGRST205``/"Could not find the table" and correctly does **not** match.

.. note::
   ``supabase-py``/``postgrest-py`` do not guarantee a stable exception *type* for
   PostgREST errors across versions, which is why the attribute path is backed by
   the message text rather than replaced by it. Do not "simplify" this to one of
   them without re-running the check above against the installed SDK.
"""

from __future__ import annotations

# PostgreSQL SQLSTATE for unique_violation. PostgREST surfaces it verbatim.
UNIQUE_VIOLATION = "23505"

_TEXT_MARKERS = ("duplicate key value", "violates unique constraint")


def is_unique_violation(exc: BaseException) -> bool:
    """True when ``exc`` is a PostgreSQL ``unique_violation`` (SQLSTATE 23505)."""
    code = getattr(exc, "code", None)
    if code is not None and str(code).strip() == UNIQUE_VIOLATION:
        return True

    blob = " ".join(
        str(getattr(exc, attr, "") or "") for attr in ("message", "details", "hint")
    )
    blob = f"{blob} {exc}".lower()
    return UNIQUE_VIOLATION in blob or any(m in blob for m in _TEXT_MARKERS)


def retry_unless_unique(exc: BaseException) -> bool:
    """Retry predicate: retry anything **except** a unique violation.

    Pass as ``db.execute(query, retry_on=retry_unless_unique)`` for inserts whose
    collision is a meaningful answer rather than a transient fault.
    """
    return not is_unique_violation(exc)
