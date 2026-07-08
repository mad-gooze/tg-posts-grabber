from dataclasses import dataclass
from datetime import datetime


@dataclass
class Item:
    source: str
    item_id: str
    url: str
    title: str
    text: str
    published: datetime | None = None
    image_url: str | None = None
    # full article text (markdown), fetched lazily by the pipeline for items about to hit
    # the LLM; empty when fetching is disabled/failed. Downstream prefers it over `text`.
    content: str = ""
    # cross-source dedup key, filled by the pipeline after prefilter (see grabber/dedup.py)
    norm_url: str = ""
    simhash: int = 0


# Submodules do `from . import Item`, so these imports must come after the dataclass.
from .bluesky import fetch_bluesky  # noqa: E402
from .discord import fetch_discord  # noqa: E402
from .hn import fetch_hn  # noqa: E402
from .html import fetch_html  # noqa: E402
from .linkedin import fetch_linkedin  # noqa: E402
from .rss import fetch_rss  # noqa: E402
from .slack import fetch_slack  # noqa: E402
from .telegram_web import fetch_telegram  # noqa: E402

# type -> fetcher(src, client) -> list[Item]. The two legacy fetchers keep their
# (name, url, client) signature (analyze_channel.py depends on it) and are adapted here.
# Reddit intentionally has no fetcher: its JSON API blocks unauthenticated bots, so reddit
# sources use type: rss with the subreddit's /.rss feed.
FETCHERS = {
    "rss": lambda src, client: fetch_rss(src.name, src.url, client),
    "html": lambda src, client: fetch_html(
        src.name, src.url, client, post_pattern=src.post_pattern or None, post_exclude=src.post_exclude or None
    ),
    "telegram": lambda src, client: fetch_telegram(src.name, src.url, client),
    "linkedin": fetch_linkedin,
    "slack": fetch_slack,
    "discord": fetch_discord,
    "hn": fetch_hn,
    "bluesky": fetch_bluesky,
}

# curated chat sources bypass the video-keyword prefilter (like hand-picked telegram channels)
PREFILTER_EXEMPT_TYPES = {"telegram", "slack", "discord"}
