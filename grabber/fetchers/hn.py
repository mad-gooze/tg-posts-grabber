import logging
from datetime import datetime

import httpx

from . import Item
from .rss import _strip_html

log = logging.getLogger(__name__)


def fetch_hn(src, client: httpx.Client) -> list[Item]:
    """Search Hacker News stories (newest first) via the Algolia API. src.url = query string."""
    resp = client.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": src.url, "tags": "story", "hitsPerPage": 50},
    )
    if resp.status_code == 429:
        log.warning("%s: rate limited, skipping this run", src.name)
        return []
    resp.raise_for_status()

    items = []
    for hit in resp.json().get("hits", []):
        object_id = hit.get("objectID")
        if not object_id:
            continue
        published = None
        if hit.get("created_at"):
            try:
                published = datetime.fromisoformat(hit["created_at"])
            except ValueError:
                pass
        items.append(
            Item(
                source=src.name,
                item_id=object_id,
                url=hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
                title=hit.get("title", ""),
                text=_strip_html(hit.get("story_text") or "")[:3000],
                published=published,
            )
        )
    return items
