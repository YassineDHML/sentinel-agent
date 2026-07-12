"""Trend engine — historical memory over the ``trends`` table (spec BF-04).

Turns analyzed articles into weekly per-topic counts, persists them, then reads
prior weeks back out to label each topic NEW / ONGOING / ACCELERATING. Also
builds a **compressed trend digest** used as LLM context — small enough that it
can be fed to the Groq fallback without ever dumping raw article history
(spec BF-03: historical context is Gemini-only; Groq only sees the digest).

Design notes:
* ISO week keys look like ``"2026-W28"`` and match the ``trends.week`` column.
* Acceleration compares the current week's count against the previous N weeks
  (default 4), treating weeks with no row as zero.
* Functions take an explicit ``week`` / repo so they're deterministic in tests.

Demo (no DB, seeded fake history):  python -m sentinel.analyze.trends
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..logging_conf import get_logger

logger = get_logger("analyze.trends")

# Trend status labels.
NEW = "NEW"
ONGOING = "ONGOING"
ACCELERATING = "ACCELERATING"

# Acceleration thresholds (kept explicit so they're easy to tune).
DEFAULT_WEEKS_BACK = 4
ACCEL_FACTOR = 1.5      # current must be >= this * prior average
MIN_ACCEL_COUNT = 3     # ...and at least this many articles, to avoid noise


# --------------------------------------------------------------------------- #
# ISO week helpers
# --------------------------------------------------------------------------- #
def iso_week_of(d: date) -> str:
    """Return the ISO week key (``"YYYY-Www"``) for a date."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def current_iso_week(reference: datetime | None = None) -> str:
    """ISO week key for ``reference`` (defaults to now, UTC)."""
    ref = reference or datetime.now(timezone.utc)
    return iso_week_of(ref.date() if isinstance(ref, datetime) else ref)


def _week_to_monday(week_str: str) -> date:
    """Parse ``"YYYY-Www"`` to the date of that ISO week's Monday."""
    year_s, week_s = week_str.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def prior_weeks(week_str: str, n: int) -> list[str]:
    """Return the ``n`` ISO week keys before ``week_str``, most-recent first."""
    monday = _week_to_monday(week_str)
    return [iso_week_of(monday - timedelta(weeks=k)) for k in range(1, n + 1)]


