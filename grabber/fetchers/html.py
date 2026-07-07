"""Generic HTML-blog fetcher for sites with no native RSS/Atom feed.

Many good blogs (static Next.js sites in particular) ship no feed at all — every
feed path 404s and dates live only in visible byline text. This fetcher turns such
a blog's index page into RSS-like Items: it scrapes the index for post links, then
opens each post and extracts title/date/text/image with trafilatura (reusing
content.extract_content for the body).

Everything is best-effort, matching the rss fetcher and content.py: a post that
fails to fetch or parse is logged and skipped rather than crashing the source.
Fetches use the caller's httpx client so SOCKS5 proxy routing is preserved.
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

# newest-first index → the first N links are the freshest posts; cap so a single
# source can't open its whole back-catalogue every run
DEFAULT_MAX_POSTS = 15

# path segments that are listing/pagination pages, not individual posts
SKIP_SEGMENTS = {"page", "tag", "tags", "category", "categories", "author", "authors", "feed", "rss"}
# a link ending in one of these is a static asset, not an article
ASSET_EXTS = (".xml", ".json", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff2", ".ico")

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# "Jun 10, 2026", "June 10 2026", "Jun. 10, 2026" — a byline date near the top of a post
BYLINE_RE = re.compile(rf"\b({_MONTHS})\w*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b")
_MONTH_NUM = {m: i for i, m in enumerate(_MONTHS.split("|"), start=1)}


def _discover_post_urls(
    base: str,
    client: httpx.Client,
    post_pattern: str | None = None,
    post_exclude: str | None = None,
) -> list[str]:
    """Scrape the blog index at `base` for individual post URLs, newest-first.

    Default mode keeps same-origin links whose path is exactly one segment below the
    blog path (e.g. /blog/<slug>), dropping pagination/listing segments and assets.

    When `post_pattern` is set (a regex matched against the URL path), that heuristic
    is replaced: a same-origin link is a post iff its path matches `post_pattern` and,
    when given, does NOT match `post_exclude`. This handles blogs whose posts don't
    sit one segment below the index (e.g. root-level slugs, /media/article/<slug>).

    Order is preserved (blog indexes list newest first) and duplicates removed.
    """
    resp = client.get(base, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    inc = re.compile(post_pattern) if post_pattern else None
    exc = re.compile(post_exclude) if post_exclude else None
    bp = urlparse(str(resp.url))  # follow_redirects may change the base
    prefix = bp.path.rstrip("/") + "/"
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        p = urlparse(urljoin(str(resp.url), a["href"]))
        if p.netloc != bp.netloc or p.path.lower().endswith(ASSET_EXTS):
            continue
        if inc is not None:  # regex mode: match the whole path against the pattern
            if not inc.search(p.path) or (exc is not None and exc.search(p.path)):
                continue
        else:  # default: exactly one segment below the blog path
            if not p.path.startswith(prefix):
                continue
            rest = p.path[len(prefix):].strip("/")
            if not rest or "/" in rest:  # empty (the index itself) or nested (not a leaf post)
                continue
            if rest.split(".")[0] in SKIP_SEGMENTS:
                continue
        clean = f"{p.scheme}://{p.netloc}{p.path}"
        if clean not in out:
            out.append(clean)
    return out


def _byline_date(text: str | None) -> datetime | None:
    """Parse a 'Mon D, YYYY' byline in the first line(s) of the extracted text.

    Restricted to the head of the article so a date mentioned in the body can't be
    mistaken for the publish date. Returns a tz-aware UTC datetime, or None.
    """
    if not text:
        return None
    m = BYLINE_RE.search(text[:300])
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), _MONTH_NUM[m.group(1)], int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _meta_date(meta) -> datetime | None:
    """trafilatura's extracted date (ISO 'YYYY-MM-DD') as tz-aware UTC, or None."""
    raw = getattr(meta, "date", None)
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _title(meta, url: str) -> str:
    """Post title from trafilatura metadata, stripping a trailing ' | Site name'
    suffix; falls back to a humanised slug when no title was extracted."""
    title = (getattr(meta, "title", None) or "").strip()
    site = (getattr(meta, "sitename", None) or "").strip()
    if title:
        # drop a trailing " | Site name" segment (the site name may be a substring of it,
        # e.g. title "... | Fishjam blog" with sitename "Fishjam")
        head, sep, tail = title.rpartition(" | ")
        if sep and site and site.lower() in tail.lower():
            title = head.strip()
        return title
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize() or url


def fetch_html(
    source_name: str,
    url: str,
    client: httpx.Client,
    max_posts: int = DEFAULT_MAX_POSTS,
    post_pattern: str | None = None,
    post_exclude: str | None = None,
) -> list[Item]:
    post_urls = _discover_post_urls(url, client, post_pattern, post_exclude)
    if not post_urls:
        raise ValueError(f"no post links found on {url} (index layout may have changed)")

    items: list[Item] = []
    for post_url in post_urls[:max_posts]:
        try:
            resp = client.get(post_url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            resp.raise_for_status()
            page = resp.text
            md = extract_content(page)
            meta = trafilatura.extract_metadata(page)
            items.append(
                Item(
                    source=source_name,
                    item_id=post_url,
                    url=post_url,
                    title=_title(meta, post_url),
                    text=(md or "")[:3000],
                    published=_byline_date(md) or _meta_date(meta),
                    image_url=getattr(meta, "image", None),
                )
            )
        except Exception as e:  # best-effort: one bad post must not sink the source
            log.info("%s: skipping %s (%s)", source_name, post_url, e)
    return items
