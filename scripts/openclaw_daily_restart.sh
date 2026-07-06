#!/bin/bash
# Stops OpenClaw (email + WhatsApp) and Chrome, then starts fresh.
# Used by the daily 5:00 AM launchd job and can be run manually.

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"

LOG_DIR="$BASE_DIR/logs"
MAIN_LOG="$LOG_DIR/openclaw_main.log"
RESTART_LOG="$LOG_DIR/daily_restart.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

stop_pattern() {
    local pattern="$1"
    pkill -f "$pattern" 2>/dev/null || true
}

stop_openclaw_processes() {
    stop_pattern "$BASE_DIR/openclaw_main.py"
    stop_pattern "openclaw_main.py"
    stop_pattern "uv run python openclaw_main.py"
    stop_pattern "$BASE_DIR/auto_claw.py"
    stop_pattern "auto_claw.py"
    stop_pattern "$BASE_DIR/whatsapp_inbox_watcher.py"
    stop_pattern "whatsapp_inbox_watcher.py"
}

wait_for_processes_to_exit() {
    local pattern="$1"
    local attempts="${2:-15}"
    local i
    for ((i = 1; i <= attempts; i++)); do
        if ! pgrep -f "$pattern" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

count_openclaw_main() {
    pgrep -f "openclaw_main.py" 2>/dev/null | wc -l | tr -d ' '
}

start_openclaw() {
    local uv_bin
    uv_bin="$(resolve_uv_bin || true)"
    if [[ -z "$uv_bin" ]]; then
        log "ERROR: uv not found. PATH=$PATH"
        log "ERROR: Install uv or set UV_BIN in the environment."
        return 1
    fi

    log "Starting OpenClaw unified runner with: $uv_bin"
    cd "$BASE_DIR"
    nohup "$uv_bin" run python openclaw_main.py >> "$MAIN_LOG" 2>&1 &
    local launcher_pid=$!
    local i
    local after_count=0

    for ((i = 1; i <= 20; i++)); do
        sleep 1
        after_count="$(count_openclaw_main)"
        if [[ "$after_count" -ge 1 ]]; then
            log "OpenClaw started (launcher PID $launcher_pid, openclaw_main.py processes=$after_count)"
            if [[ "$after_count" -gt 2 ]]; then
                log "WARN: expected at most 2 openclaw_main.py processes (uv + python), found $after_count"
            fi
            return 0
        fi
    done

    log "ERROR: OpenClaw failed to start within 20s. Check $MAIN_LOG"
    if [[ -f "$MAIN_LOG" ]]; then
        log "Last log lines:"
        tail -5 "$MAIN_LOG" | while IFS= read -r line; do
            log "  $line"
        done
    fi
    if ps -p "$launcher_pid" >/dev/null 2>&1; then
        log "Launcher PID $launcher_pid is still running but no openclaw_main.py child was detected."
    fi
    return 1
}

{
    log "=========================================="
    log "OpenClaw daily restart starting"
    log "PATH=$PATH"
    log "=========================================="

    BEFORE_COUNT="$(count_openclaw_main)"
    if [[ "$BEFORE_COUNT" -gt 0 ]]; then
        log "Found $BEFORE_COUNT openclaw_main.py process(es) before stop"
    fi

    log "Stopping OpenClaw processes..."
    stop_openclaw_processes
    sleep 2

    if ! wait_for_processes_to_exit "openclaw_main.py" 10; then
        log "Force-killing remaining openclaw_main.py processes..."
        pkill -9 -f "openclaw_main.py" 2>/dev/null || true
        sleep 2
    fi

    REMAINING="$(count_openclaw_main)"
    if [[ "$REMAINING" -gt 0 ]]; then
        log "WARN: $REMAINING openclaw_main.py process(es) still running after stop"
    else
        log "All openclaw_main.py processes stopped"
    fi

    log "Stopping WhatsApp Chrome / chromedriver..."
    pkill -f "chrome_whatsapp_profile" 2>/dev/null || true
    pkill -f "chromedriver" 2>/dev/null || true

    sleep 5

    if ! start_openclaw; then
        exit 1
    fi

    log "Daily restart complete."
} >> "$RESTART_LOG" 2>&1
