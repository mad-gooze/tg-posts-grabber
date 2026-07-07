import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from . import Item

log = logging.getLogger(__name__)

BG_IMAGE_RE = re.compile(r"background-image:\s*url\('([^']+)'\)")


def fetch_telegram(source_name: str, channel: str, client: httpx.Client) -> list[Item]:
    """Scrape the public preview page https://t.me/s/<channel> (last ~20 messages)."""
    resp = client.get(
        f"https://t.me/s/{channel}",
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
            )
        )
    return items
