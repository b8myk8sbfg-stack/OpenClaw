#!/bin/bash
# Launchd entrypoint: load .env and start the OpenClaw staff Dashboard.

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
ENV_FILE="$BASE_DIR/.env"

# shellcheck source=/dev/null
source "$BASE_DIR/scripts/openclaw_env.sh"

if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

if [[ -x "$BASE_DIR/.venv/bin/python" ]]; then
    PYTHON="$BASE_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

exec "$PYTHON" "$BASE_DIR/scripts/openclaw_dashboard.py"
