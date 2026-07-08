#!/bin/bash
# Replay a saved image through Copilot extraction using OpenClaw's uv environment.
#
# Usage:
#   bash scripts/replay_copilot_image.sh /path/to/image_full.jpg --caption "PLS QUOTE"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"

ENV_FILE="$BASE_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

cd "$BASE_DIR"

UV_BIN="$(resolve_uv_bin || true)"
if [[ -n "$UV_BIN" ]]; then
    exec "$UV_BIN" run python "$SCRIPT_DIR/replay_copilot_image.py" "$@"
fi

if [[ -x "$BASE_DIR/.venv/bin/python" ]]; then
    exec "$BASE_DIR/.venv/bin/python" "$SCRIPT_DIR/replay_copilot_image.py" "$@"
fi

echo "ERROR: OpenClaw Python environment not found." >&2
echo "Install uv, then from $BASE_DIR run: uv sync" >&2
echo "Or run: uv run python scripts/replay_copilot_image.py <image> [--caption ...]" >&2
exit 1
