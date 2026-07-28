#!/bin/bash
# Install or remove the persistent Copilot server LaunchAgent.

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.openclaw.copilot-server.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.copilot-server.plist"
RUN_SCRIPT="$SCRIPT_DIR/run_copilot_server.sh"
DOMAIN="gui/$(id -u)"
LABEL="com.openclaw.copilot-server"

chmod +x "$RUN_SCRIPT"

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"
        echo "Installed: persistent Copilot server (KeepAlive)"
        echo "  Plist:   $PLIST_DST"
        echo "  Runner:  $RUN_SCRIPT"
        echo "  Logs:    $BASE_DIR/logs/copilot_server.log"
        launchctl list | grep openclaw.copilot-server || true
        ;;
    uninstall)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Removed Copilot server LaunchAgent."
        ;;
    start|restart)
        launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || {
            echo "Service not loaded — run: $0 install"
            exit 1
        }
        ;;
    stop)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        ;;
    status)
        if launchctl print "$DOMAIN/$LABEL" &>/dev/null; then
            echo "Copilot server LaunchAgent is loaded."
            launchctl print "$DOMAIN/$LABEL" | sed -n '1,12p'
        else
            echo "Copilot server LaunchAgent is not loaded."
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 [install|uninstall|start|stop|restart|status]"
        exit 1
        ;;
esac
