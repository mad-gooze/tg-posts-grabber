# Adding content sources

Each source is one entry in `sources.yaml` with a `type`. RSS and Telegram need no setup. This doc
covers the sources that need a token or a bit of setup.

After adding a source, smoke-test it without touching state or the LLM:

```bash
.venv/bin/python -m grabber --fetch <source-name>
```

Tokens go in `.env` and may be written as `file:<path>` to read from a gitignored file (same trick
as `LLM_API_KEY`), so rotating a token doesn't mean editing `.env`.

---

## Slack

Reads the latest messages of a channel via `conversations.history` with a **user token** (sees
whatever you can see). Rate limit for a personal/internal app is far above what we use (one request
per source per run).

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch** → pick the workspace.
2. **OAuth & Permissions** → **User Token Scopes** → add:
   `channels:history`, `channels:read` (add `groups:history`, `groups:read` for private channels).
3. **Install to Workspace** → authorize → copy the **User OAuth Token** (`xoxp-…`).
4. Put it in `.env`:
   ```
   SLACK_TOKEN=xoxp-...
   ```
5. Get the channel ID: open the channel → click its name → **About** tab → the `C…` ID is at the
   bottom (or read it from the channel URL `…/archives/C0123ABCD`).
6. Add to `sources.yaml` — `url` is `<workspace-slug>/<channel-id>` (the slug is the subdomain in
   your Slack URL, e.g. `video-dev` in `video-dev.slack.com`; it only builds message permalinks):
   ```yaml
   - name: slack-video-dev-general
     type: slack
     url: "video-dev/C0123ABCD"
   ```

**Second workspace?** Put its token in another env var and point the source at it:
```yaml
  - name: slack-acme-general
    type: slack
    url: "acme/C9999ZZZZ"
    token_env: SLACK_TOKEN_ACME
```

---

## Discord

Reads a channel's latest messages using a **plain user token** (self-bot). No bot, no server admin,
no gateway intents — it works for any channel the account has joined.

> ⚠️ **Self-botting violates Discord's Terms of Service and can get the account banned.** Use a
> **throwaway account**, never your main one. Expect to re-extract the token whenever the account
> re-logs in.

1. Create a throwaway Discord account and join the servers/channels you want to read.
2. Extract that account's user token: open Discord in a browser → DevTools (F12) → **Network** tab →
   trigger any action → click a request to `discord.com/api/…` → copy the **`Authorization`** request
   header value (it is *not* prefixed with `Bot `).
3. Put it in `.env`:
   ```
   DISCORD_TOKEN=...
   ```
4. Enable **User Settings → Advanced → Developer Mode**, then right-click the server → **Copy Server
   ID** (guild) and right-click the channel → **Copy Channel ID**.
5. Add to `sources.yaml` — `url` is `<guild-id>/<channel-id>` (the guild id only builds permalinks):
   ```yaml
   - name: discord-video-dev
     type: discord
     url: "507627062434070529/534567890123456789"
   ```

---

## Hacker News, Bluesky, Reddit

No setup — public endpoints. Just add a source:

```yaml
  - name: hn-hls
    type: hn
    url: "HLS streaming"     # Hacker News search query (Algolia, newest-first)

  - name: bsky-mux
    type: bluesky
    url: "mux.com"           # a Bluesky handle → that account's posts

  - name: reddit-ffmpeg
    type: rss                # Reddit's JSON API blocks bots; use its per-subreddit RSS feed
    url: "https://www.reddit.com/r/ffmpeg/.rss"
```

Notes:
- **Reddit** goes through the normal `rss` fetcher — its unauthenticated JSON API (`/new.json`)
  returns HTTP 403 to bots, but `/r/<sub>/.rss` still works.
- **Bluesky** takes a *handle* (fetches that account's recent posts). The keyword search endpoint
  exists in the fetcher (`url: "search:<query>"`) but Bluesky bot-blocks unauthenticated search, so
  in practice follow specific handles.
- If a source is blocked on your network, set `proxy: true` on it and configure `SOCKS5_PROXY`
  in `.env` (a failed direct fetch is also auto-retried through the proxy).

---

## YouTube

No new code — YouTube publishes a per-channel RSS feed, so use `type: rss`:

```yaml
  - name: yt-demuxed
    type: rss
    url: "https://www.youtube.com/feeds/videos.xml?channel_id=UC..."
```

Get the channel ID from the channel page's HTML: view-source and search for `channelId` (or use the
`…/feeds/videos.xml?channel_id=` URL directly if you already have the `UC…` id).

---

## LinkedIn — Pulse/newsletter authors (no auth)

LinkedIn has no public read API for group or feed content, but individual **Pulse** articles
(`linkedin.com/pulse/<slug>`) serve full HTML to an unauthenticated browser User-Agent, and each
article page embeds a "More from `<author>`" block linking that author's other recent articles. The
`linkedin` fetcher exploits this to follow specific authors:

```yaml
  - name: linkedin-dmitriy-vatolin
    type: linkedin
    url: "https://www.linkedin.com/pulse/<any-recent-article-by-the-author>/"
```

- `url` is a **seed** article URL — any reasonably recent Pulse article by the author you want to
  follow. The fetcher re-derives the author's recent article list from that page on every run, so a
  stable seed keeps surfacing new posts (an old seed page still lists the author's newest article).
- A **newsletter** landing page (`linkedin.com/newsletters/<slug>-<id>`) is also a valid seed: its
  issues are `/pulse/` links, so the fetcher discovers them the same way (the landing page itself is
  excluded from the results).
- To find a seed: open the author's article on LinkedIn (logged in or not) and copy its
  `/pulse/…` URL. Tracking query params are stripped automatically.
- A `999` on a given run (LinkedIn's bot block) is treated like a rate-limit: that fetch is skipped
  and retried next run.

**Only authors are followable.** Ongoing **post/activity feeds** (`/posts/…`, `/feed/…`), profiles
(`/in/…`, `…/today/author/…`), and company pages are login-walled (they answer HTTP 999) or carry no
author-article list, so they cannot be followed without auth — the fetcher rejects such seed URLs with
a warning. **Group** and personal-feed content likewise has no public read path (the official
Community Management API is gated behind partner approval, and scraping a logged-in session violates
ToS), so those remain unsupported.

## X.com — not implemented

X's API is paid pay-per-use (roughly $0.005 per post read, no free tier). A fetcher can be added if a
must-have account list justifies the cost, but it ships out of the box without one.
