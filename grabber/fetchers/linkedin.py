"""LinkedIn Pulse/newsletter author fetcher (no auth).

LinkedIn has no public read API, but individual Pulse articles
(linkedin.com/pulse/<slug>) serve full HTML to an unauthenticated browser User-Agent, and
each article page embeds a "More from <author>" block linking that author's other recent
articles. So a source's `url` is a *seed* article URL and the fetcher re-derives the
author's recent article list from that page every run — a stable seed keeps surfacing new
posts (verified: an old seed page still lists the author's newest article).

A LinkedIn *newsletter* landing page (linkedin.com/newsletters/<slug>-<id>) works the same
way: its issues are `/pulse/` links, so a newsletter URL is also a valid seed (the landing
page itself is not an article and is excluded from the results).

Only Pulse/newsletter *authors* are followable. Profile/company activity feeds and
individual post pages are login-walled or carry no author-article list (they answer HTTP
999, LinkedIn's bot block, or link only to themselves), so ongoing post/update feeds are
not reachable without auth — the fetcher rejects such seed URLs.

Everything is best-effort, matching html.py/rss.py: a post that fails to fetch or parse is
logged and skipped rather than crashing the source. HTTP 999 (block) is treated like a
429 — skip this run, retry next — rather than raised. Fetches use the caller's httpx
client so SOCKS5 proxy routing is preserved for blocked sources.
"""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from ..content import extract_content
from . import Item

log = logging.getLogger(__name__)

# LinkedIn's "block" status for bot-detected requests; treat like 429 (skip this run)
BLOCKED_STATUS = 999

# author "more from" blocks list ~11 recent articles; cap so a source can't open its whole
# back-catalogue every run
MAX_POSTS = 15

# an individual Pulse article path; used to pick author-article links off the seed page
_PULSE_PATH_RE = re.compile(r"^/pulse/[^/]+/?$")
# valid seed URL paths: a Pulse article or a newsletter landing page (both list author
# articles). Post/profile/company URLs are rejected — those feeds are login-walled.
_SEED_PATH_RE = re.compile(r"^/(pulse|newsletters)/", re.IGNORECASE)
# ld+json publish timestamp, e.g. "2026-07-04T05:15:26.000+00:00"
_DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def _get(client: httpx.Client, url: str, source_name: str) -> httpx.Response | None:
    """GET `url`, or None on HTTP 999 (LinkedIn block — skip, retry next run).

    Any other non-2xx raises so the caller's best-effort handling / proxy retry applies.
    """
    resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    if resp.status_code == BLOCKED_STATUS:
        log.warning("%s: blocked (HTTP %d) on %s, skipping", source_name, BLOCKED_STATUS, url)
        return None
    resp.raise_for_status()
    return resp


def _discover_post_urls(seed_url: str, resp: httpx.Response) -> list[str]:
    """Same-origin Pulse article URLs linked from the seed page, newest-first, deduped.

    The "More from <author>" block lists that author's own recent articles; each is
    canonicalized to scheme://host/path (query/fragment dropped) so it's a stable item id
    across runs. When the seed itself is a Pulse article its own canonical URL is included;
    a newsletter landing-page seed is not an article, so only its discovered issue links
    are returned.
    """
    soup = BeautifulSoup(resp.text, "html.parser")
    base = str(resp.url)  # follow_redirects may change the base
    bp = urlparse(base)

    def canonical(u: str) -> str:
        p = urlparse(urljoin(base, u))
        return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"

    # include the seed itself only when it is a Pulse article (not a newsletter landing page)
    out: list[str] = [canonical(base)] if _PULSE_PATH_RE.match(bp.path) else []
    for a in soup.find_all("a", href=True):
        p = urlparse(urljoin(base, a["href"]))
        if p.netloc != bp.netloc or not _PULSE_PATH_RE.match(p.path):
            continue
        clean = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"
        if clean not in out:
            out.append(clean)
    return out


def _published(page: str, meta) -> datetime | None:
    """Publish date as tz-aware UTC: trafilatura's metadata date (ISO 'YYYY-MM-DD'),
    falling back to the ld+json datePublished timestamp. None when neither parses."""
    raw = getattr(meta, "date", None)
    if raw:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    m = _DATE_PUBLISHED_RE.search(page)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _title(meta, url: str) -> str:
    """Article title from trafilatura metadata; falls back to a humanised slug."""
    title = (getattr(meta, "title", None) or "").strip()
    if title:
        return title
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize() or url


def fetch_linkedin(src, client: httpx.Client) -> list[Item]:
    if not _SEED_PATH_RE.match(urlparse(src.url).path):
        log.warning(
            "%s: %s is not a Pulse/newsletter article URL — only authors are followable "
            "(post/profile/company feeds are login-walled); skipping",
            src.name, src.url,
        )
        return []
    seed = _get(client, src.url, src.name)
    if seed is None:  # blocked this run
        return []
    post_urls = _discover_post_urls(src.url, seed)

    items: list[Item] = []
    for post_url in post_urls[:MAX_POSTS]:
        try:
            resp = _get(client, post_url, src.name)
            if resp is None:  # this post blocked; others may still come through
                continue
            page = resp.text
            md = extract_content(page)
            meta = trafilatura.extract_metadata(page)
            items.append(
                Item(
                    source=src.name,
                    item_id=post_url,
                    url=post_url,
                    title=_title(meta, post_url),
                    text=(md or "")[:3000],
                    # keep the full extraction so the enrich phase reuses it instead of
                    # re-downloading this same page (extract_content already capped it)
                    content=md or "",
                    published=_published(page, meta),
                    image_url=getattr(meta, "image", None),
                )
            )
        except Exception as e:  # best-effort: one bad post must not sink the source
            log.info("%s: skipping %s (%s)", src.name, post_url, e)
    return items
