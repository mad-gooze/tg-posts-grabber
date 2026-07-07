import logging
from datetime import datetime

import httpx

from ..config import Source
from . import Item

log = logging.getLogger(__name__)


def fetch_discord(src: Source, client: httpx.Client) -> list[Item]:
    """Read the latest messages of one channel.

    src.url = "<guild_id>/<channel_id>" (guild id builds permalinks).
    Uses a plain user token (self-bot) — Authorization header has NO 'Bot ' prefix.
    """
    guild, _, channel = src.url.partition("/")
    if not channel:
        raise ValueError(f"discord url must be '<guild_id>/<channel_id>', got {src.url!r}")

    resp = client.get(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        params={"limit": 50},
        headers={"Authorization": src.token},
    )
    if resp.status_code == 429:
        log.warning("%s: rate limited, skipping this run", src.name)
        return []
    resp.raise_for_status()

    items = []
    for msg in resp.json():
        text = msg.get("content", "").strip()
        if not text:
            continue  # embed/attachment-only message: nothing to classify
        msg_id = msg["id"]

        image_url = None
        for att in msg.get("attachments", []):
            if (att.get("content_type") or "").startswith("image/"):
                image_url = att.get("url")
                break

        published = None
        if msg.get("timestamp"):
            try:
                published = datetime.fromisoformat(msg["timestamp"])
            except ValueError:
                pass

        items.append(
            Item(
                source=src.name,
                item_id=f"{channel}/{msg_id}",
                url=f"https://discord.com/channels/{guild}/{channel}/{msg_id}",
                title=text.split("\n", 1)[0][:120],
                text=text[:3000],
                published=published,
                image_url=image_url,
            )
        )
    return items
