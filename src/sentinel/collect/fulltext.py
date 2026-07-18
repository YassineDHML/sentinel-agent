"""Full-text extraction for a single article URL (spec BF-01).

Fetches the page with ``requests`` and extracts the main body text with
BeautifulSoup. Respects ``robots.txt`` and applies a polite per-host delay. This
is the *last-resort content step*, used only for articles that have already
passed the relevance filter (so GNews/Google-News discovery snippets get promoted
to full text sparingly).

Returns the extracted text, or ``None`` on any failure / robots disallow — never
raises, so it can't break the pipeline.

Manual smoke test:
    python -m sentinel.collect.fulltext <article-url>
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ..logging_conf import get_logger, setup_logging
from .base import DEFAULT_TIMEOUT, USER_AGENT, get_session, http_get_text, normalize_url

logger = get_logger("collect.fulltext")

POLITE_DELAY = 1.0          # seconds between requests to the same host
MIN_CONTENT_CHARS = 200     # below this we treat extraction as failed
_STRIP_TAGS = ("script", "style", "noscript", "header", "footer", "nav", "aside", "form")

# Rate-limit (HTTP 429) handling.
MAX_429_RETRIES = 2         # retries for a single URL before giving up
RETRY_BACKOFF_BASE = 1.5    # seconds; multiplied by attempt number
MAX_RETRY_AFTER = 20.0      # cap on any Retry-After we'll actually honor
HOST_TRIP_THRESHOLD = 3     # give-ups from one host before we skip it for the run

# Per-host caches (process-lifetime). robots parser cache maps host -> RobotFileParser|None.
_robots_cache: dict[str, RobotFileParser | None] = {}
_last_fetch_at: dict[str, float] = {}
# Circuit breaker: hosts that repeatedly rate-limit us are skipped for the run.
_host_429_count: dict[str, int] = {}
_tripped_hosts: set[str] = set()


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so tests can patch it out."""
    time.sleep(seconds)


def _robots_allowed(url: str) -> bool:
    """Check ``robots.txt`` for our User-Agent. Permissive if robots is unreachable."""
    parsed = urlparse(url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{host}/robots.txt"

    if robots_url not in _robots_cache:
        rp: RobotFileParser | None = RobotFileParser()
        try:
            text = http_get_text(robots_url, timeout=10)
            rp.parse(text.splitlines())
        except Exception as exc:  # noqa: BLE001 - missing/unreachable robots => allow
            logger.debug("No usable robots.txt at %s (%s); allowing.", robots_url, exc)
            rp = None
        _robots_cache[robots_url] = rp

    rp = _robots_cache[robots_url]
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


def _polite_wait(url: str, delay: float) -> None:
    """Sleep so consecutive requests to the same host are at least ``delay`` apart."""
    if delay <= 0:
        return
    host = urlparse(url).netloc
    last = _last_fetch_at.get(host)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < delay:
            _sleep(delay - elapsed)
    _last_fetch_at[host] = time.monotonic()


def extract_main_text(html: str) -> str:
    """Extract readable body text from an HTML document.

    Prefers the ``<article>`` element; otherwise falls back to all ``<p>`` text in
    the body. Boilerplate tags (scripts, nav, footer, ...) are removed first.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    root = soup.find("article") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    text = "\n\n".join(p for p in paragraphs if p)
    return text.strip()


def _retry_after_seconds(response) -> float | None:
    """Parse a numeric ``Retry-After`` header (HTTP-date form is ignored)."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _register_host_429(host: str) -> None:
    """Count a give-up for ``host`` and trip its circuit breaker past the threshold."""
    _host_429_count[host] = _host_429_count.get(host, 0) + 1
    if _host_429_count[host] >= HOST_TRIP_THRESHOLD and host not in _tripped_hosts:
        _tripped_hosts.add(host)
        logger.warning(
            "Circuit breaker: host %s rate-limited %d times; skipping it for the rest of this run.",
            host, _host_429_count[host],
        )


def _fetch_html(url: str, host: str) -> str | None:
    """GET ``url`` with 429-aware backoff. Returns HTML, or ``None`` on 429 give-up.

    Non-429 HTTP errors raise (the caller treats them as a per-article failure).
    """
    session = get_session()
    for attempt in range(MAX_429_RETRIES + 1):
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 429:
            if attempt < MAX_429_RETRIES:
                wait = _retry_after_seconds(response) or (RETRY_BACKOFF_BASE * (attempt + 1))
                wait = min(wait, MAX_RETRY_AFTER)
                logger.info("429 for %s; backing off %.1fs (retry %d/%d).",
                            url, wait, attempt + 1, MAX_429_RETRIES)
                _sleep(wait)
                continue
            _register_host_429(host)
            logger.warning("Full-text fetch gave up on %s after %d 429s.", url, MAX_429_RETRIES + 1)
            return None
        response.raise_for_status()
        return response.text
    return None


def fetch_fulltext(url: str, *, delay: float = POLITE_DELAY,
                   min_chars: int = MIN_CONTENT_CHARS) -> str | None:
    """Fetch and extract main text for ``url``. Returns ``None`` on any failure.

    Hosts that repeatedly rate-limit us (HTTP 429) are tripped by a circuit
    breaker and skipped for the rest of the run, so one hostile host doesn't cost
    dozens of slow, failing requests.
    """
    clean = normalize_url(url)
    if clean is None:
        return None

    host = urlparse(clean).netloc
    if host in _tripped_hosts:
        logger.debug("Skipping %s: host %s circuit-breaker tripped this run.", clean, host)
        return None

    try:
        if not _robots_allowed(clean):
            logger.info("robots.txt disallows fetching %s; skipping.", clean)
            return None
        _polite_wait(clean, delay)
        html = _fetch_html(clean, host)
    except Exception as exc:  # noqa: BLE001 - never let a fetch break the pipeline
        logger.warning("Full-text fetch failed for %s: %s", clean, exc)
        return None

    if html is None:  # 429 give-up (already logged)
        return None

    text = extract_main_text(html)
    if len(text) < min_chars:
        logger.debug("Extracted text for %s too short (%d chars); discarding.", clean, len(text))
        return None
    return text


def _main() -> int:
    setup_logging()
    if len(sys.argv) < 2:
        print("usage: python -m sentinel.collect.fulltext <article-url>")
        return 2
    text = fetch_fulltext(sys.argv[1])
    if text is None:
        print("No text extracted (fetch failed, robots disallow, or too short).")
        return 1
    print(f"\nExtracted {len(text)} chars:\n")
    print(text[:1000] + ("..." if len(text) > 1000 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
