import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

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
    type: str  # rss | html | telegram | slack | discord | hn | bluesky
    url: str
    enabled: bool = True
    prefilter: bool = True  # false = always send to LLM (single-topic feeds like GitHub releases)
    token_env: str | None = None  # env var holding the auth token; defaults per type (see DEFAULT_TOKEN_ENV)
    token: str = ""  # resolved at load time from token_env; never set in sources.yaml
    proxy: bool = False  # fetch via SOCKS5_PROXY (source blocked on the local network)
    fetch_content: bool = True  # open the article URL and feed the LLM its full text (see CONTENT_FETCH_SKIP_TYPES)


# source types that require an auth token, mapped to the default env var it lives in
DEFAULT_TOKEN_ENV = {"slack": "SLACK_TOKEN", "discord": "DISCORD_TOKEN"}

# chat sources whose `url` is the post itself, not an external article — no page to fetch,
# so full-content enrichment skips them regardless of their fetch_content flag
CONTENT_FETCH_SKIP_TYPES = {"telegram", "slack", "discord"}


def _resolve_secret(value: str) -> str:
    """A `file:<path>` value reads the secret from that file (relative to repo root),
    so rotating a token kept in e.g. .ELIZA_TOKEN doesn't require editing .env."""
    if value.startswith("file:"):
        return (ROOT / value[5:]).read_text().strip()
    return value


@dataclass
class Config:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    classify_model: str  # defaults to llm_model; lets classification run on a cheaper model
    classify_batch_size: int
    tg_bot_token: str
    tg_chat_id: str
    relevance_threshold: int
    max_llm_items_per_run: int
    lookback_days: int
    prefilter_enabled: bool
    fetch_full_content: bool  # master switch for opening article pages before classify
    socks5_proxy: str  # socks5h://user:pass@host:port; "" = no proxy available
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
    sources = [Source(**s) for s in raw["sources"] if s.get("enabled", True)]

    # resolve auth tokens for source types that need one; drop token-less sources
    # (a missing token must not crash the cron run) with a warning
    enabled_sources = []
    for src in sources:
        if src.type in DEFAULT_TOKEN_ENV:
            env_var = src.token_env or DEFAULT_TOKEN_ENV[src.type]
            src.token = _resolve_secret(os.environ.get(env_var, ""))
            if not src.token:
                log.warning("%s: no token in $%s, skipping source", src.name, env_var)
                continue
        enabled_sources.append(src)

    llm_api_key = _resolve_secret(os.environ.get("LLM_API_KEY", ""))
    llm_model = os.environ.get("LLM_MODEL", "")

    return Config(
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        classify_model=os.environ.get("CLASSIFY_MODEL") or llm_model,
        classify_batch_size=int(os.environ.get("CLASSIFY_BATCH_SIZE", "8")),
        tg_bot_token=os.environ.get("TG_BOT_TOKEN", ""),
        tg_chat_id=os.environ.get("TG_CHAT_ID", ""),
        relevance_threshold=int(os.environ.get("RELEVANCE_THRESHOLD", "7")),
        max_llm_items_per_run=int(os.environ.get("MAX_LLM_ITEMS_PER_RUN", "40")),
        lookback_days=int(os.environ.get("LOOKBACK_DAYS", "3")),
        prefilter_enabled=os.environ.get("PREFILTER", "1") != "0",
        fetch_full_content=os.environ.get("FETCH_FULL_CONTENT", "1") != "0",
        socks5_proxy=_resolve_secret(os.environ.get("SOCKS5_PROXY", "")),
        sources=enabled_sources,
    )
