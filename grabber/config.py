import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATE_DB = ROOT / "state.sqlite"
STYLE_EXAMPLES = ROOT / "style_examples.md"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class Source:
    name: str
    type: str  # "rss" | "telegram"
    url: str
    enabled: bool = True
    prefilter: bool = True  # false = always send to LLM (single-topic feeds like GitHub releases)


@dataclass
class Config:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tg_bot_token: str
    tg_chat_id: str
    relevance_threshold: int
    max_llm_items_per_run: int
    lookback_days: int
    prefilter_enabled: bool
    sources: list[Source]

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def bot_configured(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    with open(ROOT / "sources.yaml") as f:
        raw = yaml.safe_load(f)
    sources = [Source(**s) for s in raw["sources"]]

    # LLM_API_KEY=file:<path> reads the key from a file (relative to repo root),
    # so rotating a token kept in e.g. .ELIZA_TOKEN doesn't require editing .env
    llm_api_key = os.environ.get("LLM_API_KEY", "")
    if llm_api_key.startswith("file:"):
        llm_api_key = (ROOT / llm_api_key[5:]).read_text().strip()

    return Config(
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=llm_api_key,
        llm_model=os.environ.get("LLM_MODEL", ""),
        tg_bot_token=os.environ.get("TG_BOT_TOKEN", ""),
        tg_chat_id=os.environ.get("TG_CHAT_ID", ""),
        relevance_threshold=int(os.environ.get("RELEVANCE_THRESHOLD", "7")),
        max_llm_items_per_run=int(os.environ.get("MAX_LLM_ITEMS_PER_RUN", "40")),
        lookback_days=int(os.environ.get("LOOKBACK_DAYS", "3")),
        prefilter_enabled=os.environ.get("PREFILTER", "1") != "0",
        sources=[s for s in sources if s.enabled],
    )
