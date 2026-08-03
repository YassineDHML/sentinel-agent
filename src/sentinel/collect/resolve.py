"""Redirect resolution for aggregator links (Google News, etc.) — spec BF-01.

Google News RSS item links are opaque redirect shells on ``news.google.com``
(e.g. ``https://news.google.com/rss/articles/CBMi...?oc=5``), not the real
publisher URL. That breaks two downstream steps:

* full-text extraction skips them — ``news.google.com/robots.txt`` disallows the
  ``/rss/`` path — so no body text is ever fetched;
* even if fetched, they're redirect shells rather than the article, so summaries
  degrade to title-only.

This module resolves such links to the true publisher URL *before* the
full-text / relevance / dedup steps, so the canonical stored URL is the
publisher's (never a ``news.google.com`` redirect, which would also pollute
URL-based dedup). Two strategies, cheapest first:

1. **Offline decode** (:func:`decode_google_news_url`) — many Google News links
   base64-encode the destination URL in the path. We decode it locally: no
   network, no rate limit. Fast path.
2. **HTTP redirect follow** (:func:`follow_redirect`) — fall back to a GET with a
   browser-like User-Agent, letting ``requests`` follow the redirect chain and
   capturing the final URL.

Both are best-effort and never raise: on any failure the original URL is kept, so
resolution can only improve a link, never break the pipeline (spec BF-07).

⚠️ **MEASURED STATE OF PLAY (verified against live Google News, Aug 2026) —
neither strategy currently resolves Google News links:**

* offline decode: **0/8** live links — Google now emits an internal-id payload
  (``AUx_yqL...``) rather than a base64-embedded URL, so there is nothing to decode;
* HTTP follow: **0/5** live links — the request 302s to a ~590 KB JavaScript
  interstitial still on ``news.google.com`` whose HTML contains **no** publisher
  URL (it is resolved client-side), so ``resp.url`` never leaves the redirect host.

Resolving the current form would require Google News' internal batch endpoint:
fragile, undocumented, and not something to depend on in an unattended pipeline.
This module is therefore retained as a **free, instant, correct-when-possible**
canonicalization layer (it still handles the legacy embedded-URL form, the
``/read/`` form, and other aggregators added to :data:`REDIRECT_HOSTS`) — and as the
building block for resolving other redirect shells. Callers should keep
``allow_network=False`` while the numbers above hold: the network path costs ~0.5 s
per article for no gain (``collect_googlenews`` defaults accordingly).

Consequence to remember: Google News items remain **title/snippet-only** for
LLM analysis. Richer sourcing comes from feeds and search grounding, not from here.

Manual smoke test:
    python -m sentinel.collect.resolve <redirect-url>
"""

from __future__ import annotations

import base64
import re
import sys
from urllib.parse import urlparse

from ..logging_conf import get_logger, setup_logging
from .base import DEFAULT_TIMEOUT, get_session

logger = get_logger("collect.resolve")

# Hosts whose article links are redirect shells we should resolve. Kept as a
# tuple so "and similar" aggregators can be added without touching call sites.
REDIRECT_HOSTS: tuple[str, ...] = ("news.google.com",)

# A browser-like User-Agent. Google serves bare redirect shells / consent walls
# to unknown agents; a common desktop UA gets the plain redirect chain instead.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Match a plain http(s) URL embedded in decoded bytes. Restricted to RFC-3986
# URL characters so it stops cleanly at the first protobuf/length byte that
# surrounds the URL in the decoded payload (rather than swallowing trailing junk).
_URL_IN_BYTES = re.compile(rb"https?://[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%\[\]]{6,}")

# Path markers that precede the base64 payload in a Google News redirect link.
_PAYLOAD_MARKERS = ("/articles/", "/read/")


def is_redirect_url(url: object) -> bool:
    """True if ``url`` points at a known redirect/aggregator host."""
    if not url or not isinstance(url, str):
        return False
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in REDIRECT_HOSTS)


