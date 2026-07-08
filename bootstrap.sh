#!/usr/bin/env bash
# One-shot setup for tg-grabber: collect tokens, install deps, seed state,
# install a schedule (cron or systemd timer), and do a first run. Safe to re-run.
#
# Interactive by default. For unattended/server setup, run non-interactively and
# supply config via the environment:
#   LLM_BASE_URL=… LLM_API_KEY=… LLM_MODEL=… \
#   TG_BOT_TOKEN=… TG_CHAT_ID=… ./bootstrap.sh -y
# Optional env: SLACK_TOKEN, DISCORD_TOKEN, SOCKS5_PROXY, RELEVANCE_THRESHOLD,
#   MAX_LLM_ITEMS_PER_RUN, LOOKBACK_DAYS, CLASSIFY_MODEL, CLASSIFY_BATCH_SIZE,
#   CRON_SCHEDULE / SYSTEMD_ONCALENDAR (schedule), SEND_ON_BOOTSTRAP=1 (send for real).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"
PY="$VENV/bin/python"
CRON_DEFAULT="0 9,13,17,21 * * *"
SYSTEMD_ONCALENDAR_DEFAULT="*-*-* 09,13,17,21:00:00"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Options + interactivity mode
# ---------------------------------------------------------------------------
NONINTERACTIVE=0
USE_SYSTEMD=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes|--non-interactive) NONINTERACTIVE=1 ;;
        --systemd) USE_SYSTEMD=1 ;;
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done
# no controlling TTY (piped into a provisioner) ⇒ behave non-interactively
if [ "$NONINTERACTIVE" -eq 0 ] && [ ! -t 0 ]; then
    NONINTERACTIVE=1
fi

# ask VAR "prompt" ["default"] — required value. Interactive: prompt (loops until non-empty
# or uses the default). Non-interactive: take $VAR from the environment, else the default,
# else fail naming the variable.
ask() {
    local __var=$1 __prompt=$2 __default=${3-} __ans
    if [ "$NONINTERACTIVE" -eq 1 ]; then
        __ans=${!__var-}
        [ -z "$__ans" ] && __ans=$__default
        [ -z "$__ans" ] && die "$__var is required — export it (non-interactive mode)."
        printf -v "$__var" '%s' "$__ans"
        info "$__var=$__ans"
        return
    fi
    while :; do
        if [ -n "$__default" ]; then
            read -r -p "$__prompt [$__default]: " __ans || true
            __ans=${__ans:-$__default}
        else
            read -r -p "$__prompt: " __ans || true
        fi
        [ -n "$__ans" ] && break
        warn "This value is required."
    done
    printf -v "$__var" '%s' "$__ans"
}

# ask_secret VAR "prompt" — required secret; hidden interactive input, $VAR when headless.
ask_secret() {
    local __var=$1 __prompt=$2 __ans
    if [ "$NONINTERACTIVE" -eq 1 ]; then
        __ans=${!__var-}
        [ -z "$__ans" ] && die "$__var is required — export it (non-interactive mode)."
        printf -v "$__var" '%s' "$__ans"
        info "$__var set (hidden)"
        return
    fi
    while :; do
        read -r -s -p "$__prompt: " __ans || true
        echo
        [ -n "$__ans" ] && break
        warn "This value is required."
    done
    printf -v "$__var" '%s' "$__ans"
}

# ask_opt VAR "prompt" — optional; empty leaves it unset. Headless: take $VAR if present.
ask_opt() {
    local __var=$1 __prompt=$2 __ans
    if [ "$NONINTERACTIVE" -eq 1 ]; then
        __ans=${!__var-}
        printf -v "$__var" '%s' "$__ans"
        [ -n "$__ans" ] && info "$__var set"
        return
    fi
    read -r -p "$__prompt (optional, enter to skip): " __ans || true
    printf -v "$__var" '%s' "$__ans"
}