def week_bounds_iso(week_str: str) -> tuple[str, str]:
    """Return ``(start, end)`` UTC ISO datetimes spanning ``week_str`` (Mon-Mon).

    ``end`` is exclusive (the following Monday), so callers can filter with
    ``start <= collected_at < end``.
    """
    monday = _week_to_monday(week_str)
    start = datetime.combine(monday, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(weeks=1)
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- #
# Counting + persistence
# --------------------------------------------------------------------------- #
@dataclass
class TopicCount:
    topic: str
    article_count: int
    actors: dict[str, int] = field(default_factory=dict)


def count_topics(articles: list[dict]) -> dict[str, TopicCount]:
    """Aggregate analyzed articles into per-topic counts + actor tallies.

    Each article contributes to every canonical topic in its ``topics`` list;
    its ``actor`` (if any) is tallied per topic.
    """
    counts: dict[str, TopicCount] = {}
    for article in articles:
        actor = article.get("actor")
        for topic in article.get("topics") or []:
            tc = counts.get(topic)
            if tc is None:
                tc = counts[topic] = TopicCount(topic=topic, article_count=0)
            tc.article_count += 1
            if actor:
                tc.actors[actor] = tc.actors.get(actor, 0) + 1
    return counts


def record_week_trends(
    articles: list[dict],
    repo: Any,
    *,
    week: str | None = None,
) -> dict[str, TopicCount]:
    """Compute this week's per-topic counts and upsert them into ``trends``.

    Idempotent per ``(topic, week)`` (the repo upserts on that key), so re-running
    a week overwrites its counts rather than duplicating rows.
    """
    week = week or current_iso_week()
    counts = count_topics(articles)
    for tc in counts.values():
        repo.upsert(tc.topic, week, tc.article_count, actors=tc.actors)
    logger.info("Recorded %d topic trend(s) for week %s.", len(counts), week)
    return counts


# --------------------------------------------------------------------------- #
# Status classification
# --------------------------------------------------------------------------- #
@dataclass
class TrendStatus:
    topic: str
    week: str
    count: int
    status: str
    prior_counts: list[int]  # oldest -> newest
    actors: Any = None

    @property
    def prior_avg(self) -> float:
        return sum(self.prior_counts) / len(self.prior_counts) if self.prior_counts else 0.0


def classify_trend_status(count: int, prior_counts: list[int]) -> str:
    """Label a topic given its current count and prior-week counts (0 = absent)."""
    if not any(c > 0 for c in prior_counts):
        return NEW
    avg = sum(prior_counts) / len(prior_counts)
    if count >= MIN_ACCEL_COUNT and count > avg and count >= avg * ACCEL_FACTOR:
        return ACCELERATING
    return ONGOING


_STATUS_ORDER = {ACCELERATING: 0, NEW: 1, ONGOING: 2}


def compute_trend_statuses(
    repo: Any,
    *,
    week: str | None = None,
    weeks_back: int = DEFAULT_WEEKS_BACK,
) -> list[TrendStatus]:
    """Label every topic present in ``week`` against the previous ``weeks_back`` weeks.

    Reads the current and prior weeks from the trends table (via
    ``repo.get_by_week``), so it reflects persisted history, not just this run.
    Results are sorted ACCELERATING → NEW → ONGOING, then by count descending.
    """
    week = week or current_iso_week()
    week_list = prior_weeks(week, weeks_back)  # most-recent first
    prior_by_week = {
        w: {row["topic"]: (row.get("article_count") or 0) for row in repo.get_by_week(w)}
        for w in week_list
    }

    statuses: list[TrendStatus] = []
    for row in repo.get_by_week(week):
        topic = row["topic"]
        count = row.get("article_count") or 0
        # oldest -> newest for readable digests
        prior = [prior_by_week[w].get(topic, 0) for w in reversed(week_list)]
        statuses.append(
            TrendStatus(
                topic=topic,
                week=week,
                count=count,
                status=classify_trend_status(count, prior),
                prior_counts=prior,
                actors=row.get("actors"),
            )
        )

    statuses.sort(key=lambda s: (_STATUS_ORDER.get(s.status, 9), -s.count))
    return statuses


# --------------------------------------------------------------------------- #
# Compressed digest (LLM context, safe for Groq)
# --------------------------------------------------------------------------- #
def _top_actors(actors: Any, k: int = 3) -> str:
    if isinstance(actors, dict) and actors:
        top = [name for name, _ in Counter(actors).most_common(k)]
        return ", ".join(top)
    if isinstance(actors, list) and actors:
        return ", ".join(str(a) for a in actors[:k])
    return ""


def build_trend_digest(statuses: list[TrendStatus], *, week: str | None = None,
                       max_per_status: int = 12) -> str:
    """Render a compact, grouped digest string for use as LLM context.

    Deliberately small (counts + status + brief prior series + top actors) so it
    can be fed to the Groq fallback without ever exposing raw article history.
    """
    week = week or (statuses[0].week if statuses else current_iso_week())
    weeks_back = len(statuses[0].prior_counts) if statuses else DEFAULT_WEEKS_BACK
    lines = [f"Weekly trend digest — {week} (current count vs previous {weeks_back} weeks)"]

    grouped: dict[str, list[TrendStatus]] = {ACCELERATING: [], NEW: [], ONGOING: []}
    for s in statuses:
        grouped.setdefault(s.status, []).append(s)

    for status in (ACCELERATING, NEW, ONGOING):
        items = grouped.get(status) or []
        if not items:
            continue
        lines.append(f"{status}:")
        for s in items[:max_per_status]:
            parts = [f"  - {s.topic}: {s.count}"]
            if status == ACCELERATING:
                parts.append(f"(prev {','.join(str(c) for c in s.prior_counts)})")
            elif status == ONGOING:
                parts.append(f"(prev avg {s.prior_avg:.1f})")
            actors = _top_actors(s.actors)
            if actors:
                parts.append(f"[actors: {actors}]")
            lines.append(" ".join(parts))
        if len(items) > max_per_status:
            lines.append(f"  ... (+{len(items) - max_per_status} more)")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Demo (fabricated in-memory history — no DB, no secrets)
# --------------------------------------------------------------------------- #
class _InMemoryTrends:
    """Minimal repo stand-in for the demo: rows keyed by (topic, week)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict] = {}

    def upsert(self, topic, week, article_count, actors=None):
        self._rows[(topic, week)] = {
            "topic": topic, "week": week, "article_count": article_count, "actors": actors or {}
        }

    def get_by_week(self, week):
        return [r for (t, w), r in self._rows.items() if w == week]


def _main() -> int:
    from ..logging_conf import setup_logging

    setup_logging()
    repo = _InMemoryTrends()
    week = "2026-W28"
    pw = prior_weeks(week, 4)  # [W27, W26, W25, W24]

    # Seed history:
    #  - "AI agents": steady rise then a jump this week   -> ACCELERATING
    #  - "LLM release": no prior appearances               -> NEW
    #  - "pricing change": flat presence                   -> ONGOING
    for w, c in zip(reversed(pw), [3, 4, 5, 6]):  # W24..W27
        repo.upsert("AI agents", w, c, {"OpenAI": c})
        repo.upsert("pricing change", w, 2, {"Anthropic": 2})
    repo.upsert("AI agents", week, 12, {"OpenAI": 7, "LangChain": 5})
    repo.upsert("LLM release", week, 5, {"Mistral AI": 5})
    repo.upsert("pricing change", week, 2, {"Anthropic": 2})

    statuses = compute_trend_statuses(repo, week=week, weeks_back=4)

    print("\n=== Trend statuses ===")
    for s in statuses:
        print(f"  {s.status:12s} {s.topic:16s} count={s.count:<3d} prior={s.prior_counts}")

    print("\n=== Compressed trend digest (LLM / Groq-safe context) ===")
    print(build_trend_digest(statuses, week=week))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
