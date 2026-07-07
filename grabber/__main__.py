import argparse
import html
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from .config import Config, Source, STATE_DB, USER_AGENT, load_config
from .fetchers import FETCHERS, PREFILTER_EXEMPT_TYPES, Item
from .llm import LLM
from .notify import Notifier
from .prefilter import match as prefilter_match
from .state import State

log = logging.getLogger("grabber")


@contextmanager
def _proxied_client(cfg: Config):
    """Client tunneled through SOCKS5_PROXY, or None when no proxy is configured."""
    if not cfg.socks5_proxy:
        yield None
        return
    with httpx.Client(timeout=20, proxy=cfg.socks5_proxy) as client:
        yield client


def _fetch(fn, src: Source, direct: httpx.Client, proxied: httpx.Client | None) -> list[Item]:
    """Blocked sources (proxy: true) go straight through the SOCKS5 proxy; the rest go
    direct, with one proxied retry so a newly blocked source still comes through."""
    if src.proxy:
        if proxied is None:
            raise RuntimeError("source has proxy: true but SOCKS5_PROXY is not set in .env")
        return fn(src, proxied)
    try:
        return fn(src, direct)
    except Exception as e:
        if proxied is None:
            raise
        log.info("%s: direct fetch failed (%s), retrying via proxy — set proxy: true in sources.yaml if it is blocked", src.name, e)
        return fn(src, proxied)


def fetch_all(cfg: Config) -> list[Item]:
    items: list[Item] = []
    with httpx.Client(timeout=20) as direct, _proxied_client(cfg) as proxied:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for src in cfg.sources:
                fn = FETCHERS.get(src.type)
                if fn is None:
                    log.warning("%s: unknown source type %r, skipping", src.name, src.type)
                    continue
                futures[pool.submit(_fetch, fn, src, direct, proxied)] = src
            for future in as_completed(futures):
                src = futures[future]
                try:
                    fetched = future.result()
                    log.info("%s: %d items", src.name, len(fetched))
                    items.extend(fetched)
                except Exception as e:
                    log.warning("%s: fetch failed: %s", src.name, e)
    return items


def fetch_one(cfg: Config, name: str):
    """Fetch a single source by name and print its items — no state writes, no LLM."""
    src = next((s for s in cfg.sources if s.name == name), None)
    if src is None:
        sys.exit(f"no enabled source named {name!r} (check sources.yaml and its token)")
    fn = FETCHERS.get(src.type)
    if fn is None:
        sys.exit(f"unknown source type {src.type!r}")
    with httpx.Client(timeout=20) as direct, _proxied_client(cfg) as proxied:
        items = _fetch(fn, src, direct, proxied)
    log.info("%s: %d items", src.name, len(items))
    for item in items:
        print(f"\n{'=' * 70}\n[{item.published}] {item.title}\n{item.url}\n{item.text[:500]}")


def og_image(url: str, proxy: str | None = None) -> str | None:
    """Best-effort <meta property="og:image"> lookup on the article page."""
    try:
        with httpx.Client(timeout=15, proxy=proxy) as client:
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            resp.raise_for_status()
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', resp.text) or re.search(
            r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', resp.text
        )
        return html.unescape(m.group(1)) if m else None
    except Exception:
        return None


def format_message(draft: dict, item: Item, classification: dict) -> str:
    return (
        f"📝 <b>{html.escape(draft['title'])}</b>\n\n"
        f"{draft['text']}\n\n"
        f"🔗 <a href=\"{html.escape(item.url)}\">{html.escape(item.url)}</a>\n"
        f"#{classification['category']} · score {classification['score']} · {item.source}"
    )


