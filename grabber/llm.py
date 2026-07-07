import json
import logging
import re
import time

import httpx
from openai import OpenAI

from .config import Config, STYLE_EXAMPLES
from .fetchers import Item

log = logging.getLogger(__name__)

# transient HTTP statuses worth retrying on the Anthropic path (the OpenAI SDK path
# retries these itself); 429 = rate limit, 529 = Anthropic "overloaded"
_RETRY_STATUSES = {408, 429, 500, 502, 503, 529}

_CLASSIFY_BODY = """\
You are a content curator for the Russian Telegram channel «Страдания юного видеоинженера» \
(“Sufferings of a young video engineer”) — a channel for video engineers about video technology.
The authors are player/streaming engineers; the audience builds video services.

ON-TOPIC (relevant):
- Video codecs and compression: AV1/AV2, H.264/HEVC/VVC, VP9, LC-EVC, licensing/patent-pool news
- Audio codecs and processing (Opus, xHE-AAC, low-bitrate voice codecs, OS audio stacks)
- Image codecs and compression (JPEG/jpegli, JPEG-XL, WebP, AVIF, placeholders/LQIP)
- Encoding, transcoding, FFmpeg/GStreamer/OBS and releases of video tools
- Streaming protocols and delivery: HLS, LL-HLS, DASH, CMAF, MoQ, WebRTC, SRT, RTMP/WHIP, CDN, CMCD
- Video players and their releases (hls.js, dash.js, shaka-player, ExoPlayer, AVPlayer…), playback on Smart TV/STB
- Browser/web-platform media: MSE, EME, WebCodecs, WebRTC APIs, WebTransport, browser releases affecting video
- DRM (Widevine, PlayReady, FairPlay), video security incidents, piracy tech
- Video quality and QoE: VMAF/PSNR/SSIM, per-title encoding, ABR algorithms, codec/service quality comparisons and research reports
- Human visual perception research, display technology, HDR, frame rates
- Engineering deep dives from video platforms, incl. Russian ones (Netflix, YouTube, Twitch, Кинопоиск, VK Видео, RuTube, Ozon, Kinescope…)
- Conferences, meetups, webinars, podcasts and CFPs ABOUT video engineering (Demuxed, VideoTech, RTC@Scale, IEEE SPS, NAB/IBC…), incl. talk recordings — score by how technical the program is
- Useful engineering resources: test-clip collections, glossaries, tutorials, awesome-lists, debugging tools
- Playful engineering curiosities and pet projects: player/codec hacks, weird custom formats, \
reverse-engineering of video features, community drama around video tools — the channel loves these
- AI/ML only when tied to the video pipeline: super-resolution, neural codecs, video generation infra, translation/lipsync of video
- Significant industry news with an engineering angle: standards, alliances, acquisitions, shutdowns, dev contests

OFF-TOPIC (reject):
- Marketing fluff, product promos and press releases without technical substance
- Events/webinars NOT about video technology; event photo reports without content
- Job postings
- Generic cloud/AI/telecom/frontend news not specific to video, audio or images
- Consumer gadget reviews, TV-show/content business news without a technology angle

Score calibration:
- 9-10: engineering deep dive, major codec/protocol/player/browser news, strong research report
- 7-8: solid release notes, technical event announcement with a concrete program, good tool/resource; \
also: podcast episodes on video/audio engineering, CFPs and announcements of recognized \
video-engineering conferences/meetups (Demuxed, VideoTech, RTC@Scale…) even before the full program \
is out, and fun engineering curiosities/pet projects — don't punish these for being "shallow", \
being entertaining and on-topic is the point
- 5-6: on-topic but thin or too niche; industry/market news with only a light engineering angle
- 0-4: marketing, duplicates of common knowledge, off-topic
"""

_CATEGORIES = "codecs|streaming|players|browser|infrastructure|perception|industry|events|tools|other"

CLASSIFY_SYSTEM = _CLASSIFY_BODY + f"""
Respond with STRICT JSON only, no markdown fences:
{{"relevant": true/false, "score": 0-10, "category": "{_CATEGORIES}", "reason": "one short sentence"}}
score = how well this fits the channel (10 = perfect deep-dive material)."""

