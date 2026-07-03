#!/bin/bash
# Restart OpenClaw on your Mac from another machine (cloud VM, laptop, phone SSH app).
#
# Prerequisites (on the Mac):
#   - Tailscale connected
#   - Remote Login (SSH) enabled
#   - scripts/openclaw_daily_restart.sh present
#
# Usage:
#   export OPENCLAW_SSH_HOST='your-mac.tailXXXX.ts.net'   # or Tailscale IP 100.x.x.x
#   export OPENCLAW_SSH_USER='evon'
#   export OPENCLAW_SSH_KEY='$HOME/.ssh/openclaw_remote_ed25519'   # optional
#   bash scripts/remote_restart_openclaw.sh
#
# Optional:
#   OPENCLAW_SSH_PORT=22
#   OPENCLAW_BASE_DIR=/Users/evon/OpenClaw

set -euo pipefail

HOST="${OPENCLAW_SSH_HOST:-}"
USER_NAME="${OPENCLAW_SSH_USER:-evon}"
PORT="${OPENCLAW_SSH_PORT:-22}"
KEY="${OPENCLAW_SSH_KEY:-}"
BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
RESTART_SCRIPT="${BASE_DIR}/scripts/openclaw_daily_restart.sh"

if [[ -z "$HOST" ]]; then
    echo "ERROR: Set OPENCLAW_SSH_HOST to your Mac Tailscale DNS name or IP."
    echo ""
    echo "Run on the Mac first:"
    echo "  bash scripts/check_remote_access.sh"
    exit 1
fi

SSH_OPTS=(
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o StrictHostKeyChecking=accept-new
    -p "$PORT"
)

if [[ -n "$KEY" ]]; then
    SSH_OPTS+=(-i "$KEY")
fi

TARGET="${USER_NAME}@${HOST}"
echo "OpenClaw remote restart"
echo "  Target: $TARGET"
echo "  Script: $RESTART_SCRIPT"
echo ""

echo "Testing SSH connectivity..."
if ! ssh "${SSH_OPTS[@]}" "$TARGET" 'echo connected && uname -a'; then
    echo ""
    echo "SSH failed. On your Mac, run:"
    echo "  bash scripts/check_remote_access.sh"
    echo "  bash scripts/setup_remote_restart.sh"
    exit 1
fi

echo ""
echo "Restarting OpenClaw on Mac..."
ssh "${SSH_OPTS[@]}" "$TARGET" "bash '$RESTART_SCRIPT'"

echo ""
echo "Done. Tail restart log:"
ssh "${SSH_OPTS[@]}" "$TARGET" "tail -8 '${BASE_DIR}/logs/daily_restart.log' 2>/dev/null || true"
