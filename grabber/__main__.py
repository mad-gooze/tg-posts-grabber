import argparse
import html
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .config import CONTENT_FETCH_SKIP_TYPES, Config, Source, STATE_DB, USER_AGENT, load_config
from .content import fetch_content
from .dedup import content_key, hamming_close, normalize_url
from .fetchers import FETCHERS, PREFILTER_EXEMPT_TYPES, Item
from .llm import LLM
from .notify import Notifier
from .prefilter import match as prefilter_match
from .state import State

log = logging.getLogger("grabber")

# most sources link is what drives Telegram's web preview, so keep the list short
MAX_SOURCE_LINKS = 6


@dataclass
class Group:
    """A set of items judged to be the same story. Drafted once; every member is marked
    in state so none re-processes next run."""

    members: list[Item]
    representative: Item
    classification: dict | None = None


def _pick_representative(members: list[Item], by_score: bool) -> Item:
    """Pre-classify (by_score=False): longest text, tie-break earliest published — a proxy
    for the most substantial write-up. Post-merge (by_score=True): handled by the caller
    from group scores, so this branch just falls back to the same heuristic."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return min(members, key=lambda m: (-len(m.text), m.published or epoch))


def _group_lexically(items: list[Item]) -> list[Group]:
    """Union-find grouping by exact normalized URL or near-duplicate simhash."""
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        parent[find(a)] = find(b)

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            same_url = bool(a.norm_url) and a.norm_url == b.norm_url
            if same_url or hamming_close(a.simhash, b.simhash):
                union(i, j)

    buckets: dict[int, list[Item]] = {}
    for i, item in enumerate(items):
        buckets.setdefault(find(i), []).append(item)
    return [Group(members=m, representative=_pick_representative(m, by_score=False)) for m in buckets.values()]


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


def format_message(draft: dict, group: Group, classification: dict) -> str:
    rep = group.representative
    header = f"📝 <b>{html.escape(draft['title'])}</b>\n\n{draft['text']}\n\n"
    tag = f"#{classification['category']} · score {classification['score']}"

    if len(group.members) == 1:
        link = f"🔗 <a href=\"{html.escape(rep.url)}\">{html.escape(rep.url)}</a>\n" if rep.url else ""
        return header + link + f"{tag} · {html.escape(rep.source)}"

    # multi-source: one link per distinct source URL, representative first so the web
    # preview uses it; de-dup by normalized URL (two members can share a link)
    ordered = [rep] + [m for m in group.members if m is not rep]
    seen: set[str] = set()
    links: list[str] = []
    for m in ordered:
        if not m.url:
            continue
        key = normalize_url(m.url) or m.url
        if key in seen:
            continue
        seen.add(key)
        links.append(f"• <a href=\"{html.escape(m.url)}\">{html.escape(m.source)}</a>")

    n_sources = len(links)
    extra = ""
    if len(links) > MAX_SOURCE_LINKS:
        extra = f"\n• +{len(links) - MAX_SOURCE_LINKS} ещё"
        links = links[:MAX_SOURCE_LINKS]
    sources_block = "🔗 Источники:\n" + "\n".join(links) + extra + "\n"
    return header + sources_block + f"{tag} · {n_sources} sources"


def _mark_group(state: State, group: Group, status: str, score: int | None = None):
    """Persist every member (not just the representative) so is_known suppresses the whole
    story next run, and store each member's content-key so later cross-run reshares dedup."""
    rep = group.representative
    ref = f"{rep.source}/{rep.item_id}"
    for m in group.members:
        state.mark(
            m.source, m.item_id, m.url, status, score,
            norm_url=m.norm_url, simhash=m.simhash,
            primary_ref=None if m is rep else ref,
        )


