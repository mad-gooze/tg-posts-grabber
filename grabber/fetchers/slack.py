import logging
import re
from datetime import datetime, timezone

import httpx

from ..config import Source
from . import Item
from ._http import get_json

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[UW]\w+>")
LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")


def _clean(text: str) -> str:
    """Normalize Slack mrkdwn: strip user mentions, unwrap <url|label> to the url."""
    text = MENTION_RE.sub("@user", text)
    text = LINK_RE.sub(r"\1", text)
    return text.strip()


def fetch_slack(src: Source, client: httpx.Client) -> list[Item]:
    """Read the latest messages of one channel via conversations.history.

    src.url = "<workspace-slug>/<channel_id>" (the slug builds permalinks locally).
    Needs a user token (xoxp-) with channels:history / groups:history.
    """
    workspace, _, channel = src.url.partition("/")
    if not channel:
        raise ValueError(f"slack url must be '<workspace-slug>/<channel_id>', got {src.url!r}")

    data = get_json(
        client,
        "https://slack.com/api/conversations.history",
        src.name,
        params={"channel": channel, "limit": 50},
        headers={"Authorization": f"Bearer {src.token}"},
    )
    if data is None:
        return []
    if not data.get("ok"):
        raise ValueError(data.get("error", "unknown slack error"))

    items = []
    for msg in data.get("messages", []):
        if msg.get("subtype"):  # joins, bot posts, channel events
            continue
        text = _clean(msg.get("text", ""))
        if not text:
            continue
        ts = msg["ts"]
        items.append(
            Item(
                source=src.name,
                item_id=f"{channel}/{ts}",
                url=f"https://{workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}",
                title=text.split("\n", 1)[0][:120],
                text=text[:3000],
                published=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            )
        )
    return items
