"""One-off self-audit: run every post of a Telegram channel through the pipeline's gates.

Fetches the channel's full history via the public t.me/s preview (paginated),
checks each post against the keyword prefilter (which telegram sources normally
bypass) and scores every post with LLM.classify — including prefilter misses,
so the two gates can be compared. Touches neither state.sqlite nor the bot.

Usage: .venv/bin/python analyze_channel.py [channel] [--limit N]
"""

import argparse
import json
import logging
import sys
import time

import httpx

from grabber import prefilter
from grabber.config import USER_AGENT, load_config
from grabber.fetchers import Item
from grabber.fetchers.telegram_web import fetch_telegram
from grabber.llm import LLM

log = logging.getLogger("analyze")


def fetch_all_posts(channel: str, client: httpx.Client) -> list[Item]:
    items: dict[str, Item] = {}
    before: int | None = None
    while True:
        page = fetch_telegram(channel, channel, client, before=before)
        new = [it for it in page if it.item_id not in items]
        for it in new:
            items[it.item_id] = it
        page_min = min(
            (int(it.item_id.split("/")[1]) for it in page),
            default=before or 1,
        )
        log.info("fetched %s?before=%s: %d posts (%d new)", channel, before, len(page), len(new))
        if (not new and before is not None) or page_min <= 1:
            break
        before = page_min
        time.sleep(0.5)
    return sorted(items.values(), key=lambda it: int(it.item_id.split("/")[1]))


CLS_ERROR = {"relevant": None, "score": None, "category": "error", "reason": "classification failed"}


def build_report(channel: str, records: list[dict], threshold: int) -> str:
    total = len(records)
    scored = [r for r in records if r["score"] is not None]
    passed_pf = [r for r in records if r["prefilter_keyword"]]
    relevant = [r for r in scored if r["relevant"]]
    above = [r for r in scored if r["score"] >= threshold]
    avg = sum(r["score"] for r in scored) / len(scored) if scored else 0

    categories: dict[str, int] = {}
    for r in scored:
        categories[r["category"]] = categories.get(r["category"], 0) + 1

    def pct(n: int, d: int) -> str:
        return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"

    lines = [
        f"# Pipeline self-audit: @{channel}",
        "",
        "## Summary",
        "",
        f"- Text posts analyzed: **{total}**",
        f"- Pass prefilter (keyword gate): **{pct(len(passed_pf), total)}**",
        f"- LLM relevant: **{pct(len(relevant), len(scored))}**",
        f"- LLM score ≥ {threshold} (would be drafted): **{pct(len(above), len(scored))}**",
        f"- Average score: **{avg:.1f}**",
        "",
        "### Categories",
        "",
    ]
    for cat, n in sorted(categories.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {cat}: {n}")

    misses = [r for r in records if not r["prefilter_keyword"]]
    lines += ["", "## Prefilter misses (would be dropped before the LLM)", ""]
    if misses:
        for r in misses:
            score = r["score"] if r["score"] is not None else "—"
            lines.append(f"- [{r['item_id']}]({r['url']}) score={score} — {r['title']}")
    else:
        lines.append("None — every post matches at least one keyword.")

    low = sorted((r for r in scored if r["score"] < threshold), key=lambda r: r["score"])
    lines += ["", f"## Low-scoring posts (score < {threshold})", ""]
    if low:
        for r in low:
            lines.append(f"- [{r['item_id']}]({r['url']}) score={r['score']} ({r['category']}) — {r['title']}")
            lines.append(f"  - {r['reason']}")
    else:
        lines.append(f"None — every scored post is at {threshold} or above.")

    lines += [
        "",
        "## All posts by score",
        "",
        "| post | score | rel | prefilter | category | title |",
        "|---|---|---|---|---|---|",
    ]
    by_score = sorted(records, key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    for r in by_score:
        title = r["title"].replace("|", "\\|")
        rel = {True: "yes", False: "no", None: "?"}[r["relevant"]]
        score = r["score"] if r["score"] is not None else "err"
        lines.append(
            f"| [{r['item_id']}]({r['url']}) | {score} | {rel} "
            f"| {r['prefilter_keyword'] or '—'} | {r['category']} | {title} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("channel", nargs="?", default="video_engineer_pains")
    ap.add_argument("--limit", type=int, help="score only the N newest posts (smoke run)")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.llm_configured:
        sys.exit("LLM is not configured (LLM_API_KEY / LLM_MODEL in .env)")
    llm = LLM(cfg)

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        posts = fetch_all_posts(args.channel, client)
    log.info("total text posts: %d", len(posts))
    if args.limit:
        posts = posts[-args.limit :]

    records = []
    batch_size = max(1, cfg.classify_batch_size)
    for start in range(0, len(posts), batch_size):
        batch = posts[start : start + batch_size]
        for offset, (item, cls) in enumerate(zip(batch, llm.classify_batch(batch)), 1):
            cls = cls or CLS_ERROR
            keyword = prefilter.match(item.title, item.text)
            log.info(
                "[%d/%d] %s prefilter=%s score=%s %s",
                start + offset, len(posts), item.item_id, keyword or "MISS", cls["score"], cls["reason"][:80],
            )
            records.append(
                {
                    "item_id": item.item_id,
                    "url": item.url,
                    "date": item.published.isoformat() if item.published else None,
                    "title": item.title,
                    "prefilter_keyword": keyword,
                    **cls,
                }
            )
    log.info("LLM usage — %s", llm.usage_summary())

    json_path = f"analysis_{args.channel}.json"
    md_path = f"analysis_{args.channel}.md"
    with open(json_path, "w") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    report = build_report(args.channel, records, cfg.relevance_threshold)
    with open(md_path, "w") as f:
        f.write(report)

    print(f"\nwrote {json_path} and {md_path}\n")
    print(report.split("## Prefilter misses")[0])


if __name__ == "__main__":
    main()
