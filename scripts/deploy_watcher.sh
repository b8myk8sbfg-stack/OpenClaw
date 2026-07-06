#!/bin/bash
# Poll GitHub for new commits on main and deploy when changed.

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
DEPLOY_SCRIPT="$BASE_DIR/scripts/deploy_and_restart.sh"

exec bash "$DEPLOY_SCRIPT"