def decode_google_news_url(url: str) -> str | None:
    """Best-effort offline decode of a Google News link to the publisher URL.

    Many ``news.google.com/rss/articles/<payload>`` links base64-encode the real
    URL inside ``<payload>``. Decode it locally and pull out the first embedded
    ``http(s)`` URL. Returns ``None`` if the payload can't be decoded or carries
    no plain URL (the newer encoded form needs an online lookup — see
    :func:`resolve_redirect`).
    """
    try:
        path = urlparse(url).path
        payload = ""
        for marker in _PAYLOAD_MARKERS:
            idx = path.find(marker)
            if idx != -1:
                # segment right after the marker, ignoring any further path parts
                payload = path[idx + len(marker):].split("/")[0]
                break
        if not payload:
            return None
        # base64url, tolerant of the missing padding these payloads usually have
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except Exception as exc:  # noqa: BLE001 - malformed payloads are expected
        logger.debug("Google News decode failed for %s: %s", url, exc)
        return None

    match = _URL_IN_BYTES.search(raw)
    if not match:
        return None
    try:
        found = match.group(0).decode("utf-8")
    except UnicodeDecodeError:
        found = match.group(0).decode("latin-1")
    # Guard: never "resolve" one redirect link into another.
    if is_redirect_url(found):
        return None
    return found


def follow_redirect(url: str, *, session: object | None = None,
                    timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """Resolve ``url`` by following its HTTP redirect chain to the final URL.

    Uses a browser-like User-Agent and a streamed GET so we capture the final URL
    without downloading the whole body. Returns ``None`` on any error or if the
    chain never left the redirect host.
    """
    sess = session or get_session()
    resp = None
    try:
        resp = sess.get(
            url,
            headers={"User-Agent": BROWSER_UA},
            allow_redirects=True,
            timeout=timeout,
            stream=True,
        )
        final = resp.url
    except Exception as exc:  # noqa: BLE001 - a failed follow must not break the run
        logger.debug("Redirect follow failed for %s: %s", url, exc)
        return None
    finally:
        if resp is not None:
            resp.close()

    if not final or is_redirect_url(final):
        return None
    return final


def resolve_redirect(url: str, *, allow_network: bool = True,
                     session: object | None = None) -> str:
    """Resolve a redirect/aggregator ``url`` to its publisher URL.

    Non-redirect URLs are returned unchanged. For redirect URLs, tries the free
    offline decode first, then (if ``allow_network``) an HTTP redirect follow.
    Always returns a usable URL — the original if resolution fails, so the caller
    can substitute unconditionally.
    """
    if not is_redirect_url(url):
        return url

    decoded = decode_google_news_url(url)
    if decoded:
        logger.debug("Resolved via decode: %s -> %s", url, decoded)
        return decoded

    if allow_network:
        followed = follow_redirect(url, session=session)
        if followed:
            logger.debug("Resolved via HTTP: %s -> %s", url, followed)
            return followed

    logger.info("Could not resolve redirect URL; keeping original: %s", url)
    return url


def resolve_articles(articles: list[dict], *, allow_network: bool = True,
                     session: object | None = None) -> list[dict]:
    """Rewrite redirect URLs to publisher URLs in a batch of article dicts.

    Only articles whose ``url`` is a known redirect host are touched; the resolved
    publisher URL replaces ``url`` **in place** so it becomes the canonical key
    for full-text fetch and both dedup layers. Returns the same list for
    convenience. Never raises.
    """
    resolved = 0
    for article in articles:
        url = article.get("url")
        if not is_redirect_url(url):
            continue
        new_url = resolve_redirect(url, allow_network=allow_network, session=session)
        if new_url and new_url != url:
            article["url"] = new_url
            resolved += 1
    if resolved:
        logger.info(
            "Redirect resolution: rewrote %d/%d article URL(s) to publisher URLs.",
            resolved,
            len(articles),
        )
    return articles


def _main() -> int:
    setup_logging()
    if len(sys.argv) < 2:
        print("usage: python -m sentinel.collect.resolve <redirect-url>")
        return 2
    url = sys.argv[1]
    resolved = resolve_redirect(url)
    print(f"\noriginal : {url}\nresolved : {resolved}")
    return 0 if resolved != url else 1


if __name__ == "__main__":
    raise SystemExit(_main())
