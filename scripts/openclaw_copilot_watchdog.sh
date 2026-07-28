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
CHAT_STATE_FILE="$LOG_DIR/copilot_watchdog_chat_state"
RECOVERY_SCRIPT="$SCRIPT_DIR/openclaw_copilot_recovery.sh"
CLEARANCE_SCRIPT="$SCRIPT_DIR/copilot_refresh_clearance.sh"
FAIL_THRESHOLD="${COPILOT_WATCHDOG_FAIL_THRESHOLD:-3}"
CHAT_FAIL_THRESHOLD="${COPILOT_WATCHDOG_CHAT_FAIL_THRESHOLD:-2}"

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

read_chat_fail_count() {
    if [[ -f "$CHAT_STATE_FILE" ]]; then
        tr -d '[:space:]' < "$CHAT_STATE_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

write_chat_fail_count() {
    echo "$1" > "$CHAT_STATE_FILE"
}

run_watchdog() {
    if copilot_recovery_in_cooldown; then
        log "Recovery cooldown active; skipping watchdog check"
        return 0
    fi

    if copilot_server_reachable; then
        write_fail_count 0

        if copilot_chat_ok; then
            write_chat_fail_count 0
            return 0
        fi

        chat_status="$(copilot_chat_status)"
        chat_fail_count="$(read_chat_fail_count)"
        chat_fail_count=$((chat_fail_count + 1))
        write_chat_fail_count "$chat_fail_count"

        log "Copilot chat unhealthy: /v1/models OK but chat probe HTTP $chat_status ($chat_fail_count/$CHAT_FAIL_THRESHOLD)"

        if [[ "$chat_fail_count" -lt "$CHAT_FAIL_THRESHOLD" ]]; then
            log "Waiting for another failed chat probe before auto clearance refresh"
            return 0
        fi

        write_chat_fail_count 0
        log "Auto-refreshing Cloudflare clearance (saved session preserved)"
        if bash "$CLEARANCE_SCRIPT"; then
            log "Clearance auto-refresh succeeded"
            return 0
        fi

        log "Clearance auto-refresh failed — escalating to recovery"
    else
        write_chat_fail_count 0
    fi

    fail_count="$(read_fail_count)"
    fail_count=$((fail_count + 1))
    write_fail_count "$fail_count"

    if copilot_port_listening || copilot_process_running; then
        log "Copilot unhealthy: port/process up but /v1/models unreachable ($fail_count/$FAIL_THRESHOLD)"
    else
        log "Copilot unhealthy: server not running ($fail_count/$FAIL_THRESHOLD)"
    fi

    if [[ "$fail_count" -lt "$FAIL_THRESHOLD" ]]; then
        log "Waiting for another failed check before recovery"
        return 0
    fi

    write_fail_count 0

    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/openclaw_process.sh"
    local openclaw_count
    openclaw_count="$(openclaw_count_main)"
    if [[ "$openclaw_count" -ge 1 ]]; then
        log "OpenClaw is running ($openclaw_count process(es)) — trying Copilot-only light restart"
        if copilot_light_restart; then
            copilot_mark_recovery
            log "Copilot light restart succeeded; skipped full OpenClaw recovery"
            return 0
        fi
        log "Copilot light restart failed — escalating to full OpenClaw + Copilot recovery"
    else
        log "OpenClaw not running — triggering full recovery"
    fi

    bash "$RECOVERY_SCRIPT"
}

{
    run_watchdog
} >> "$WATCHDOG_LOG" 2>&1
