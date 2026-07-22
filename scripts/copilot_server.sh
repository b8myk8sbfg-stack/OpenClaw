#!/bin/bash
# Start, stop, and health-check the local Windows-Copilot-API server.

BASE_DIR="${BASE_DIR:-${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"

COPILOT_DIR="${COPILOT_DIR:-$BASE_DIR/Windows-Copilot-API}"
COPILOT_HOST="${COPILOT_HOST:-127.0.0.1}"
COPILOT_PORT="${COPILOT_PORT:-8000}"
COPILOT_BASE_URL="${COPILOT_BASE_URL:-http://${COPILOT_HOST}:${COPILOT_PORT}}"
COPILOT_LOG="${COPILOT_LOG:-$LOG_DIR/copilot_server.log}"
COPILOT_PID_FILE="${COPILOT_PID_FILE:-$LOG_DIR/copilot_server.pid}"
COPILOT_HEALTH_ATTEMPTS="${COPILOT_HEALTH_ATTEMPTS:-24}"
COPILOT_HEALTH_INTERVAL="${COPILOT_HEALTH_INTERVAL:-5}"
COPILOT_RECOVERY_COOLDOWN="${COPILOT_RECOVERY_COOLDOWN:-900}"
COPILOT_LAST_RECOVERY_FILE="${COPILOT_LAST_RECOVERY_FILE:-$LOG_DIR/copilot_last_recovery.ts}"
COPILOT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COPILOT_LAUNCH_LABEL="${COPILOT_LAUNCH_LABEL:-com.openclaw.copilot-server}"
COPILOT_PLIST_SRC="${COPILOT_PLIST_SRC:-$COPILOT_SCRIPT_DIR/com.openclaw.copilot-server.plist}"
COPILOT_PLIST_DST="${COPILOT_PLIST_DST:-$HOME/Library/LaunchAgents/com.openclaw.copilot-server.plist}"
COPILOT_RUN_SCRIPT="${COPILOT_RUN_SCRIPT:-$COPILOT_SCRIPT_DIR/run_copilot_server.sh}"

# macOS ships /usr/bin/log — define our own so sourced calls don't hit the system tool.
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

