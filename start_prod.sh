#!/bin/bash
# BeachManager production entrypoint.
#
# Starts the Flask admin panel in the FOREGROUND.
#
# The Telegram bot runs exclusively via the "BeachManager Telegram Bot"
# Replit workflow (development container). Running the bot here would create
# a duplicate poller sharing the same token → Telegram 409 Conflict.
#
# Environment variables required:
#   SESSION_SECRET  — Flask session secret key
#   PORT            — set automatically by Replit deployment

if [ -z "$SESSION_SECRET" ]; then
    echo "WARNING: SESSION_SECRET is not set — using insecure default." >&2
fi

LOG_FILE="/home/runner/workspace/beach-manager/bot.log"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [PROD] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

log "═══════════════════════════════════════════════"
log "BeachManager starting (production)"
log "PORT=${PORT:-5000}"
log "Telegram bot: NOT started here (runs in dev workflow only)"
log "═══════════════════════════════════════════════"

# ── Flask admin panel in FOREGROUND ──────────────────────────────────────────
exec python /home/runner/workspace/beach-manager/admin/app.py