def _suppress_cross_run(state: State, items: list[Item], cutoff_iso: str) -> list[Item]:
    """Drop items whose content already appeared (drafted or rejected) in a recent run,
    marking them 'duplicate'. This is deterministic and spends no LLM calls."""
    known = state.recent_content_keys(cutoff_iso)
    url_map = {row[4]: (row[0], row[1]) for row in known if row[4]}  # norm_url -> (source, item_id)
    sim_rows = [(row[5], row[0], row[1]) for row in known if row[5]]  # (simhash, source, item_id)

    survivors: list[Item] = []
    for item in items:
        primary = None
        if item.norm_url and item.norm_url in url_map:
            primary = url_map[item.norm_url]
        elif item.simhash:
            for sh, src, iid in sim_rows:
                if hamming_close(item.simhash, sh):
                    primary = (src, iid)
                    break
        if primary is None:
            survivors.append(item)
            continue
        state.mark(
            item.source, item.item_id, item.url, "duplicate",
            norm_url=item.norm_url, simhash=item.simhash,
            primary_ref=f"{primary[0]}/{primary[1]}",
        )
        log.info("cross-run dup of %s/%s [%s] %s", primary[0], primary[1], item.source, item.title[:60])
    return survivors


def _merge_clusters(llm: LLM, groups: list[Group]) -> list[Group]:
    """Ask the LLM to merge groups reporting the same story. Only merges, never rejects —
    on failure the lexical groups pass through unchanged. Merged group keeps the highest-
    scoring member's classification and representative."""
    if len(groups) < 2:
        return groups
    clusters = llm.cluster([g.representative for g in groups])
    merged: list[Group] = []
    for idxs in clusters:
        chosen = [groups[i] for i in idxs]
        primary = max(chosen, key=lambda g: (g.classification["score"], len(g.representative.text)))
        members = [m for g in chosen for m in g.members]
        merged.append(Group(members=members, representative=primary.representative,
                            classification=primary.classification))
    return merged


def enrich_groups(cfg: Config, groups: list[Group], state: State, proxied_sources: set[str]) -> None:
    """Fetch each eligible group representative's full article page and store the extracted
    text on rep.content, so classify + draft work from the real page, not the feed teaser.

    Only representatives are fetched (one page per story) and only for groups that survived
    to the LLM stage, so at most len(groups) <= MAX_LLM_ITEMS_PER_RUN pages are opened.
    Best-effort: cache hits are reused, failures leave rep.content empty and the LLM falls
    back to rep.text."""
    if not cfg.fetch_full_content:
        return
    src_by_name = {s.name: s for s in cfg.sources}

    todo: list[Item] = []  # reps that need a live fetch (cache miss)
    cached = 0
    for g in groups:
        rep = g.representative
        src = src_by_name.get(rep.source)
        if src is None or not src.fetch_content or src.type in CONTENT_FETCH_SKIP_TYPES:
            continue
        if not rep.url.lower().startswith(("http://", "https://")):
            continue
        hit = state.get_content(rep.norm_url)
        if hit:
            rep.content = hit
            cached += 1
        else:
            todo.append(rep)

    fetched = 0
    if todo:
        with httpx.Client(timeout=20) as direct, _proxied_client(cfg) as proxied:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {}
                for rep in todo:
                    use_proxy = rep.source in proxied_sources and proxied is not None
                    futures[pool.submit(fetch_content, rep.url, proxied if use_proxy else direct)] = rep
                for future in as_completed(futures):
                    rep = futures[future]
                    try:
                        md = future.result()
                    except Exception as e:  # fetch_content swallows its own errors; belt and suspenders
                        log.info("content enrich failed for %s (%s)", rep.url, e)
                        md = None
                    if md:
                        rep.content = md
                        state.set_content(rep.norm_url, md)
                        fetched += 1

    if cached or todo:
        log.info(
            "content: enriched %d of %d groups (%d fetched, %d cached, %d failed)",
            cached + fetched, len(groups), fetched, cached, len(todo) - fetched,
        )


