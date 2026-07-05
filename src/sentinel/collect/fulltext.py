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
from .base import USER_AGENT, http_get_text, normalize_url

logger = get_logger("collect.fulltext")

POLITE_DELAY = 1.0          # seconds between requests to the same host
MIN_CONTENT_CHARS = 200     # below this we treat extraction as failed
_STRIP_TAGS = ("script", "style", "noscript", "header", "footer", "nav", "aside", "form")

# Per-host caches (process-lifetime). robots parser cache maps host -> RobotFileParser|None.
_robots_cache: dict[str, RobotFileParser | None] = {}
_last_fetch_at: dict[str, float] = {}


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


def fetch_fulltext(url: str, *, delay: float = POLITE_DELAY,
                   min_chars: int = MIN_CONTENT_CHARS) -> str | None:
    """Fetch and extract main text for ``url``. Returns ``None`` on any failure."""
    clean = normalize_url(url)
    if clean is None:
        return None

    try:
        if not _robots_allowed(clean):
            logger.info("robots.txt disallows fetching %s; skipping.", clean)
            return None
        _polite_wait(clean, delay)
        html = http_get_text(clean)
    except Exception as exc:  # noqa: BLE001 - never let a fetch break the pipeline
        logger.warning("Full-text fetch failed for %s: %s", clean, exc)
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
