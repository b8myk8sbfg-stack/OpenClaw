#!/bin/bash
# Refresh Cloudflare clearance for the local Copilot server without wiping login.
#
# Uses the saved session in Windows-Copilot-API/session/ (profile + token.json).
# Equivalent to the manual maintenance loop:
#   kill port 8000 -> copilot login -> copilot ask -> main.py
#
# Run manually:
#   bash /Users/evon/OpenClaw/scripts/copilot_refresh_clearance.sh
#
# Also invoked automatically by openclaw_copilot_watchdog.sh when chat returns 503.

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/copilot_server.sh"

LOG_DIR="$BASE_DIR/logs"
REFRESH_LOG="$LOG_DIR/copilot_clearance_refresh.log"
LOCK_FILE="$LOG_DIR/copilot_clearance_refresh.lock"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

acquire_lock() {
    if ! mkdir "$LOCK_FILE" 2>/dev/null; then
        log "Clearance refresh already running (lock: $LOCK_FILE). Skipping."
        exit 0
    fi
}

release_lock() {
    rmdir "$LOCK_FILE" 2>/dev/null || true
}

acquire_lock
trap release_lock EXIT

{
    log "=========================================="
    log "Copilot Cloudflare clearance refresh"
    log "Session dir: ${COPILOT_DIR}/session/"
    log "=========================================="
    if copilot_refresh_clearance; then
        log "Done."
        exit 0
    fi
    log "Failed."
    exit 1
} >> "$REFRESH_LOG" 2>&1

tail -15 "$REFRESH_LOG"