confirm() {  # confirm "question" -> 0 for yes; always "no" when non-interactive
    local __ans
    [ "$NONINTERACTIVE" -eq 1 ] && return 1
    read -r -p "$1 [y/N]: " __ans || true
    case "$__ans" in [yY]|[yY][eE][sS]) return 0;; *) return 1;; esac
}

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
bold "tg-grabber bootstrap"
[ "$NONINTERACTIVE" -eq 1 ] && info "Non-interactive mode (config from environment)."
[ -f "$SCRIPT_DIR/sources.yaml" ] || die "sources.yaml not found — run this from a tg-grabber clone."
[ -f "$SCRIPT_DIR/.env.example" ] || die ".env.example not found — run this from a tg-grabber clone."
[ -f "$SCRIPT_DIR/pyproject.toml" ] || die "pyproject.toml not found — run this from a tg-grabber clone."

# Pick an interpreter to build the venv with. Debian/Ubuntu ship an older python3
# next to a versioned python3.11, so probe versioned names before falling back to python3.
PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON_BIN="$cand"
        break
    fi
done
[ -n "$PYTHON_BIN" ] || die "Python 3.11+ required (found $(python3 -V 2>&1 || echo 'no python3'))."
info "Python: $("$PYTHON_BIN" -V 2>&1) ($PYTHON_BIN)"

# ---------------------------------------------------------------------------
# 2. Venv + deps (uv when available; dependencies read from pyproject.toml)
# ---------------------------------------------------------------------------
bold "Installing dependencies"
HAVE_UV=0
command -v uv >/dev/null 2>&1 && HAVE_UV=1

if [ ! -x "$PY" ]; then
    if [ "$HAVE_UV" -eq 1 ]; then
        info "Creating venv at .venv (uv)"
        uv venv "$VENV" >/dev/null
    else
        info "Creating venv at .venv"
        "$PYTHON_BIN" -m venv "$VENV"
    fi
else
    info "Reusing existing .venv"
fi

# pyproject.toml is the single source of truth for dependencies; extract them into a
# temp requirements file (tomllib ships with Python 3.11+, which we just verified).
REQ="$(mktemp "${TMPDIR:-/tmp}/tg-grabber-reqs.XXXXXX")"
trap 'rm -f "$REQ"' EXIT
"$PY" - "$SCRIPT_DIR/pyproject.toml" > "$REQ" <<'PYEOF'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    deps = tomllib.load(f)["project"]["dependencies"]
print("\n".join(deps))
PYEOF

if [ "$HAVE_UV" -eq 1 ]; then
    uv pip install --python "$PY" --quiet -r "$REQ"
else
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$REQ"
fi
info "Dependencies installed (from pyproject.toml)."

# ---------------------------------------------------------------------------
# 3. Collect config
# ---------------------------------------------------------------------------
bold "Configuration"
if [ -f "$SCRIPT_DIR/.env" ]; then
    if [ "$NONINTERACTIVE" -eq 1 ]; then
        cp "$SCRIPT_DIR/.env" "$SCRIPT_DIR/.env.bak"
        warn ".env exists — backed up to .env.bak and regenerating (non-interactive)."
    elif confirm ".env already exists. Overwrite it? (a backup .env.bak will be saved)"; then
        cp "$SCRIPT_DIR/.env" "$SCRIPT_DIR/.env.bak"
        info "Backed up existing .env to .env.bak"
    else
        die "Keeping existing .env — edit it by hand, or move it aside and re-run."
    fi
fi

echo
info "LLM endpoint (OpenAI-compatible, or Anthropic Messages API if URL contains 'anthropic')"
ask        LLM_BASE_URL "  LLM_BASE_URL" "https://api.openai.com/v1"
ask_secret LLM_API_KEY  "  LLM_API_KEY (hidden)"
ask        LLM_MODEL    "  LLM_MODEL" "gpt-4o-mini"

