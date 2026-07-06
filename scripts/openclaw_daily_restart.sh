#!/bin/bash
# Stops OpenClaw (email + WhatsApp) and Chrome, then starts fresh.
# Used by the daily 5:00 AM launchd job and can be run manually.

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
LOG_DIR="$BASE_DIR/logs"
MAIN_LOG="$LOG_DIR/openclaw_main.log"
RESTART_LOG="$LOG_DIR/daily_restart.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

{
    log "=========================================="
    log "OpenClaw daily restart starting"
    log "=========================================="

    log "Stopping OpenClaw processes..."
    pkill -f "$BASE_DIR/openclaw_main.py" 2>/dev/null || true
    pkill -f "$BASE_DIR/auto_claw.py" 2>/dev/null || true
    pkill -f "$BASE_DIR/whatsapp_inbox_watcher.py" 2>/dev/null || true

    sleep 3

    log "Stopping WhatsApp Chrome / chromedriver..."
    pkill -f "chrome_whatsapp_profile" 2>/dev/null || true
    pkill -f "chromedriver" 2>/dev/null || true

    sleep 5

    log "Ensuring Copilot server is running before OpenClaw..."
    if ! bash "$BASE_DIR/scripts/ensure_copilot_server.sh"; then
        log "ERROR: Copilot server failed to start. OpenClaw not started."
        log "Check $LOG_DIR/copilot_server.log"
        exit 1
    fi

    log "Starting OpenClaw unified runner..."
    cd "$BASE_DIR"
    nohup uv run python openclaw_main.py >> "$MAIN_LOG" 2>&1 &
    new_pid=$!

    sleep 2

    if ps -p "$new_pid" > /dev/null 2>&1; then
        log "OpenClaw started (PID $new_pid)"
    else
        log "ERROR: OpenClaw failed to start. Check $MAIN_LOG"
        exit 1
    fi

    log "Daily restart complete."
} >> "$RESTART_LOG" 2>&1
