import logging
from datetime import datetime

import httpx

from . import Item

log = logging.getLogger(__name__)

APPVIEW = "https://public.api.bsky.app/xrpc"


def _post_to_item(source_name: str, post: dict) -> Item | None:
    record = post.get("record", {})
    text = record.get("text", "")
    if not text:
        return None
    handle = post.get("author", {}).get("handle", "")
    rkey = post.get("uri", "").rsplit("/", 1)[-1]

    published = None
    if record.get("createdAt"):
        try:
            published = datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00"))
        except ValueError:
            pass

    image_url = None
    images = post.get("embed", {}).get("images") or record.get("embed", {}).get("images")
    if images:
        image_url = images[0].get("fullsize") or images[0].get("thumb")

    return Item(
        source=source_name,
        item_id=post["uri"],
        url=f"https://bsky.app/profile/{handle}/post/{rkey}",
        title=text.split("\n", 1)[0][:120],
        text=text[:3000],
        published=published,
        image_url=image_url,
    )


def fetch_bluesky(src, client: httpx.Client) -> list[Item]:
    """Read Bluesky posts (no auth). src.url = handle, or 'search:<query>'."""
    if src.url.startswith("search:"):
        resp = client.get(
            f"{APPVIEW}/app.bsky.feed.searchPosts",
            params={"q": src.url[len("search:"):], "limit": 50, "sort": "latest"},
        )
    else:
        resp = client.get(
            f"{APPVIEW}/app.bsky.feed.getAuthorFeed",
            params={"actor": src.url, "limit": 50, "filter": "posts_no_replies"},
        )
    if resp.status_code == 429:
        log.warning("%s: rate limited, skipping this run", src.name)
        return []
    resp.raise_for_status()
    data = resp.json()

    items = []
    if "posts" in data:  # searchPosts
        posts = data["posts"]
    else:  # getAuthorFeed: skip reposts (they carry a "reason")
        posts = [entry["post"] for entry in data.get("feed", []) if not entry.get("reason")]
    for post in posts:
        item = _post_to_item(src.name, post)
        if item:
            items.append(item)
    return items