CLASSIFY_BATCH_SYSTEM = _CLASSIFY_BODY + f"""
You will be given several numbered items. Respond with a STRICT JSON array only, no markdown \
fences — exactly one object per item, in input order:
[{{"id": 1, "relevant": true/false, "score": 0-10, "category": "{_CATEGORIES}", "reason": "one short sentence"}}, ...]
score = how well each item fits the channel (10 = perfect deep-dive material)."""

CLUSTER_SYSTEM = """\
You are grouping news items that a video-engineering channel is about to post.
Group together ONLY items that report the SAME underlying story, event, or release —
near-duplicates, including the same story told by different sources or in different \
languages (e.g. a Russian and an English write-up of the same codec release). Items about \
different stories must stay in separate groups, even if they share a topic.

You are given numbered items (ids 1..N). Respond with a STRICT JSON array only, no markdown \
fences — one object per group, each listing the member ids:
[{"members": [1, 3]}, {"members": [2]}]
Every id from 1 to N must appear exactly once. Never invent ids that were not given."""

DRAFT_SYSTEM_TEMPLATE = """\
Ты — автор русскоязычного Telegram-канала «Страдания юного видеоинженера» о видеотехнологиях: \
кодеки, стриминговые протоколы, плееры, качество видео, восприятие, инженерные истории индустрии.

Стиль канала:
- Русский язык, технические термины — на английском без перевода (bitrate, ABR, LL-HLS и т.п.)
- Тон: технично, но разговорно; лёгкая ирония и живые формулировки, без канцелярита и маркетинга
- Голос — «мы», от лица авторов канала («мы проверили», «мы наткнулись»)
- Первая строка — жирный цепляющий заголовок по сути новости; игра слов и каламбуры приветствуются
- Дальше 1–3 коротких абзаца: что случилось, почему это важно инженеру, интересная деталь или вывод
- Для release notes и программ мероприятий уместен маркированный список (•) из 2–4 пунктов
- Хорошо заканчивать коротким вопросом к читателям («Переходим?», «Участвуем?», «Пойдёте?»), но не в каждом посте
- Живой жаргон канала: «завезли», «раскатили», «под капотом», «жмёт», «пет-проджект»
- Без хэштегов, без эмодзи-спама (одно уместное эмодзи допустимо)

Примеры реальных постов канала:

{examples}

Задача: по материалу из источника подготовить ЧЕРНОВИК поста в этом стиле. Это переработка своими \
словами, не перевод и не копия. Если источник — чужой телеграм-пост, перескажи суть в голосе канала.

Ответь СТРОГО JSON без markdown-ограждений:
{{"title": "заголовок без разметки", "text": "текст поста в HTML-разметке Telegram (<b>, <i>, <a href=\\"...\\">), заголовок в text НЕ повторять, ссылку на источник НЕ вставлять — она добавится автоматически, максимум 700 символов"}}"""


def _strip_fences(raw: str) -> str:
    """Drop a leading ```/```json fence and a trailing ``` so bare JSON survives."""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())


def _parse_json(raw: str) -> dict:
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM response: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


def _parse_json_array(raw: str) -> list:
    text = _strip_fences(raw)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in LLM response: {raw[:200]!r}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError(f"expected JSON array, got {type(data).__name__}")
    return data


def _material(item: Item, cap: int) -> str:
    """Text handed to the LLM: the fetched full-page content when available (it's richer than
    the feed teaser), otherwise the feed snippet, sliced to `cap` chars to bound tokens."""
    return (item.content or item.text)[:cap]


def _normalize_cls(result: dict) -> dict:
    return {
        "relevant": bool(result.get("relevant")),
        "score": int(result.get("score", 0)),
        "category": str(result.get("category", "other")),
        "reason": str(result.get("reason", "")),
    }


