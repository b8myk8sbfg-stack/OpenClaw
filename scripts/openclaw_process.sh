#!/bin/bash
# Stop, start, and restart the OpenClaw unified runner (email + WhatsApp).
# Copilot is managed separately — restarting OpenClaw does not restart Copilot.

set -uo pipefail

BASE_DIR="${BASE_DIR:-${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
MAIN_LOG="${MAIN_LOG:-$LOG_DIR/openclaw_main.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

openclaw_stop_processes() {
    local pattern
    for pattern in \
        "$BASE_DIR/openclaw_main.py" \
        "openclaw_main.py" \
        "uv run python openclaw_main.py" \
        "$BASE_DIR/auto_claw.py" \
        "auto_claw.py" \
        "$BASE_DIR/whatsapp_inbox_watcher.py" \
        "whatsapp_inbox_watcher.py" \
        "$BASE_DIR/purchasing_whatsapp_watcher.py" \
        "purchasing_whatsapp_watcher.py"; do
        pkill -f "$pattern" 2>/dev/null || true
    done
}

openclaw_wait_for_exit() {
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

openclaw_count_main() {
    pgrep -f "openclaw_main.py" 2>/dev/null | wc -l | tr -d ' '
}

openclaw_stop_chrome() {
    pkill -f "chrome_whatsapp_profile" 2>/dev/null || true
    pkill -f "chrome_purchasing_whatsapp_profile" 2>/dev/null || true
    pkill -f "chromedriver" 2>/dev/null || true
}

openclaw_stop_all() {
    log "Stopping OpenClaw processes..."
    openclaw_stop_processes
    sleep 2

    if ! openclaw_wait_for_exit "openclaw_main.py" 10; then
        log "Force-killing remaining openclaw_main.py processes..."
        pkill -9 -f "openclaw_main.py" 2>/dev/null || true
        sleep 2
    fi

    local remaining
    remaining="$(openclaw_count_main)"
    if [[ "$remaining" -gt 0 ]]; then
        log "WARN: $remaining openclaw_main.py process(es) still running after stop"
    else
        log "All openclaw_main.py processes stopped"
    fi

    log "Stopping WhatsApp Chrome / chromedriver..."
    openclaw_stop_chrome
}

openclaw_start() {
    local uv_bin
    uv_bin="$(resolve_uv_bin || true)"
    if [[ -z "$uv_bin" ]]; then
        log "ERROR: uv not found. PATH=$PATH"
        log "ERROR: Install uv or set UV_BIN in the environment."
        return 1
    fi

    if [[ "$(openclaw_count_main)" -ge 1 ]]; then
        log "OpenClaw already running ($(openclaw_count_main) process(es)); skipping start"
        return 0
    fi

    log "Starting OpenClaw unified runner with: $uv_bin"
    cd "$BASE_DIR"
    nohup "$uv_bin" run python openclaw_main.py >> "$MAIN_LOG" 2>&1 &
    local launcher_pid=$!
    local i
    local after_count=0

    for ((i = 1; i <= 20; i++)); do
        sleep 1
        after_count="$(openclaw_count_main)"
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

openclaw_status() {
    local count
    count="$(openclaw_count_main)"
    if [[ "$count" -ge 1 ]]; then
        log "OpenClaw running ($count openclaw_main.py process(es))"
        pgrep -fl "openclaw_main.py" 2>/dev/null | sed 's/^/  /' || true
        return 0
    fi
    log "OpenClaw is not running"
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
case "${1:-status}" in
    start)
        openclaw_start
        ;;
    stop)
        openclaw_stop_all
        ;;
    restart)
        openclaw_stop_all
        sleep 2
        openclaw_start
        ;;
    status)
        openclaw_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        echo "Log: $MAIN_LOG"
        exit 1
        ;;
esac
fi
