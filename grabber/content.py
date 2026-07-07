"""Best-effort full-page article extraction.

The feed snippet (item.text) is often a one-line teaser, which makes both relevance
scoring and drafting weak. This module opens the article URL and extracts its main
readable text as markdown with trafilatura, dropping nav/ads/boilerplate.

We fetch the HTML ourselves with the caller's httpx client (so SOCKS5 proxy routing is
preserved for blocked sources) and hand the raw HTML to trafilatura, rather than letting
trafilatura fetch — its downloader would bypass the proxy. Everything is best-effort:
any failure returns None and the pipeline falls back to the feed snippet (see og_image
in __main__.py for the same resilience contract).
"""

import logging

import httpx
import trafilatura

from .config import USER_AGENT

log = logging.getLogger(__name__)

# hard cap on stored/extracted text so a pathological page can't bloat the DB or the
# LLM prompt; the per-call slices in llm.py are smaller still
MAX_CONTENT_CHARS = 20000


def extract_content(html: str) -> str | None:
    """Extract the main article text from an HTML string as markdown, or None."""
    if not html:
        return None
    md = trafilatura.extract(
        html,
        output_format="markdown",
        favor_recall=True,
        include_comments=False,
        include_tables=True,
    )
    if not md or not md.strip():
        return None
    return md.strip()[:MAX_CONTENT_CHARS]


def fetch_content(url: str, client: httpx.Client) -> str | None:
    """Fetch `url` and return its extracted markdown, or None on any failure.

    Uses the passed client (direct or proxied) so blocked sources still resolve.
    """
    if not url:
        return None
    try:
        resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        resp.raise_for_status()
        return extract_content(resp.text)
    except Exception as e:
        log.info("content fetch failed for %s (%s), falling back to snippet", url, e)
        return None
