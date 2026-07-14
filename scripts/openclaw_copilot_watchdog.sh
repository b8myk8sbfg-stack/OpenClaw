#!/bin/bash
# Poll Copilot health and run full recovery when it is down.

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/copilot_server.sh"

LOG_DIR="$BASE_DIR/logs"
WATCHDOG_LOG="$LOG_DIR/copilot_watchdog.log"
STATE_FILE="$LOG_DIR/copilot_watchdog_state"
RECOVERY_SCRIPT="$SCRIPT_DIR/openclaw_copilot_recovery.sh"
FAIL_THRESHOLD="${COPILOT_WATCHDOG_FAIL_THRESHOLD:-2}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

read_fail_count() {
    if [[ -f "$STATE_FILE" ]]; then
        tr -d '[:space:]' < "$STATE_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

write_fail_count() {
    echo "$1" > "$STATE_FILE"
}

{
    if copilot_is_healthy; then
        write_fail_count 0
        exit 0
    fi

    local fail_count
    fail_count="$(read_fail_count)"
    fail_count=$((fail_count + 1))
    write_fail_count "$fail_count"

    if copilot_models_reachable; then
        log "Copilot unhealthy: chat probe failed ($fail_count/$FAIL_THRESHOLD)"
    elif copilot_port_listening || copilot_process_running; then
        log "Copilot unhealthy: server up but /v1/models unreachable ($fail_count/$FAIL_THRESHOLD)"
    else
        log "Copilot unhealthy: server not running ($fail_count/$FAIL_THRESHOLD)"
    fi

    if [[ "$fail_count" -lt "$FAIL_THRESHOLD" ]]; then
        log "Waiting for another failed check before recovery"
        exit 0
    fi

    log "Triggering OpenClaw + Copilot recovery..."
    write_fail_count 0
    bash "$RECOVERY_SCRIPT"
} >> "$WATCHDOG_LOG" 2>&1
