import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from . import Item

log = logging.getLogger(__name__)

BG_IMAGE_RE = re.compile(r"background-image:\s*url\('([^']+)'\)")
# a public, single-message t.me URL: https://t.me/<channel>/<id>. Excludes /s/ (the
# preview listing) and /c/ (private channels, not publicly linkable).
POST_URL_RE = re.compile(r"^https?://t\.me/(?!s/|c/)[A-Za-z0-9_]+/\d+")


def _forwarded_from(msg) -> str | None:
    """The t.me post URL a message widget was forwarded from, or None when it's original.
    Telegram links the forward header to the source post; a channel-only link (no post id,
    e.g. a private source) is dropped since there is no single original post to point at."""
    a = msg.select_one("a.tgme_widget_message_forwarded_from_name[href]")
    if not a:
        return None
    href = a["href"].split("?", 1)[0]
    return href if POST_URL_RE.match(href) else None


def resolve_original(start_url: str, client: httpx.Client, max_hops: int = 5) -> str:
    """Walk a forward chain from `start_url` (a post's immediate forward source) to the
    deepest publicly-linkable original post, returning its URL.

    `start_url` is already known from the listing page, so this only fetches hops 2..N:
    each hop opens the post's embed page and reads *its* forward header. Stops at the first
    original post, a private/unlinkable hop, a cycle, or `max_hops`. Never raises — a failed
    hop just returns the deepest URL reached so far (at worst `start_url` itself)."""
    original = current = start_url
    seen = {start_url}
    for _ in range(max_hops):
        try:
            resp = client.get(
                current + ("&" if "?" in current else "?") + "embed=1",
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            nxt = _forwarded_from(BeautifulSoup(resp.text, "html.parser"))
        except Exception as e:
            log.info("forward-chain lookup failed at %s (%s)", current, e)
            break
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        original = current = nxt
    return original


def fetch_telegram(
    source_name: str, channel: str, client: httpx.Client, before: int | None = None
) -> list[Item]:
    """Scrape the public preview page https://t.me/s/<channel> (last ~20 messages).

    `before` pages backwards: returns the ~20 messages preceding that post ID.
    """
    resp = client.get(
        f"https://t.me/s/{channel}" + (f"?before={before}" if before else ""),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    messages = soup.select("div.tgme_widget_message[data-post]")
    if not messages and "tgme_channel_history" not in resp.text:
        raise ValueError("no message markup found — channel private, renamed, or page layout changed")

    items = []
    for msg in messages:
        post_id = msg["data-post"]  # "channel/123"
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        if not text:
            continue  # media-only or service message: nothing to classify

        image_url = None
        photo = msg.select_one("a.tgme_widget_message_photo_wrap[style]")
        if photo:
            m = BG_IMAGE_RE.search(photo["style"])
            if m:
                image_url = m.group(1)

        published = None
        time_el = msg.select_one("time[datetime]")
        if time_el:
            try:
                published = datetime.fromisoformat(time_el["datetime"])
            except ValueError:
                pass

        first_line = text.split("\n", 1)[0]
        items.append(
            Item(
                source=source_name,
                item_id=post_id,
                url=f"https://t.me/{post_id}",
                title=first_line[:120],
                text=text[:3000],
                published=published,
                image_url=image_url,
                forwarded_from_url=_forwarded_from(msg) or "",
            )
        )
    return items
