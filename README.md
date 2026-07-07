# tg-grabber

Cron-driven content pipeline for the Telegram channel
[«Страдания юного видеоинженера»](https://t.me/s/video_engineer_pains).

Each run: fetches RSS feeds + public Telegram channels (`sources.yaml`) → dedupes against
`state.sqlite` → keyword prefilter (`grabber/prefilter.py`) → classifies remaining items with
an LLM (relevance to video engineering) → drafts posts in the channel's style (few-shot from
`style_examples.md`) → sends drafts (title, text, source URL, image) to you via a Telegram bot.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install feedparser httpx beautifulsoup4 openai python-dotenv pyyaml truststore
cp .env.example .env   # then fill it in
```

### 1. LLM

Any OpenAI-compatible endpoint. In `.env` set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
Examples: OpenAI (`https://api.openai.com/v1`), OpenRouter (`https://openrouter.ai/api/v1`),
local Ollama (`http://localhost:11434/v1`, any key).

Anthropic-format endpoints are also supported — if `LLM_BASE_URL` contains `anthropic`
(e.g. a proxy exposing the Messages API), the grabber speaks that dialect instead, and
trusts the system certificate store (for proxies behind a corporate CA).
`LLM_API_KEY=file:<path>` reads the key from a file relative to the repo root, so a
rotating token can live in its own gitignored file.

### 2. Telegram bot

1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TG_BOT_TOKEN`.
2. Send any message to your new bot (bots can't message you first).
3. `.venv/bin/python -m grabber --whoami` → copy the printed `TG_CHAT_ID` into `.env`.

### 3. First run

```bash
.venv/bin/python -m grabber --init      # mark current feed content as seen (no flood)
.venv/bin/python -m grabber --dry-run   # full pipeline, drafts printed to stdout
.venv/bin/python -m grabber             # real run, drafts arrive in Telegram
```

## Cron (4×/day)

```cron
0 9,13,17,21 * * * cd $HOME/tg-grabber && .venv/bin/python -m grabber >> grabber.log 2>&1
```

`crontab -e`, paste, done. Any cadence works — dedupe is stateful, nothing is sent twice.

## Tuning

- `RELEVANCE_THRESHOLD` (default 7): raise for fewer/better drafts, lower for more.
- `MAX_LLM_ITEMS_PER_RUN` (default 40): caps LLM spend per run; overflow items are picked up next run.
- `LOOKBACK_DAYS` (default 3): items older than this are marked seen without processing.
- `PREFILTER=0`: disable the keyword whitelist gate. By default RSS items with no
  video/audio/streaming keyword in title+text are marked `filtered` without an LLM call
  (the keyword list in `grabber/prefilter.py` is deliberately wide — false positives just
  cost one LLM call). Telegram sources and sources with `prefilter: false` in `sources.yaml`
  (single-topic feeds like GitHub releases, where titles are bare version numbers) bypass it.
- `sources.yaml`: add/remove sources; `enabled: false` disables without deleting.
- `--limit N`: one-off cap override for testing.

## Notes

- Rejected items are remembered (`status=rejected` in `state.sqlite`) and never re-processed;
  transient failures (fetch/LLM/send) are *not* marked, so they retry next run.
- Telegram sources use the public `t.me/s/<channel>` preview page (last ~20 messages, no API keys).
- Discord/Slack/LinkedIn sources from the old `sources.txt` were dropped — they require
  authentication; the fetcher layer is pluggable if you want to add them later.