resolve_copilot_python() {
    local candidate
    for candidate in \
        "${COPILOT_PYTHON:-}" \
        "${COPILOT_DIR}/venv/bin/python" \
        "${COPILOT_DIR}/.venv/bin/python" \
        "/opt/homebrew/bin/python3.14" \
        "/opt/homebrew/bin/python3" \
        "${HOME}/.local/bin/python3" \
        "/usr/local/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"; do
        if [[ -n "$candidate" && -x "$candidate" ]] \
            && "$candidate" -c "import fastapi" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

copilot_process_running() {
    if [[ -f "$COPILOT_PID_FILE" ]]; then
        local pid
        pid="$(tr -d '[:space:]' < "$COPILOT_PID_FILE" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
            return 0
        fi
    fi
    pgrep -f "$COPILOT_DIR/main.py" >/dev/null 2>&1 \
        || pgrep -f "Windows-Copilot-API/main.py" >/dev/null 2>&1 \
        || pgrep -f "Windows-Copilot-API.*main.py" >/dev/null 2>&1
}

copilot_port_listening() {
    lsof -nP -iTCP:"$COPILOT_PORT" -sTCP:LISTEN >/dev/null 2>&1
}

copilot_models_reachable() {
    curl -sf --max-time 5 "${COPILOT_BASE_URL}/v1/models" >/dev/null 2>&1
}

# Lightweight check for the watchdog — avoids chat probes that compete with OpenClaw.
copilot_server_reachable() {
    copilot_models_reachable
}

copilot_chat_status() {
    curl -sS --max-time 30 -o /dev/null -w '%{http_code}' \
        -X POST "${COPILOT_BASE_URL}/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d '{"model":"copilot","messages":[{"role":"user","content":"Reply with exactly: ok"}],"max_tokens":8}' \
        2>/dev/null || echo "000"
}

copilot_chat_ok() {
    [[ "$(copilot_chat_status)" == "200" ]]
}

copilot_server_up() {
    copilot_port_listening && copilot_models_reachable
}

copilot_is_healthy() {
    copilot_server_reachable
}

copilot_recovery_in_cooldown() {
    local now last elapsed
    [[ -f "$COPILOT_LAST_RECOVERY_FILE" ]] || return 1
    now="$(date +%s)"
    last="$(tr -d '[:space:]' < "$COPILOT_LAST_RECOVERY_FILE" 2>/dev/null || echo 0)"
    elapsed=$((now - last))
    [[ "$elapsed" -lt "$COPILOT_RECOVERY_COOLDOWN" ]]
}

copilot_mark_recovery() {
    date +%s > "$COPILOT_LAST_RECOVERY_FILE"
}

copilot_launchd_domain() {
    echo "gui/$(id -u)"
}

copilot_launchd_target() {
    echo "$(copilot_launchd_domain)/$COPILOT_LAUNCH_LABEL"
}

copilot_launchd_loaded() {
    launchctl print "$(copilot_launchd_target)" >/dev/null 2>&1
}

copilot_record_pid_from_port() {
    local pid
    pid="$(lsof -tiTCP:"$COPILOT_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    if [[ -n "$pid" ]]; then
        echo "$pid" > "$COPILOT_PID_FILE"
    fi
}

copilot_install_launch_agent() {
    if [[ ! -f "$COPILOT_PLIST_SRC" ]]; then
        log "WARN: missing LaunchAgent plist: $COPILOT_PLIST_SRC"
        return 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
    chmod +x "$COPILOT_RUN_SCRIPT" 2>/dev/null || true
    cp "$COPILOT_PLIST_SRC" "$COPILOT_PLIST_DST"

    if copilot_launchd_loaded; then
        return 0
    fi

    launchctl bootstrap "$(copilot_launchd_domain)" "$COPILOT_PLIST_DST" 2>/dev/null || \
        launchctl load "$COPILOT_PLIST_DST"
}

copilot_unload_launch_agent() {
    if [[ ! -f "$COPILOT_PLIST_DST" ]] && ! copilot_launchd_loaded; then
        return 0
    fi

    launchctl bootout "$(copilot_launchd_target)" 2>/dev/null || \
        launchctl unload "$COPILOT_PLIST_DST" 2>/dev/null || true
}

copilot_start_launch_agent() {
    if ! copilot_install_launch_agent; then
        return 1
    fi

    launchctl kickstart -k "$(copilot_launchd_target)" 2>/dev/null || true
    return 0
}

copilot_session_present() {
    [[ -f "$COPILOT_DIR/session/token.json" && -d "$COPILOT_DIR/session/profile" ]]
}

copilot_needs_clearance_refresh() {
    copilot_models_reachable && ! copilot_chat_ok
}

# Re-earn Cloudflare clearance using the saved browser profile (no session wipe).
copilot_refresh_clearance() {
    local python_bin refresh_log
    python_bin="$(resolve_copilot_python || true)"
    refresh_log="${COPILOT_CLEARANCE_LOG:-$LOG_DIR/copilot_clearance_refresh.log}"

    if [[ -z "$python_bin" ]]; then
        log "ERROR: no Python with fastapi found for Copilot clearance refresh"
        return 1
    fi

    if ! copilot_session_present; then
        log "ERROR: no saved Copilot session at ${COPILOT_DIR}/session/"
        log "ERROR: run once manually: cd ${COPILOT_DIR} && ./venv/bin/python -m copilot login"
        return 1
    fi

    mkdir -p "$LOG_DIR"
    log "Refreshing Cloudflare clearance (keeping saved login session)..."

    copilot_stop_server || true
    sleep 1

    log "Step 1/3: copilot login (cached sign-in + warm-up)..."
    if ! (
        cd "$COPILOT_DIR"
        "$python_bin" -m copilot login
    ) >> "$refresh_log" 2>&1; then
        log "ERROR: copilot login failed — see $refresh_log"
        tail -5 "$refresh_log" | while IFS= read -r line; do log "  $line"; done
        return 1
    fi

    log "Step 2/3: copilot ask verification probe..."
    if ! (
        cd "$COPILOT_DIR"
        "$python_bin" -m copilot ask "Reply: ok"
    ) >> "$refresh_log" 2>&1; then
        log "ERROR: copilot ask failed after login — see $refresh_log"
        tail -8 "$refresh_log" | while IFS= read -r line; do log "  $line"; done
        return 1
    fi

    log "Step 3/3: restarting Copilot server..."
    if ! copilot_start_server; then
        return 1
    fi

    sleep 2
    if copilot_chat_ok; then
        log "Cloudflare clearance refresh succeeded — chat probe OK"
        copilot_mark_recovery
        return 0
    fi

    local chat_status
    chat_status="$(copilot_chat_status)"
    log "WARN: clearance refresh finished but chat probe returned HTTP $chat_status"
    return 1
}

copilot_stop_server() {
    log "Stopping Copilot server..."

    copilot_unload_launch_agent

    if [[ -f "$COPILOT_PID_FILE" ]]; then
        local pid
        pid="$(tr -d '[:space:]' < "$COPILOT_PID_FILE" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
            kill "$pid" 2>/dev/null || true
            sleep 2
            if ps -p "$pid" >/dev/null 2>&1; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$COPILOT_PID_FILE"
    fi

    pkill -f "$COPILOT_DIR/main.py" 2>/dev/null || true
    pkill -f "Windows-Copilot-API/main.py" 2>/dev/null || true
    pkill -f "Windows-Copilot-API.*main.py" 2>/dev/null || true

    if copilot_port_listening; then
        local port_pids
        port_pids="$(lsof -tiTCP:"$COPILOT_PORT" -sTCP:LISTEN 2>/dev/null || true)"
        if [[ -n "$port_pids" ]]; then
            log "Force-killing process(es) listening on port $COPILOT_PORT: $port_pids"
            # shellcheck disable=SC2086
            kill $port_pids 2>/dev/null || true
            sleep 2
            # shellcheck disable=SC2086
            kill -9 $port_pids 2>/dev/null || true
        fi
    fi

    sleep 1
    if copilot_port_listening; then
        log "WARN: port $COPILOT_PORT is still in use after stop"
        return 1
    fi

    log "Copilot server stopped"
    return 0
}

# Start Copilot in a new session so it survives LaunchAgent watchdog/recovery exit.
# Plain `nohup ... &` from a short-lived launchd job is killed when the job finishes.
copilot_launch_detached() {
    local python_bin="$1"
    local pid

    if command -v setsid >/dev/null 2>&1; then
        setsid "$python_bin" main.py >> "$COPILOT_LOG" 2>&1 </dev/null &
        pid=$!
    else
        # bash -c + exec keeps one PID (stored in copilot_server.pid).
        nohup bash -c "cd \"$COPILOT_DIR\" && exec \"$python_bin\" main.py" \
            >> "$COPILOT_LOG" 2>&1 </dev/null &
        pid=$!
    fi

    disown -h "$pid" 2>/dev/null || disown "$pid" 2>/dev/null || true
    echo "$pid"
}

copilot_start_server() {
    local python_bin
    python_bin="$(resolve_copilot_python || true)"
    if [[ -z "$python_bin" ]]; then
        log "ERROR: no Python with fastapi found for Copilot server"
        log "ERROR: expected venv at ${COPILOT_DIR}/venv — run: cd ${COPILOT_DIR} && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
        return 1
    fi

    if [[ ! -f "$COPILOT_DIR/main.py" ]]; then
        log "ERROR: missing $COPILOT_DIR/main.py"
        return 1
    fi

    if copilot_server_reachable; then
        log "Copilot server already reachable on ${COPILOT_BASE_URL}; skipping start"
        return 0
    fi

    mkdir -p "$LOG_DIR"

    if [[ -f "$COPILOT_PLIST_SRC" ]]; then
        log "Starting Copilot server via LaunchAgent (KeepAlive)..."
        if ! copilot_start_launch_agent; then
            log "WARN: LaunchAgent start failed — falling back to detached process"
        else
            local i
            for ((i = 1; i <= 20; i++)); do
                sleep 1
                if copilot_port_listening; then
                    copilot_record_pid_from_port
                    log "Copilot LaunchAgent listening on ${COPILOT_BASE_URL}"
                    return 0
                fi
            done
            log "WARN: LaunchAgent started but port $COPILOT_PORT not ready within 20s"
        fi
    fi

    log "Starting Copilot server (detached fallback) with: $python_bin"
    local pid
    pid="$(copilot_launch_detached "$python_bin")"
    echo "$pid" > "$COPILOT_PID_FILE"
    log "Copilot detached PID $pid (log: $COPILOT_LOG)"

    local i
    for ((i = 1; i <= 6; i++)); do
        sleep 1
        if copilot_port_listening; then
            return 0
        fi
    done

    log "ERROR: Copilot did not bind to port $COPILOT_PORT within 6s"
    if [[ -f "$COPILOT_LOG" ]]; then
        tail -8 "$COPILOT_LOG" | while IFS= read -r line; do
            log "  $line"
        done
    fi
    rm -f "$COPILOT_PID_FILE"
    return 1
}

copilot_wait_until_healthy() {
    local attempts="${1:-$COPILOT_HEALTH_ATTEMPTS}"
    local interval="${2:-$COPILOT_HEALTH_INTERVAL}"
    local chat_fail_streak=0
    local i

    for ((i = 1; i <= attempts; i++)); do
        if copilot_server_reachable; then
            if copilot_chat_ok; then
                log "Copilot healthy after ${i} check(s)"
                return 0
            fi

            local chat_status
            chat_status="$(copilot_chat_status)"
            chat_fail_streak=$((chat_fail_streak + 1))
            log "Copilot reachable but chat probe failed with HTTP $chat_status (attempt $i/$attempts, streak=$chat_fail_streak)"

            if [[ "$chat_status" == "503" || "$chat_status" == "502" ]] && [[ "$chat_fail_streak" -ge 3 ]]; then
                log "WARN: Copilot server is up but upstream chat is failing ($chat_status)."
                log "WARN: Run: cd ${COPILOT_DIR} && ./venv/bin/python -m copilot login"
                log "WARN: Continuing anyway so OpenClaw can start (may fall back to OpenAI)."
                return 0
            fi

            sleep "$interval"
            continue
        fi

        if copilot_port_listening || copilot_process_running; then
            log "Copilot starting... models not ready yet (attempt $i/$attempts)"
        else
            log "Copilot process/port not ready (attempt $i/$attempts)"
        fi
        chat_fail_streak=0
        sleep "$interval"
    done

    if copilot_server_reachable; then
        log "WARN: Copilot /v1/models is reachable but chat never returned 200."
        log "WARN: Continuing anyway so OpenClaw can start."
        return 0
    fi

    log "ERROR: Copilot did not become reachable within $((attempts * interval))s"
    return 1
}

copilot_wait_until_models() {
    local attempts="${1:-12}"
    local interval="${2:-5}"
    local i

    for ((i = 1; i <= attempts; i++)); do
        if copilot_models_reachable; then
            log "Copilot /v1/models reachable after ${i} check(s)"
            return 0
        fi
        if copilot_port_listening || copilot_process_running; then
            log "Copilot starting... models not ready yet (attempt $i/$attempts)"
        else
            log "Copilot process/port not ready (attempt $i/$attempts)"
        fi
        sleep "$interval"
    done

    log "ERROR: Copilot /v1/models did not become reachable within $((attempts * interval))s"
    return 1
}

# Restart Copilot only — leaves OpenClaw and WhatsApp Chrome running.
copilot_light_restart() {
    if copilot_models_reachable && copilot_chat_ok; then
        log "Copilot already healthy; skipping light restart"
        return 0
    fi

    if copilot_needs_clearance_refresh; then
        log "Copilot /v1/models OK but chat failing — trying clearance refresh first"
        if copilot_refresh_clearance; then
            return 0
        fi
        log "Clearance refresh failed — falling back to process restart"
    elif copilot_models_reachable; then
        log "Copilot models already reachable; skipping light restart"
        return 0
    fi

    log "Light Copilot restart (OpenClaw left running)..."
    copilot_stop_server || true
    sleep 2
    if ! copilot_start_server; then
        return 1
    fi
    copilot_wait_until_models 12 5
}

copilot_restart_and_wait() {
    if copilot_server_reachable && copilot_chat_ok; then
        log "Copilot already healthy; skipping restart"
        return 0
    fi

    if copilot_needs_clearance_refresh; then
        log "Copilot reachable but chat unhealthy — trying clearance refresh"
        if copilot_refresh_clearance; then
            return 0
        fi
        log "Clearance refresh failed — restarting Copilot process"
    elif copilot_server_reachable; then
        log "Copilot already reachable; skipping restart"
        return 0
    fi

    copilot_stop_server || true
    sleep 2
    if ! copilot_start_server; then
        return 1
    fi
    if [[ "${COPILOT_HEALTH_CHAT_PROBE:-0}" == "1" ]]; then
        copilot_wait_until_healthy
    else
        copilot_wait_until_models "${COPILOT_HEALTH_ATTEMPTS:-12}" "${COPILOT_HEALTH_INTERVAL:-5}"
    fi
}
