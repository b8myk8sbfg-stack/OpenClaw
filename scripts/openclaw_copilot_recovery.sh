#!/bin/bash
# Full recovery when the local Copilot server is down:
# 1) stop OpenClaw, 2) restart Copilot and wait until healthy,
# 3) start OpenClaw again.

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
RECOVERY_LOG="$LOG_DIR/copilot_recovery.log"
LOCK_FILE="$LOG_DIR/copilot_recovery.lock"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

acquire_lock() {
    if ! mkdir "$LOCK_FILE" 2>/dev/null; then
        log "Recovery already running (lock: $LOCK_FILE). Skipping."
        exit 0
    fi
}

release_lock() {
    rmdir "$LOCK_FILE" 2>/dev/null || true
}

ENV_FILE="$BASE_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

acquire_lock
trap release_lock EXIT

{
    log "=========================================="
    log "OpenClaw + Copilot recovery starting"
    log "PATH=$PATH"
    log "=========================================="

    local before_count
    before_count="$(openclaw_count_main)"
    if [[ "$before_count" -gt 0 ]]; then
        log "Found $before_count openclaw_main.py process(es) before stop"
    fi

    openclaw_stop_all
    sleep 3

    if ! copilot_restart_and_wait; then
        log "ERROR: Copilot restart failed. OpenClaw left stopped."
        exit 1
    fi

    sleep 2

    if ! openclaw_start; then
        log "ERROR: OpenClaw failed to start after Copilot recovery."
        exit 1
    fi

    log "Recovery complete."
} >> "$RECOVERY_LOG" 2>&1

tail -15 "$RECOVERY_LOG"