def run(cfg: Config, init_only: bool, dry_run: bool, limit: int | None):
    state = State(STATE_DB)
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)

    # hand-curated chat sources (telegram/slack/discord) and prefilter:false sources
    # (single-topic feeds like GitHub releases, titles just "v4.2.0") bypass the keyword gate
    prefilter_exempt = {
        s.name for s in cfg.sources if s.type in PREFILTER_EXEMPT_TYPES or not s.prefilter
    }
    proxied_sources = {s.name for s in cfg.sources if s.proxy}

    all_items = fetch_all(cfg)
    new_items = []
    filtered = 0
    for item in all_items:
        if state.is_known(item.source, item.item_id):
            continue
        if init_only:
            state.mark(item.source, item.item_id, item.url, "seen")
            continue
        if item.published and item.published < cutoff:
            state.mark(item.source, item.item_id, item.url, "seen")
            continue
        if (
            cfg.prefilter_enabled
            and item.source not in prefilter_exempt
            and prefilter_match(item.title, item.text) is None
        ):
            state.mark(item.source, item.item_id, item.url, "filtered")
            filtered += 1
            log.info("filtered (no video keywords) [%s] %s", item.source, item.title[:80])
            continue
        new_items.append(item)

    if filtered:
        log.info("prefilter: skipped %d of %d new items without LLM calls", filtered, filtered + len(new_items))

    if init_only:
        log.info("init: marked %d items as seen, no LLM calls or sends", len(all_items))
        state.close()
        return

    new_items.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    cap = limit if limit is not None else cfg.max_llm_items_per_run
    if len(new_items) > cap:
        log.info("capping LLM processing to %d of %d new items (rest stay unseen for next run)", cap, len(new_items))
        new_items = new_items[:cap]

    if not new_items:
        log.info("no new items")
        state.close()
        return

    if not cfg.llm_configured:
        log.warning("LLM not configured (LLM_API_KEY/LLM_MODEL) — listing %d new items, not marking them", len(new_items))
        for item in new_items:
            print(f"[{item.source}] {item.title} — {item.url}")
        state.close()
        return

    llm = LLM(cfg)
    notifier = None
    if not dry_run:
        if not cfg.bot_configured:
            log.error("TG_BOT_TOKEN/TG_CHAT_ID not set; use --dry-run or fill .env")
            state.close()
            sys.exit(1)
        notifier = Notifier(cfg.tg_bot_token, cfg.tg_chat_id)

    drafted = rejected = 0
    batch_size = max(1, cfg.classify_batch_size)
    for start in range(0, len(new_items), batch_size):
        batch = new_items[start : start + batch_size]
        for item, cls in zip(batch, llm.classify_batch(batch)):
            if cls is None:
                # classify failed even per-item (already logged); item stays unmarked for the next run
                continue

            if not cls["relevant"] or cls["score"] < cfg.relevance_threshold:
                state.mark(item.source, item.item_id, item.url, "rejected", cls["score"])
                rejected += 1
                log.info("rejected (%d/10) [%s] %s — %s", cls["score"], item.source, item.title[:60], cls["reason"])
                continue

            try:
                draft = llm.draft(item, cls["category"])
            except Exception as e:
                log.warning("draft failed for %s (%s), leaving for next run", item.url, e)
                continue

            # article pages of blocked sources are blocked too — look up og:image via proxy
            image = item.image_url or og_image(
                item.url, cfg.socks5_proxy if item.source in proxied_sources else None
            )
            message = format_message(draft, item, cls)

            if dry_run:
                print(f"\n{'=' * 70}\nIMAGE: {image}\n{message}")
            else:
                try:
                    notifier.send_draft(message, image)
                except Exception as e:
                    log.error("send failed for %s (%s), leaving for next run", item.url, e)
                    continue
            state.mark(item.source, item.item_id, item.url, "drafted", cls["score"])
            drafted += 1

    log.info("done: %d drafted, %d rejected, %d prefiltered, %d sent to LLM", drafted, rejected, filtered, len(new_items))
    log.info("tokens: %s", llm.usage_summary())
    state.close()


def main():
    parser = argparse.ArgumentParser(prog="grabber", description="Fetch, filter and draft posts for the channel")
    parser.add_argument("--init", action="store_true", help="mark all current feed items as seen; no LLM, no sends")
    parser.add_argument("--dry-run", action="store_true", help="full pipeline, but print drafts instead of sending")
    parser.add_argument("--whoami", action="store_true", help="print chat IDs from the bot's recent updates")
    parser.add_argument("--fetch", metavar="NAME", help="fetch one source and print its items; no state, no LLM")
    parser.add_argument("--limit", type=int, default=None, help="override MAX_LLM_ITEMS_PER_RUN for this run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()

    if args.whoami:
        if not cfg.tg_bot_token:
            sys.exit("TG_BOT_TOKEN is not set in .env")
        Notifier(cfg.tg_bot_token, cfg.tg_chat_id).whoami()
        return

    if args.fetch:
        fetch_one(cfg, args.fetch)
        return

    run(cfg, init_only=args.init, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
