#!/bin/bash
# Install or remove the OpenClaw daily 5:00 AM restart LaunchAgent.

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
PLIST_SRC="$BASE_DIR/scripts/com.openclaw.daily-restart.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.daily-restart.plist"
RESTART_SCRIPT="$BASE_DIR/scripts/openclaw_daily_restart.sh"
ENV_SCRIPT="$BASE_DIR/scripts/openclaw_env.sh"

chmod +x "$RESTART_SCRIPT" "$ENV_SCRIPT"

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl bootout "gui/$(id -u)/com.openclaw.daily-restart" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"
        echo "Installed: daily restart at 5:00 AM"
        echo "  Script:  $RESTART_SCRIPT"
        echo "  Logs:    $BASE_DIR/logs/daily_restart.log"
        launchctl list | grep openclaw || true
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/com.openclaw.daily-restart" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Removed daily restart schedule."
        ;;
    test)
        echo "Running restart script now (manual test)..."
        bash "$RESTART_SCRIPT"
        tail -5 "$BASE_DIR/logs/daily_restart.log"
        ;;
    *)
        echo "Usage: $0 [install|uninstall|test]"
        exit 1
        ;;
esac
