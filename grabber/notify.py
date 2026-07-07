import logging
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.client = httpx.Client(timeout=30)

    def _call(self, method: str, payload: dict) -> httpx.Response:
        return self.client.post(API.format(token=self.token, method=method), json=payload)

    def send_draft(self, message_html: str, image_url: str | None):
        """Send one draft; falls back photo→text and HTML→plain so a draft is never lost."""
        if image_url:
            resp = self._call(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "photo": image_url,
                    "caption": message_html[:1024],
                    "parse_mode": "HTML",
                },
            )
            if resp.status_code == 200:
                return
            log.warning("sendPhoto failed (%s), falling back to text: %s", resp.status_code, resp.text[:200])

        resp = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message_html[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        if resp.status_code != 200:
            log.warning("sendMessage with HTML failed (%s), retrying as plain text: %s",
                        resp.status_code, resp.text[:200])
            resp = self._call(
                "sendMessage",
                {"chat_id": self.chat_id, "text": message_html[:4096]},
            )
            resp.raise_for_status()
        time.sleep(1)  # Bot API rate limit

    def whoami(self):
        """Print chat IDs seen in recent bot updates (send the bot any message first)."""
        resp = self._call("getUpdates", {})
        resp.raise_for_status()
        updates = resp.json().get("result", [])
        if not updates:
            print("No updates. Open Telegram, send any message to your bot, then rerun --whoami.")
            return
        seen = {}
        for upd in updates:
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat", {})
            if chat.get("id"):
                seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "?")
        for chat_id, name in seen.items():
            print(f"TG_CHAT_ID={chat_id}  ({name})")
