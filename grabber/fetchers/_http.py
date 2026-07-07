"""Shared HTTP helper for the API-based fetchers (discord, slack, hn, bluesky).

They all hit a JSON API where a 429 should skip the source for this run (its items are
re-listed next run) rather than crash it. This centralizes that contract so the four
fetchers don't each copy-paste the same rate-limit block.
"""

import logging

import httpx

log = logging.getLogger(__name__)


def get_json(
    client: httpx.Client,
    url: str,
    source_name: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
):
    """GET `url` and return parsed JSON, or None on HTTP 429 (rate limited, skip this run).

    Any other non-2xx raises (transient failures are left unmarked so they retry next run).
    """
    resp = client.get(url, params=params, headers=headers)
    if resp.status_code == 429:
        log.warning("%s: rate limited, skipping this run", source_name)
        return None
    resp.raise_for_status()
    return resp.json()
