import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import USER_AGENT
from . import Item

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).strip()


def _entry_image(entry) -> str | None:
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/") and enc.get("href"):
            return enc["href"]
    for media in entry.get("media_content", []) or entry.get("media_thumbnail", []):
        url = media.get("url", "")
        if url:
            return url
    # first <img> in the summary/content HTML
    body = entry.get("summary", "")
    if entry.get("content"):
        body = entry["content"][0].get("value", "") or body
    m = re.search(r'<img[^>]+src="([^"]+)"', body)
    return m.group(1) if m else None


def _entry_published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def fetch_rss(source_name: str, url: str, client: httpx.Client) -> list[Item]:
    resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception}")

    items = []
    for entry in feed.entries:
        link = entry.get("link", "")
        item_id = entry.get("id") or link
        if not item_id:
            continue
        body = entry.get("summary", "")
        if entry.get("content"):
            body = entry["content"][0].get("value", "") or body
        items.append(
            Item(
                source=source_name,
                item_id=item_id,
                url=link,
                title=_strip_html(entry.get("title", "")),
                text=_strip_html(body)[:3000],
                published=_entry_published(entry),
                image_url=_entry_image(entry),
            )
        )
    return items