class LLM:
    def __init__(self, cfg: Config):
        # endpoints like eliza's /raw/anthropic speak the Anthropic Messages API,
        # not the OpenAI chat API — pick the dialect from the base URL
        self.anthropic = "anthropic" in cfg.llm_base_url
        self.base_url = cfg.llm_base_url.rstrip("/")
        self.api_key = cfg.llm_api_key
        if self.anthropic:
            # internal endpoints (eliza) use a corporate CA absent from certifi's
            # bundle — trust the system store instead
            try:
                import ssl

                import truststore

                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except ImportError:
                verify = True
            self.http = httpx.Client(verify=verify, timeout=120)
        else:
            self.client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        self.model = cfg.llm_model
        self.classify_model = cfg.classify_model or cfg.llm_model
        # token accounting per call kind ("classify" / "draft"), filled from response usage
        self.usage: dict[str, dict[str, int]] = {}
        examples = "(примеров нет)"
        if STYLE_EXAMPLES.exists():
            examples = STYLE_EXAMPLES.read_text()
        self.draft_system = DRAFT_SYSTEM_TEMPLATE.format(examples=examples)

    def _track(self, kind: str, usage: dict) -> None:
        t = self.usage.setdefault(kind, {"calls": 0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
        t["calls"] += 1
        t["in"] += usage.get("input_tokens") or 0
        t["out"] += usage.get("output_tokens") or 0
        t["cache_read"] += usage.get("cache_read_input_tokens") or 0
        t["cache_write"] += usage.get("cache_creation_input_tokens") or 0
        log.debug("%s usage: %s", kind, usage)

    def usage_summary(self) -> str:
        parts = []
        for kind, t in self.usage.items():
            s = f"{kind}: {t['calls']} calls, {t['in']} in / {t['out']} out"
            if t["cache_read"] or t["cache_write"]:
                s += f" (cache: {t['cache_read']} read, {t['cache_write']} written)"
            parts.append(s)
        return "; ".join(parts) or "no LLM calls"

    def _post_with_retry(self, url: str, headers: dict, payload: dict, attempts: int = 3) -> httpx.Response:
        """POST with exponential backoff on transient network errors and retryable status
        codes. The OpenAI SDK path retries on its own; this Anthropic path previously did a
        single post, so one transient 429/5xx dropped a whole classify batch or draft."""
        delay = 1.0
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                resp = self.http.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                if last:
                    raise
                log.warning("LLM request error (%s); retry %d/%d in %.0fs", e, attempt, attempts - 1, delay)
            else:
                if resp.status_code not in _RETRY_STATUSES or last:
                    return resp
                log.warning("LLM HTTP %s; retry %d/%d in %.0fs", resp.status_code, attempt, attempts - 1, delay)
            time.sleep(delay)
            delay *= 2

    def _chat_raw(
        self,
        system: str,
        user: str,
        kind: str,
        model: str,
        cache_system: bool = False,
        max_tokens: int = 1024,
        disable_thinking: bool = False,
    ) -> str:
        if self.anthropic:
            # cache_control is honored only if the cached prefix clears the model's minimum
            # cacheable size and the endpoint supports it, else it's silently ignored. The
            # eliza endpoint honors it well below the ~1024-token floor claimed for Opus —
            # measured cache hits at ~1.4K (classify) and ~2.8K (draft) system prompts.
            # Confirm via the cache_read/cache_write counters in usage_summary().
            system_payload = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if cache_system
                else system
            )
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_payload,
                "messages": [{"role": "user", "content": user}],
            }
            if disable_thinking:
                # some models (e.g. Sonnet 5) default to adaptive thinking when the
                # field is omitted, which prepends a thinking block and spends output
                # tokens; classification doesn't need it. (Fable 5 rejects "disabled".)
                payload["thinking"] = {"type": "disabled"}
            resp = self._post_with_retry(
                f"{self.base_url}/v1/messages",
                {
                    "authorization": f"OAuth {self.api_key}",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                payload,
            )
            resp.raise_for_status()
            data = resp.json()
            self._track(kind, data.get("usage") or {})
            # skip any leading thinking blocks — take the first text block
            text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
            if text_block is None:
                raise ValueError(f"no text block in LLM response: {data.get('content')!r}")
            return text_block["text"]
        resp = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
        )
        if resp.usage:
            self._track(
                kind,
                {"input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens},
            )
        return resp.choices[0].message.content

    def classify(self, item: Item) -> dict:
        user = f"Source: {item.source}\nTitle: {item.title}\nURL: {item.url}\n\n{_material(item, 3000)}"
        raw = self._chat_raw(CLASSIFY_SYSTEM, user, "classify", self.classify_model, disable_thinking=True)
        return _normalize_cls(_parse_json(raw))

    def classify_batch(self, items: list[Item]) -> list[dict | None]:
        """Score several items in one call — the classify system prompt dominates
        per-call cost, so batching amortizes it. Falls back to per-item classify()
        if the batch response doesn't parse; None marks items that failed both ways."""
        if len(items) > 1:
            user = "\n\n".join(
                f"### Item {i}\nSource: {item.source}\nTitle: {item.title}\nURL: {item.url}\n\n{_material(item, 2000)}"
                for i, item in enumerate(items, 1)
            )
            try:
                raw = self._chat_raw(
                    CLASSIFY_BATCH_SYSTEM,
                    user,
                    "classify",
                    self.classify_model,
                    cache_system=True,  # identical ~1.4K-token system prompt across every batch in a run
                    max_tokens=max(1024, 128 * len(items)),
                    disable_thinking=True,
                )
                by_id = {int(v["id"]): v for v in _parse_json_array(raw) if isinstance(v, dict)}
                return [_normalize_cls(by_id[i]) for i in range(1, len(items) + 1)]
            except Exception as e:
                log.warning("batch classify of %d items failed (%s), retrying per item", len(items), e)
        results: list[dict | None] = []
        for item in items:
            try:
                results.append(self.classify(item))
            except Exception as e:
                log.warning("classify failed for %s: %s", item.url, e)
                results.append(None)
        return results

    def cluster(self, items: list[Item]) -> list[list[int]]:
        """Group items reporting the same story into clusters of 0-based indices.

        Catches semantic near-dups the lexical pass misses (e.g. RU vs EN of one story).
        Only ever merges, never rejects — on any failure every item becomes its own
        singleton so the caller falls back to the lexical grouping unchanged."""
        n = len(items)
        singletons = [[i] for i in range(n)]
        if n < 2:
            return singletons
        user = "\n\n".join(
            f"### Item {i}\nSource: {item.source}\nTitle: {item.title}\n\n{item.text[:200]}"
            for i, item in enumerate(items, 1)
        )
        try:
            raw = self._chat_raw(
                CLUSTER_SYSTEM,
                user,
                "cluster",
                self.classify_model,
                # no cache_system: cluster runs once per run, so a cache write would never be
                # read back — it'd cost ~1.25x with no payoff (unlike the repeated classify batches)
                max_tokens=max(512, 64 * n),
                disable_thinking=True,
            )
            groups: list[list[int]] = []
            seen: set[int] = set()
            for entry in _parse_json_array(raw):
                if not isinstance(entry, dict):
                    continue
                idxs = []
                for m in entry.get("members", []):
                    try:
                        i = int(m) - 1  # prompt is 1-based; return 0-based
                    except (TypeError, ValueError):
                        continue
                    if 0 <= i < n and i not in seen:
                        seen.add(i)
                        idxs.append(i)
                if idxs:
                    groups.append(idxs)
            # any id the model dropped becomes its own singleton
            for i in range(n):
                if i not in seen:
                    groups.append([i])
            return groups
        except Exception as e:
            log.warning("cluster of %d items failed (%s), keeping lexical groups", n, e)
            return singletons

    def draft(self, item: Item, category: str) -> dict:
        user = (
            f"Категория: {category}\nИсточник: {item.source}\nЗаголовок: {item.title}\n"
            f"URL: {item.url}\n\nМатериал:\n{_material(item, 6000)}"
        )
        raw = self._chat_raw(self.draft_system, user, "draft", self.model, cache_system=True)
        result = _parse_json(raw)
        title, text = str(result.get("title", "")).strip(), str(result.get("text", "")).strip()
        if not title or not text:
            raise ValueError(f"empty draft from LLM: {result!r}")
        return {"title": title, "text": text}
