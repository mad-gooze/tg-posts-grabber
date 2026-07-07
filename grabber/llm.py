import json
import logging
import re

import httpx
from openai import OpenAI

from .config import Config, STYLE_EXAMPLES
from .fetchers import Item

log = logging.getLogger(__name__)

CLASSIFY_SYSTEM = """\
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

Respond with STRICT JSON only, no markdown fences:
{"relevant": true/false, "score": 0-10, "category": "codecs|streaming|players|browser|infrastructure|perception|industry|events|tools|other", "reason": "one short sentence"}
score = how well this fits the channel (10 = perfect deep-dive material)."""

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


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM response: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


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
        examples = "(примеров нет)"
        if STYLE_EXAMPLES.exists():
            examples = STYLE_EXAMPLES.read_text()
        self.draft_system = DRAFT_SYSTEM_TEMPLATE.format(examples=examples)

    def _chat_anthropic(self, system: str, user: str) -> str:
        resp = self.http.post(
            f"{self.base_url}/v1/messages",
            headers={
                "authorization": f"OAuth {self.api_key}",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    def _chat(self, system: str, user: str) -> dict:
        if self.anthropic:
            return _parse_json(self._chat_anthropic(system, user))
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
        )
        return _parse_json(resp.choices[0].message.content)

    def classify(self, item: Item) -> dict:
        user = f"Source: {item.source}\nTitle: {item.title}\nURL: {item.url}\n\n{item.text[:2000]}"
        result = self._chat(CLASSIFY_SYSTEM, user)
        return {
            "relevant": bool(result.get("relevant")),
            "score": int(result.get("score", 0)),
            "category": str(result.get("category", "other")),
            "reason": str(result.get("reason", "")),
        }

    def draft(self, item: Item, category: str) -> dict:
        user = (
            f"Категория: {category}\nИсточник: {item.source}\nЗаголовок: {item.title}\n"
            f"URL: {item.url}\n\nМатериал:\n{item.text[:2500]}"
        )
        result = self._chat(self.draft_system, user)
        title, text = str(result.get("title", "")).strip(), str(result.get("text", "")).strip()
        if not title or not text:
            raise ValueError(f"empty draft from LLM: {result!r}")
        return {"title": title, "text": text}
