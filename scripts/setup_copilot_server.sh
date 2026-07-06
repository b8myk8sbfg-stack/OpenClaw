#!/bin/bash
# One-time setup for Windows-Copilot-API on your Mac.
# Usage: bash scripts/setup_copilot_server.sh

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
COPILOT_DIR="${OPENCLAW_COPILOT_DIR:-$BASE_DIR/Windows-Copilot-API}"
VENV_PY="$COPILOT_DIR/venv/bin/python"
VENV_PIP="$COPILOT_DIR/venv/bin/pip"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [COPILOT-SETUP] $*"
}

if [[ ! -d "$COPILOT_DIR" ]]; then
    log "ERROR: Copilot directory not found: $COPILOT_DIR"
    exit 1
fi

if [[ ! -x "$VENV_PY" ]]; then
    log "Creating virtualenv: $COPILOT_DIR/venv"
    python3 -m venv "$COPILOT_DIR/venv"
fi

log "Installing Python dependencies..."
"$VENV_PIP" install -r "$COPILOT_DIR/requirements.txt"

log "Installing Playwright Chromium (one-time browser download)..."
"$VENV_PY" -m playwright install chromium

if ! "$VENV_PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    log "ERROR: fastapi/uvicorn still missing after install"
    exit 1
fi

log "Checking Copilot login session..."
if ! (cd "$COPILOT_DIR" && "$VENV_PY" -m copilot ask "ping" >/dev/null 2>&1); then
    log "No active Copilot session yet. Run login next:"
    log "  cd $COPILOT_DIR"
    log "  source venv/bin/activate"
    log "  python -m copilot login"
else
    log "Copilot session looks usable."
fi

log "Setup complete. Start server with:"
log "  bash $BASE_DIR/scripts/ensure_copilot_server.sh"
