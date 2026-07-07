import logging
from datetime import datetime

import httpx

from . import Item
from ._http import get_json
from .rss import _strip_html

log = logging.getLogger(__name__)


def fetch_hn(src, client: httpx.Client) -> list[Item]:
    """Search Hacker News stories (newest first) via the Algolia API. src.url = query string."""
    data = get_json(
        client,
        "https://hn.algolia.com/api/v1/search_by_date",
        src.name,
        params={"query": src.url, "tags": "story", "hitsPerPage": 50},
    )
    if data is None:
        return []

    items = []
    for hit in data.get("hits", []):
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