echo
info "Telegram bot (create via @BotFather; see README §2)"
ask_secret TG_BOT_TOKEN "  TG_BOT_TOKEN (hidden)"
info "  Need your chat id? Message the bot, then: .venv/bin/python -m grabber --whoami"
ask        TG_CHAT_ID   "  TG_CHAT_ID"

echo
info "Optional content-source tokens (only needed for slack/discord sources)"
ask_opt SLACK_TOKEN   "  SLACK_TOKEN"
ask_opt DISCORD_TOKEN "  DISCORD_TOKEN"

echo
info "Optional SOCKS5 proxy for blocked sources (e.g. socks5h://user:pass@host:port)"
ask_opt SOCKS5_PROXY "  SOCKS5_PROXY"

echo
info "Optional pipeline tuning (enter to accept in-code defaults)"
ask_opt RELEVANCE_THRESHOLD   "  RELEVANCE_THRESHOLD (default 7)"
ask_opt MAX_LLM_ITEMS_PER_RUN "  MAX_LLM_ITEMS_PER_RUN (default 40)"
ask_opt LOOKBACK_DAYS         "  LOOKBACK_DAYS (default 3)"
ask_opt CLASSIFY_MODEL        "  CLASSIFY_MODEL (default = LLM_MODEL)"
ask_opt CLASSIFY_BATCH_SIZE   "  CLASSIFY_BATCH_SIZE (default 8)"

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------
bold "Writing .env"
ENV_FILE="$SCRIPT_DIR/.env"
{
    echo "# Generated by bootstrap.sh — edit freely; see .env.example for docs."
    echo
    echo "LLM_BASE_URL=$LLM_BASE_URL"
    echo "LLM_API_KEY=$LLM_API_KEY"
    echo "LLM_MODEL=$LLM_MODEL"
    echo
    echo "TG_BOT_TOKEN=$TG_BOT_TOKEN"
    echo "TG_CHAT_ID=$TG_CHAT_ID"
    # Optional values are written only when provided, so unset ones fall back to defaults.
    [ -n "$SLACK_TOKEN" ]           && { echo; echo "SLACK_TOKEN=$SLACK_TOKEN"; }
    [ -n "$DISCORD_TOKEN" ]         && echo "DISCORD_TOKEN=$DISCORD_TOKEN"
    [ -n "$SOCKS5_PROXY" ]          && { echo; echo "SOCKS5_PROXY=$SOCKS5_PROXY"; }
    [ -n "$RELEVANCE_THRESHOLD" ]   && { echo; echo "RELEVANCE_THRESHOLD=$RELEVANCE_THRESHOLD"; }
    [ -n "$MAX_LLM_ITEMS_PER_RUN" ] && echo "MAX_LLM_ITEMS_PER_RUN=$MAX_LLM_ITEMS_PER_RUN"
    [ -n "$LOOKBACK_DAYS" ]         && echo "LOOKBACK_DAYS=$LOOKBACK_DAYS"
    [ -n "$CLASSIFY_MODEL" ]        && echo "CLASSIFY_MODEL=$CLASSIFY_MODEL"
    [ -n "$CLASSIFY_BATCH_SIZE" ]   && echo "CLASSIFY_BATCH_SIZE=$CLASSIFY_BATCH_SIZE"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
info "Wrote $ENV_FILE (mode 600)."

# ---------------------------------------------------------------------------
# 5. Seed state (avoid first-run flood)
# ---------------------------------------------------------------------------
bold "Seeding state (marking current feed items as seen)"
"$PY" -m grabber --init
info "Done — existing items won't be sent as drafts."

# ---------------------------------------------------------------------------
# 6. Install a schedule — cron by default, systemd --user timer when asked or cron is absent
# ---------------------------------------------------------------------------
install_cron() {
    bold "Cron schedule"
    info "Default runs 4×/day. Press enter to accept, or type a custom cron expression."
    ask CRON_SCHEDULE "  Schedule" "$CRON_DEFAULT"

    local cron_line existing new_cron
    cron_line="$CRON_SCHEDULE cd $SCRIPT_DIR && $VENV/bin/python -m grabber >> $SCRIPT_DIR/grabber.log 2>&1"
    existing="$(crontab -l 2>/dev/null || true)"  # no crontab yet exits non-zero → empty
    # Drop any prior tg-grabber line, keep everything else, append the fresh one.
    new_cron="$(printf '%s\n' "$existing" | grep -v -- '-m grabber' | sed '/^$/d' || true)"
    if [ -n "$new_cron" ]; then
        new_cron="$new_cron"$'\n'"$cron_line"
    else
        new_cron="$cron_line"
    fi
    if printf '%s\n' "$new_cron" | crontab -; then
        SCHEDULE_DESC="cron: $cron_line"
        info "Installed cron entry:"
        info "  $cron_line"
    else
        SCHEDULE_DESC="cron (manual install needed)"
        warn "Could not install crontab automatically (on macOS your terminal may need"
        warn "Full Disk Access). Add this line manually with 'crontab -e':"
        warn "  $cron_line"
    fi
}

install_systemd_timer() {
    bold "systemd timer"
    ask SYSTEMD_ONCALENDAR "  OnCalendar" "$SYSTEMD_ONCALENDAR_DEFAULT"

    local unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/tg-grabber.service" <<EOF
[Unit]
Description=tg-grabber content pipeline
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PY -m grabber
EOF
    cat > "$unit_dir/tg-grabber.timer" <<EOF
[Unit]
Description=Run tg-grabber on a schedule

[Timer]
OnCalendar=$SYSTEMD_ONCALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF
    SCHEDULE_DESC="systemd user timer tg-grabber.timer ($SYSTEMD_ONCALENDAR)"
    if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null; then
        systemctl --user enable --now tg-grabber.timer
        info "Installed $SCHEDULE_DESC"
        info "Tip: 'sudo loginctl enable-linger $USER' so the timer runs without an active login."
    else
        warn "Wrote units to $unit_dir but couldn't reach 'systemctl --user'."
        warn "Enable later with:"
        warn "  systemctl --user daemon-reload && systemctl --user enable --now tg-grabber.timer"
    fi
}

SCHEDULE_DESC=""
if [ "$USE_SYSTEMD" -eq 1 ]; then
    install_systemd_timer
elif command -v crontab >/dev/null 2>&1; then
    install_cron
elif command -v systemctl >/dev/null 2>&1; then
    warn "No 'crontab' found; falling back to a systemd --user timer."
    install_systemd_timer
else
    warn "Neither crontab nor systemctl found — skipping schedule install."
    warn "Run periodically yourself: cd $SCRIPT_DIR && $VENV/bin/python -m grabber"
    SCHEDULE_DESC="none (install a schedule manually)"
fi

# ---------------------------------------------------------------------------
# 7. First run
# ---------------------------------------------------------------------------
bold "First run (dry-run — drafts printed below, nothing sent)"
"$PY" -m grabber --dry-run

echo
if [ "$NONINTERACTIVE" -eq 1 ]; then
    if [ "${SEND_ON_BOOTSTRAP:-0}" = "1" ]; then
        bold "Real run"
        "$PY" -m grabber
        info "Sent. Check your Telegram."
    else
        info "Non-interactive: skipped the real send (set SEND_ON_BOOTSTRAP=1 to send now)."
    fi
elif confirm "Send these drafts to Telegram for real now?"; then
    bold "Real run"
    "$PY" -m grabber
    info "Sent. Check your Telegram."
else
    info "Skipped. The schedule will produce the first real drafts at the next slot."
fi

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
echo
bold "All set."
info "Config:    $ENV_FILE"
info "Schedule:  $SCHEDULE_DESC"
info "Logs:      $SCRIPT_DIR/grabber.log"
info "Manual:    $VENV/bin/python -m grabber [--dry-run|--init|--whoami]"
