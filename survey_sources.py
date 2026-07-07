"""One-off source survey: score every enabled source by its LLM relevance.

Fetches each source's current feed (best-effort ~3-month window: items published within
--days are kept, undated items are kept), enriches each item with its full article text
like the production pipeline, scores every item with LLM.classify_batch (0-10 relevance to
video engineering), aggregates per source, and writes source_scores.csv / .json.

Fetched full-page article text is cached in survey_cache.sqlite (keyed by normalized URL,
reusing State's content cache), so re-runs skip re-downloading pages. Touches neither the
production state.sqlite nor the Telegram bot; no sends. Safe to re-run.

Usage: .venv/bin/python survey_sources.py [--days 90] [--limit-per-source N] [--snippets]
"""

import argparse
import csv
import json
import logging
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from grabber.config import CONTENT_FETCH_SKIP_TYPES, ROOT, USER_AGENT, load_config
from grabber.content import fetch_content
from grabber.dedup import normalize_url
from grabber.fetchers import PREFILTER_EXEMPT_TYPES
from grabber.prefilter import match as prefilter_match
from grabber.__main__ import _proxied_client, fetch_all
from grabber.llm import LLM
from grabber.state import State

log = logging.getLogger("survey")

# dedicated content cache — reuses State's get_content/set_content, kept separate from the
# production state.sqlite so we never disturb the cron's dedup/prune bookkeeping
CACHE_DB = ROOT / "survey_cache.sqlite"


def _within_window(item, cutoff: datetime) -> bool:
    """Keep undated items (many feeds omit dates) and anything published on/after cutoff."""
    if item.published is None:
        return True
    published = item.published
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published >= cutoff


def _enrich(items, sources_by_name, cfg, cache: State) -> None:
    """Fill item.content with full-page text (in place), like the real pipeline. Skips chat
    sources (their URL is the post) and sources with fetch_content: false. Cached extracted
    text (keyed by normalized URL) is reused across runs; only misses are fetched. Best-effort:
    fetch_content returns None on failure and the item falls back to its feed snippet.

    DB access stays on the main thread (sqlite3 connections aren't shareable across threads);
    only the network fetches run in the pool."""
    targets = []
    for it in items:
        src = sources_by_name.get(it.source)
        if src is None or not src.fetch_content or src.type in CONTENT_FETCH_SKIP_TYPES:
            continue
        targets.append(it)
    if not targets:
        return

    # cache lookup (main thread); items sharing a normalized URL reuse one fetch
    misses = []
    hits = 0
    for it in targets:
        key = normalize_url(it.url)
        cached = cache.get_content(key)
        if cached:
            it.content = cached
            hits += 1
        else:
            misses.append(it)
    log.info("enrich: %d items, %d cache hits, fetching %d", len(targets), hits, len(misses))

    with httpx.Client(timeout=20) as direct, _proxied_client(cfg) as proxied:
        def work(it):
            src = sources_by_name.get(it.source)
            client = proxied if (src and src.proxy) else direct
            if client is None:
                client = direct
            return it, fetch_content(it.url, client)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for it, md in pool.map(work, misses):
                if md:
                    it.content = md
                    cache.set_content(normalize_url(it.url), md)  # main thread — safe


