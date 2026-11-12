#!/bin/bash
# Install / manage the OpenClaw staff Dashboard (QR + server URL).
# Run ON THE MAC: bash scripts/install_openclaw_dashboard.sh

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
PLIST_SRC="$BASE_DIR/scripts/com.openclaw.dashboard.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.dashboard.plist"
PORT="${OPENCLAW_DASHBOARD_PORT:-8787}"

chmod +x "$BASE_DIR/scripts/openclaw_dashboard.py"
chmod +x "$BASE_DIR/scripts/start_openclaw_dashboard.sh"

# Ensure qrcode is available in the project venv.
if [[ -x "$BASE_DIR/.venv/bin/python" ]]; then
    "$BASE_DIR/.venv/bin/python" -c "import qrcode" 2>/dev/null || {
        if command -v uv >/dev/null 2>&1; then
            (cd "$BASE_DIR" && uv sync)
        else
            "$BASE_DIR/.venv/bin/pip" install qrcode
        fi
    }
fi

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        cp "$PLIST_SRC" "$PLIST_DST"

        launchctl bootout "gui/$(id -u)/com.openclaw.dashboard" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"

        echo ""
        echo "Installed: OpenClaw staff Dashboard on 0.0.0.0:${PORT}"
        echo "  Local:   http://127.0.0.1:${PORT}/"
        echo "  Health:  curl http://127.0.0.1:${PORT}/health"
        echo ""
        echo "Optional overrides in .env:"
        echo "  OPENCLAW_DASHBOARD_PORT=8787"
        echo "  OPENCLAW_DASHBOARD_URL=http://evons-macbook-air-1.tail62128b.ts.net:8787"
        echo ""
        echo "Open the Dashboard on the Mac display so staff can scan the QR."
        launchctl list | grep openclaw.dashboard || true
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/com.openclaw.dashboard" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Removed OpenClaw staff Dashboard."
        ;;
    test)
        curl -fsS "http://127.0.0.1:${PORT}/health"
        echo ""
        curl -fsS "http://127.0.0.1:${PORT}/api/info"
        echo ""
        ;;
    *)
        echo "Usage: $0 [install|uninstall|test]"
        exit 1
        ;;
esac
