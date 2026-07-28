#!/bin/bash
# Stop every OpenClaw-related process before a clean restart.
# Safe to run manually: bash scripts/kill_all_openclaw.sh

set -uo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

count_matches() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | wc -l | tr -d ' '
}

list_matches() {
    local pattern="$1"
    pgrep -fl "$pattern" 2>/dev/null || true
}

stop_pattern() {
    local pattern="$1"
    pkill -f "$pattern" 2>/dev/null || true
}

wait_gone() {
    local pattern="$1"
    local attempts="${2:-12}"
    local i
    for ((i = 1; i <= attempts; i++)); do
        if ! pgrep -f "$pattern" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

PATTERNS=(
    "$BASE_DIR/openclaw_main.py"
    "openclaw_main.py"
    "uv run python openclaw_main.py"
    "$BASE_DIR/whatsapp_inbox_watcher.py"
    "whatsapp_inbox_watcher.py"
    "$BASE_DIR/auto_claw.py"
    "auto_claw.py"
    "chromedriver"
    "chrome_whatsapp_profile"
)

log "=========================================="
log "Kill all OpenClaw processes"
log "BASE_DIR=$BASE_DIR"
log "=========================================="

for pattern in "${PATTERNS[@]}"; do
    count="$(count_matches "$pattern")"
    if [[ "$count" -gt 0 ]]; then
        log "Before stop ($count): $pattern"
        list_matches "$pattern" | sed 's/^/  /'
    fi
done

for pattern in "${PATTERNS[@]}"; do
    stop_pattern "$pattern"
done

sleep 2

for pattern in "${PATTERNS[@]}"; do
    if ! wait_gone "$pattern" 8; then
        log "Force-killing: $pattern"
        pkill -9 -f "$pattern" 2>/dev/null || true
    fi
done

sleep 1

log "Remaining processes (should be empty except tail -f log viewers):"
remaining=0
for pattern in "${PATTERNS[@]}"; do
    count="$(count_matches "$pattern")"
    if [[ "$count" -gt 0 ]]; then
        remaining=1
        log "STILL RUNNING ($count): $pattern"
        list_matches "$pattern" | sed 's/^/  /'
    fi
done

if [[ "$remaining" -eq 0 ]]; then
    log "All OpenClaw / Chrome watcher processes stopped."
else
    log "WARN: some processes may still be running — check with: pgrep -fl openclaw"
    exit 1
fi