CLS_ERROR = {"relevant": None, "score": None, "category": "error", "reason": "classification failed"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90, help="keep items published within N days (default 90)")
    ap.add_argument("--limit-per-source", type=int, default=None, help="cap newest N items per source")
    ap.add_argument("--snippets", action="store_true", help="skip full-page enrichment (fast, less accurate)")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.llm_configured:
        sys.exit("LLM is not configured (LLM_API_KEY / LLM_MODEL in .env)")
    sources_by_name = {s.name: s for s in cfg.sources}
    log.info("surveying %d enabled sources, window=%d days", len(cfg.sources), args.days)

    # 1. fetch every source concurrently (each Item is tagged with .source)
    all_items = fetch_all(cfg)

    # 2. group by source, date-filter, and cap
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    by_source: dict[str, list] = defaultdict(list)
    for it in all_items:
        if _within_window(it, cutoff):
            by_source[it.source].append(it)
    if args.limit_per_source:
        for name, items in by_source.items():
            items.sort(
                key=lambda it: it.published or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            by_source[name] = items[: args.limit_per_source]

    items = [it for group in by_source.values() for it in group]
    log.info("in-window items to score: %d across %d sources", len(items), len(by_source))

    # 3. enrich with full-page content (unless --snippets), caching fetches across runs
    cache = State(CACHE_DB)
    if not args.snippets:
        _enrich(items, sources_by_name, cfg, cache)
    cache.close()

    # 4. score in batches
    llm = LLM(cfg)
    records = []
    batch_size = max(1, cfg.classify_batch_size)
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        for offset, (item, cls) in enumerate(zip(batch, llm.classify_batch(batch)), 1):
            cls = cls or CLS_ERROR
            src = sources_by_name.get(item.source)
            exempt = bool(src and src.type in PREFILTER_EXEMPT_TYPES)
            keyword = prefilter_match(item.title, item.text)
            log.info(
                "[%d/%d] %s score=%s pf=%s %s",
                start + offset, len(items), item.source, cls["score"],
                keyword or ("exempt" if exempt else "MISS"), (cls["reason"] or "")[:60],
            )
            records.append(
                {
                    "source": item.source,
                    "item_id": item.item_id,
                    "url": item.url,
                    "title": item.title,
                    "published": item.published.isoformat() if item.published else None,
                    "score": cls["score"],
                    "category": cls["category"],
                    "reason": cls["reason"],
                    # would this item survive the production RSS keyword gate? (chat sources
                    # bypass it, so exempt=True there). Lets us measure prefilter false negatives.
                    "prefilter_keyword": keyword,
                    "prefilter_exempt": exempt,
                }
            )
    log.info("LLM usage — %s", llm.usage_summary())

    # 5. aggregate per source (every enabled source is listed, even if it scored nothing)
    scores_by_source: dict[str, list[int]] = defaultdict(list)
    for r in records:
        if r["score"] is not None:
            scores_by_source[r["source"]].append(r["score"])

    rows = []
    for name, src in sources_by_name.items():
        scores = scores_by_source.get(name, [])
        rows.append(
            {
                "source": name,
                "type": src.type,
                "url": src.url,
                "items_scored": len(scores),
                "avg_score": round(statistics.mean(scores), 2) if scores else None,
                "median_score": statistics.median(scores) if scores else None,
                "max_score": max(scores) if scores else None,
            }
        )
    # scored sources first (by avg desc), unscored sources last (alpha)
    rows.sort(key=lambda r: (r["avg_score"] is None, -(r["avg_score"] or 0), r["source"]))

    with open("source_scores.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["source", "type", "url", "items_scored", "avg_score", "median_score", "max_score"]
        )
        writer.writeheader()
        writer.writerows(rows)
    with open("source_scores.json", "w") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)

    # prefilter false-negative check: high-scoring items the production keyword gate would
    # drop before the LLM (non-exempt sources only) — directly informs keep/tune/remove
    thr = cfg.relevance_threshold
    gated = [
        r for r in records
        if r["score"] is not None and r["score"] >= thr
        and not r["prefilter_exempt"] and not r["prefilter_keyword"]
    ]
    total_gateable = [r for r in records if not r["prefilter_exempt"] and r["score"] is not None]
    log.info(
        "prefilter false negatives: %d of %d gate-eligible items score >= %d but MISS the keyword gate",
        len(gated), len(total_gateable), thr,
    )
    for r in sorted(gated, key=lambda r: -r["score"])[:20]:
        log.info("  MISS score=%d %s — %s", r["score"], r["source"], r["title"][:70])

    scored = [r for r in rows if r["items_scored"]]
    print(f"\nwrote source_scores.csv ({len(rows)} sources, {len(scored)} scored) and source_scores.json")
    print(f"prefilter would drop {len(gated)}/{len(total_gateable)} gate-eligible items scoring >= {thr}\n")
    print(f"{'source':32} {'type':9} {'n':>4} {'avg':>5} {'med':>5} {'max':>4}")
    for r in rows[:25]:
        avg = f"{r['avg_score']:.2f}" if r["avg_score"] is not None else "—"
        med = f"{r['median_score']:.1f}" if r["median_score"] is not None else "—"
        mx = r["max_score"] if r["max_score"] is not None else "—"
        print(f"{r['source']:32} {r['type']:9} {r['items_scored']:>4} {avg:>5} {med:>5} {str(mx):>4}")


if __name__ == "__main__":
    main()
