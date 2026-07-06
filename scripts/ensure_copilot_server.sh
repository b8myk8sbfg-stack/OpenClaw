#!/bin/bash
# Ensure the local Copilot OpenAI-compatible server is running before OpenClaw starts.
# Usage: bash scripts/ensure_copilot_server.sh

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
COPILOT_DIR="${OPENCLAW_COPILOT_DIR:-$BASE_DIR/Windows-Copilot-API}"
LOG_DIR="${OPENCLAW_LOG_DIR:-$BASE_DIR/logs}"
COPILOT_LOG="${COPILOT_LOG:-$LOG_DIR/copilot_server.log}"
COPILOT_HOST="${COPILOT_HOST:-127.0.0.1}"
COPILOT_PORT="${COPILOT_PORT:-8000}"
COPILOT_HEALTH_URL="${COPILOT_HEALTH_URL:-http://${COPILOT_HOST}:${COPILOT_PORT}/v1/models}"
COPILOT_START_TIMEOUT="${COPILOT_START_TIMEOUT:-180}"
COPILOT_FORCE_RESTART="${COPILOT_FORCE_RESTART:-0}"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [COPILOT] $*"
}

copilot_health_ok() {
    curl -sf --max-time 5 "$COPILOT_HEALTH_URL" >/dev/null 2>&1
}

find_copilot_pids() {
    pgrep -f "$COPILOT_DIR/app.py" 2>/dev/null || true
    pgrep -f "$COPILOT_DIR.*uvicorn server.api:app" 2>/dev/null || true
}

stop_copilot_if_requested() {
    if [[ "$COPILOT_FORCE_RESTART" != "1" ]]; then
        return 0
    fi
    local pids
    pids="$(find_copilot_pids | tr '\n' ' ' | xargs echo 2>/dev/null || true)"
    if [[ -z "${pids// }" ]]; then
        return 0
    fi
    log "Stopping existing Copilot server (PIDs: $pids)"
    kill $pids 2>/dev/null || true
    sleep 2
    kill -9 $pids 2>/dev/null || true
}

choose_python() {
    if [[ -n "${COPILOT_PYTHON:-}" && -x "${COPILOT_PYTHON}" ]]; then
        echo "$COPILOT_PYTHON"
        return
    fi
    if [[ -x "$COPILOT_DIR/venv/bin/python" ]]; then
        echo "$COPILOT_DIR/venv/bin/python"
        return
    fi
    if [[ -x "$BASE_DIR/.venv/bin/python" ]]; then
        echo "$BASE_DIR/.venv/bin/python"
        return
    fi
    if command -v uv >/dev/null 2>&1; then
        echo "uv run python"
        return
    fi
    echo "python3"
}

start_copilot_server() {
    if [[ ! -d "$COPILOT_DIR" ]]; then
        log "ERROR: Copilot directory not found: $COPILOT_DIR"
        exit 1
    fi
    if [[ ! -f "$COPILOT_DIR/app.py" ]]; then
        log "ERROR: Copilot entrypoint missing: $COPILOT_DIR/app.py"
        exit 1
    fi

    local py
    py="$(choose_python)"
    log "Starting Copilot server from $COPILOT_DIR using: $py"
    log "Health check URL: $COPILOT_HEALTH_URL"
    log "Log file: $COPILOT_LOG"

    (
        cd "$COPILOT_DIR"
        export HOST="$COPILOT_HOST"
        export PORT="$COPILOT_PORT"
        if [[ "$py" == "uv run python" ]]; then
            nohup uv run python app.py >> "$COPILOT_LOG" 2>&1 &
        else
            nohup "$py" app.py >> "$COPILOT_LOG" 2>&1 &
        fi
        echo $! > "$LOG_DIR/copilot_server.pid"
    )
}

wait_for_copilot_server() {
    local elapsed=0
    while (( elapsed < COPILOT_START_TIMEOUT )); do
        if copilot_health_ok; then
            log "Copilot server is healthy (${COPILOT_HEALTH_URL})"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if (( elapsed % 10 == 0 )); then
            log "Waiting for Copilot server... (${elapsed}s / ${COPILOT_START_TIMEOUT}s)"
        fi
    done
    log "ERROR: Copilot server did not become healthy within ${COPILOT_START_TIMEOUT}s"
    log "Check log: $COPILOT_LOG"
    tail -30 "$COPILOT_LOG" 2>/dev/null || true
    exit 1
}

main() {
    log "Ensuring Copilot server is running"

    if copilot_health_ok && [[ "$COPILOT_FORCE_RESTART" != "1" ]]; then
        log "Copilot server already healthy — skipping start"
        return 0
    fi

    stop_copilot_if_requested
    start_copilot_server
    wait_for_copilot_server
    log "Copilot server ready"
}

main "$@"
