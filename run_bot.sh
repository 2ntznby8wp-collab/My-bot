#!/bin/bash
# BeachManager bot supervisor.
# Auto-restarts the bot on crash with exponential backoff.
#
# Signal handling:
#   SIGTERM/SIGINT from Replit is forwarded to the python3 child process,
#   then waited — so the child shuts down cleanly before bash exits.
#   This prevents orphaned python3 processes that cause 409 Conflict on restart.

BOT_SCRIPT="/home/runner/workspace/beach-manager/bot/main.py"
RUN_STATE="/home/runner/workspace/beach-manager/bot_run_count.txt"
LOG_FILE="/home/runner/workspace/beach-manager/bot.log"
PID_FILE="/home/runner/workspace/beach-manager/bot.pid"

MIN_DELAY=5        # seconds before first restart after crash
MAX_DELAY=60       # hard cap on backoff
HEALTHY_UPTIME=300 # seconds of uptime that resets backoff

delay=$MIN_DELAY
run_count=0
BOT_PID=""         # PID of the currently-running python3 child

# ── Restore run count from previous session ───────────────────────────────────
if [ -f "$RUN_STATE" ]; then
    run_count=$(cat "$RUN_STATE" 2>/dev/null || echo 0)
fi

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

# ── SIGTERM / SIGINT handler ───────────────────────────────────────────────────
# When Replit stops the workflow it sends SIGTERM to this bash process.
# We forward it to the python3 child so it can shut down gracefully and release
# the Telegram long-poll connection before the process dies.
# Without this, python3 becomes an orphan and keeps polling → 409 Conflict.
_on_stop() {
    log "🛑 Supervisor received stop signal — forwarding SIGTERM to bot (PID ${BOT_PID:-none})..."
    if [ -n "$BOT_PID" ]; then
        kill -TERM "$BOT_PID" 2>/dev/null
        # Give python-telegram-bot up to 12 s for graceful shutdown
        for _i in $(seq 1 12); do
            sleep 1
            kill -0 "$BOT_PID" 2>/dev/null || break
        done
        # Force-kill if still alive
        kill -0 "$BOT_PID" 2>/dev/null && kill -KILL "$BOT_PID" 2>/dev/null
        wait "$BOT_PID" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    log "✅ Supervisor stopped cleanly."
    exit 0
}
trap '_on_stop' SIGTERM SIGINT

# ── Startup banner ────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════"
log "  BeachManager bot supervisor starting"
log "  Previous run count: $run_count"
log "═══════════════════════════════════════════════"

# ── Kill any stale bot instances (safety net for container reboots) ───────────
# On a fresh container start there should be none; on a within-session restart
# the SIGTERM trap above handles it. This block is a belt-and-suspenders guard.
STALE=$(pgrep -f "python3.*main\.py" 2>/dev/null)
if [ -n "$STALE" ]; then
    log "⚠️  Stale bot instance(s) found: PIDs [$STALE] — sending SIGTERM..."
    kill -TERM $STALE 2>/dev/null
    for _i in $(seq 1 12); do
        sleep 1
        STILL=$(pgrep -f "python3.*main\.py" 2>/dev/null)
        [ -z "$STILL" ] && { log "   Exited after ${_i}s."; break; }
        log "   Still alive (${_i}/12): [$STILL]"
    done
    STILL=$(pgrep -f "python3.*main\.py" 2>/dev/null)
    if [ -n "$STILL" ]; then
        log "   Force-killing: [$STILL]"
        kill -KILL $STILL 2>/dev/null
        sleep 1
    fi
    rm -f "$PID_FILE"
    log "   Stale cleanup done."
fi

# ── Main restart loop ─────────────────────────────────────────────────────────
while true; do
    run_count=$((run_count + 1))
    echo "$run_count" > "$RUN_STATE"
    start_ts=$(date +%s)

    log "▶ Starting BeachManager bot (run #${run_count})"

    # Run python3 in the BACKGROUND so the SIGTERM trap above can react while
    # we are blocked in `wait`. bash only processes traps between commands, and
    # `wait` is interruptible by signals, so this pattern works correctly.
    python3 "$BOT_SCRIPT" &
    BOT_PID=$!

    wait $BOT_PID
    EXIT_CODE=$?
    BOT_PID=""

    end_ts=$(date +%s)
    uptime_sec=$((end_ts - start_ts))

    rm -f "$PID_FILE"

    if [ $EXIT_CODE -eq 0 ]; then
        log "✅ Bot exited cleanly (code 0, uptime=${uptime_sec}s). Supervisor stopped."
        exit 0
    fi

    log "💥 Bot crashed/killed (code=$EXIT_CODE, uptime=${uptime_sec}s, run=#${run_count})"

    # Reset backoff after a healthy run, otherwise double the delay
    if [ "$uptime_sec" -ge "$HEALTHY_UPTIME" ]; then
        delay=$MIN_DELAY
        log "   Healthy run — backoff reset to ${delay}s"
    else
        delay=$((delay * 2))
        [ "$delay" -gt "$MAX_DELAY" ] && delay=$MAX_DELAY
    fi

    log "   Restarting in ${delay}s..."
    sleep "$delay"
done
