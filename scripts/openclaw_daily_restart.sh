#!/bin/bash
# Stops OpenClaw (email + WhatsApp) and Chrome, restarts Copilot, then starts OpenClaw fresh.
# Used by the daily 5:00 AM launchd job and can be run manually.

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_process.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/copilot_server.sh"

LOG_DIR="$BASE_DIR/logs"
MAIN_LOG="$LOG_DIR/openclaw_main.log"
RESTART_LOG="$LOG_DIR/daily_restart.log"

mkdir -p "$LOG_DIR"

ENV_FILE="$BASE_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

{
    log "=========================================="
    log "OpenClaw daily restart starting"
    log "PATH=$PATH"
    log "=========================================="

    BEFORE_COUNT="$(openclaw_count_main)"
    if [[ "$BEFORE_COUNT" -gt 0 ]]; then
        log "Found $BEFORE_COUNT openclaw_main.py process(es) before stop"
    fi

    openclaw_stop_all
    sleep 5

    log "Restarting Copilot server before OpenClaw..."
    if ! copilot_restart_and_wait; then
        log "ERROR: Copilot failed to become healthy. OpenClaw left stopped."
        exit 1
    fi

    sleep 2

    if ! openclaw_start; then
        exit 1
    fi

    log "Daily restart complete."
} >> "$RESTART_LOG" 2>&1
