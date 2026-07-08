#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"
ENV_FILE="$BASE_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi
cd "$BASE_DIR"
UV_BIN="$(resolve_uv_bin || true)"
if [[ -n "$UV_BIN" ]]; then exec "$UV_BIN" run python "$SCRIPT_DIR/diagnose_copilot_image.py" "$@"; fi
if [[ -x "$BASE_DIR/.venv/bin/python" ]]; then exec "$BASE_DIR/.venv/bin/python" "$SCRIPT_DIR/diagnose_copilot_image.py" "$@"; fi
echo "ERROR: uv not found" >&2; exit 1
