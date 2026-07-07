import json
import logging
import re

from openai import OpenAI

from .config import Config, STYLE_EXAMPLES
from .fetchers import Item

log = logging.getLogger(__name__)

CLASSIFY_SYSTEM = """\
You are a content curator for the Russian Telegram channel «Страдания юного видеоинженера» \
(“Sufferings of a young video engineer”) — a channel for video engineers about video technology.

ON-TOPIC (relevant):
- Video codecs and compression: AV1/AV2, H.264/HEVC/VVC, VP9, licensing/patent-pool news
- Encoding, transcoding, FFmpeg and video tooling
- Streaming protocols and delivery: HLS, LL-HLS, DASH, CMAF, MoQ, WebRTC, SRT, RTMP, CDN
- Video players, playback, DRM
- Video quality and QoE: VMAF/PSNR/SSIM, per-title encoding, ABR algorithms
- Human visual perception, display technology, HDR, frame rates
- Engineering deep dives from streaming platforms (Netflix, YouTube, Twitch, Vimeo, etc.)
- Significant industry news: standards, alliances, acquisitions, shutdowns in the streaming space

OFF-TOPIC (reject):
- Marketing fluff, product promos and press releases without technical substance
- Webinar/conference announcements, job postings, event photo reports
- Generic cloud/AI/telecom news not specific to video
- Consumer gadget reviews, TV-show/content business news without a technology angle

Respond with STRICT JSON only, no markdown fences:
{"relevant": true/false, "score": 0-10, "category": "codecs|streaming|players|infrastructure|perception|industry|tools|other", "reason": "one short sentence"}
score = how well this fits the channel (10 = perfect deep-dive material)."""

DRAFT_SYSTEM_TEMPLATE = """\
Ты — автор русскоязычного Telegram-канала «Страдания юного видеоинженера» о видеотехнологиях: \
кодеки, стриминговые протоколы, плееры, качество видео, восприятие, инженерные истории индустрии.

Стиль канала:
- Русский язык, технические термины — на английском без перевода (bitrate, ABR, LL-HLS и т.п.)
- Тон: технично, но разговорно; лёгкая ирония и живые формулировки, без канцелярита и маркетинга
- Первая строка — жирный цепляющий заголовок по сути новости
- Дальше 1–3 коротких абзаца: что случилось, почему это важно инженеру, интересная деталь или вывод
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
        self.client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        self.model = cfg.llm_model
        examples = "(примеров нет)"
        if STYLE_EXAMPLES.exists():
            examples = STYLE_EXAMPLES.read_text()
        self.draft_system = DRAFT_SYSTEM_TEMPLATE.format(examples=examples)

    def _chat(self, system: str, user: str) -> dict:
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
