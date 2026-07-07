import argparse
import html
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx

from .config import Config, STATE_DB, USER_AGENT, load_config
from .fetchers import Item
from .fetchers.rss import fetch_rss
from .fetchers.telegram_web import fetch_telegram
from .llm import LLM
from .notify import Notifier
from .state import State

log = logging.getLogger("grabber")


def fetch_all(cfg: Config) -> list[Item]:
    items: list[Item] = []
    with httpx.Client(timeout=20) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for src in cfg.sources:
                fn = fetch_rss if src.type == "rss" else fetch_telegram
                futures[pool.submit(fn, src.name, src.url, client)] = src
            for future in as_completed(futures):
                src = futures[future]
                try:
                    fetched = future.result()
                    log.info("%s: %d items", src.name, len(fetched))
                    items.extend(fetched)
                except Exception as e:
                    log.warning("%s: fetch failed: %s", src.name, e)
    return items


def og_image(url: str) -> str | None:
    """Best-effort <meta property="og:image"> lookup on the article page."""
    try:
        with httpx.Client(timeout=15) as client:
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

    all_items = fetch_all(cfg)
    new_items = []
    for item in all_items:
        if state.is_known(item.source, item.item_id):
            continue
        if init_only:
            state.mark(item.source, item.item_id, item.url, "seen")
            continue
        if item.published and item.published < cutoff:
            state.mark(item.source, item.item_id, item.url, "seen")
            continue
        new_items.append(item)

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
    for item in new_items:
        try:
            cls = llm.classify(item)
        except Exception as e:
            log.warning("classify failed for %s (%s), leaving for next run", item.url, e)
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

        image = item.image_url or og_image(item.url)
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

    log.info("done: %d drafted, %d rejected, %d new total", drafted, rejected, len(new_items))
    state.close()


def main():
    parser = argparse.ArgumentParser(prog="grabber", description="Fetch, filter and draft posts for the channel")
    parser.add_argument("--init", action="store_true", help="mark all current feed items as seen; no LLM, no sends")
    parser.add_argument("--dry-run", action="store_true", help="full pipeline, but print drafts instead of sending")
    parser.add_argument("--whoami", action="store_true", help="print chat IDs from the bot's recent updates")
    parser.add_argument("--limit", type=int, default=None, help="override MAX_LLM_ITEMS_PER_RUN for this run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()

    if args.whoami:
        if not cfg.tg_bot_token:
            sys.exit("TG_BOT_TOKEN is not set in .env")
        Notifier(cfg.tg_bot_token, cfg.tg_chat_id).whoami()
        return

    run(cfg, init_only=args.init, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