def run(cfg: Config, init_only: bool, dry_run: bool, limit: int | None):
    state = State(STATE_DB)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.lookback_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")

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

    # content keys drive both cross-run suppression and within-run grouping
    for item in new_items:
        item.norm_url, item.simhash = content_key(item)

    survivors = _suppress_cross_run(state, new_items, cutoff_iso)
    cross_run_dups = len(new_items) - len(survivors)

    groups = _group_lexically(survivors)
    groups.sort(
        key=lambda g: g.representative.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    lexical_groups = len(groups)

    cap = limit if limit is not None else cfg.max_llm_items_per_run
    if len(groups) > cap:
        log.info("capping LLM processing to %d of %d groups (rest stay unseen for next run)", cap, len(groups))
        groups = groups[:cap]

    if not groups:
        log.info("no new items (%d cross-run dups)", cross_run_dups)
        state.prune((now - timedelta(days=2 * cfg.lookback_days)).strftime("%Y-%m-%d %H:%M:%S"))
        state.close()
        return

    if not cfg.llm_configured:
        log.warning("LLM not configured (LLM_API_KEY/LLM_MODEL) — listing %d groups, not marking them", len(groups))
        for g in groups:
            r = g.representative
            extra = f" (+{len(g.members) - 1} sources)" if len(g.members) > 1 else ""
            print(f"[{r.source}] {r.title} — {r.url}{extra}")
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

    # open the article page of each surviving representative so classify + draft see the
    # full text, not just the feed snippet (bounded to <= cap, cached in state.sqlite)
    enrich_groups(cfg, groups, state, proxied_sources)

    # classify phase: one call per group representative
    rejected = 0
    relevant: list[Group] = []
    batch_size = max(1, cfg.classify_batch_size)
    reps = [g.representative for g in groups]
    for start in range(0, len(groups), batch_size):
        batch_groups = groups[start : start + batch_size]
        for group, cls in zip(batch_groups, llm.classify_batch(reps[start : start + batch_size])):
            if cls is None:
                # classify failed even per-item (already logged); group stays unmarked for next run
                continue
            if not cls["relevant"] or cls["score"] < cfg.relevance_threshold:
                _mark_group(state, group, "rejected", cls["score"])
                rejected += 1
                log.info("rejected (%d/10) [%s] %s — %s", cls["score"], group.representative.source,
                         group.representative.title[:60], cls["reason"])
                continue
            group.classification = cls
            relevant.append(group)

    # semantic merge of same-story groups the lexical pass missed
    final = _merge_clusters(llm, relevant)
    llm_merged = len(relevant) - len(final)

    # draft + send phase: one draft per final group
    drafted = 0
    for group in final:
        rep, cls = group.representative, group.classification
        try:
            draft = llm.draft(rep, cls["category"])
        except Exception as e:
            log.warning("draft failed for %s (%s), leaving for next run", rep.url, e)
            continue

        # article pages of blocked sources are blocked too — look up og:image via proxy
        image = rep.image_url
        if not image and rep.url:
            image = og_image(rep.url, cfg.socks5_proxy if rep.source in proxied_sources else None)
        message = format_message(draft, group, cls)

        if dry_run:
            print(f"\n{'=' * 70}\nIMAGE: {image}\n{message}")
        else:
            try:
                notifier.send_draft(message, image)
            except Exception as e:
                log.error("send failed for %s (%s), leaving for next run", rep.url, e)
                continue
        _mark_group(state, group, "drafted", cls["score"])
        drafted += 1

    log.info(
        "done: %d drafted, %d rejected, %d cross-run dups, %d lexical groups, %d llm-merged",
        drafted, rejected, cross_run_dups, lexical_groups, llm_merged,
    )
    log.info("tokens: %s", llm.usage_summary())
    state.prune((now - timedelta(days=2 * cfg.lookback_days)).strftime("%Y-%m-%d %H:%M:%S"))
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
