#!/bin/bash
# Long-running Copilot API entrypoint for launchd (com.openclaw.copilot-server).

set -uo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/copilot_server.sh"

python_bin="$(resolve_copilot_python || true)"
if [[ -z "$python_bin" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: no Python with fastapi for Copilot server" >&2
    exit 1
fi

cd "$COPILOT_DIR"
exec "$python_bin" main.py
