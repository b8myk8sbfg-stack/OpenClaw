#!/bin/bash
# Install or remove the OpenClaw Copilot watchdog LaunchAgent.

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
PLIST_SRC="$BASE_DIR/scripts/com.openclaw.copilot-watchdog.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.copilot-watchdog.plist"
WATCHDOG_SCRIPT="$BASE_DIR/scripts/openclaw_copilot_watchdog.sh"
RECOVERY_SCRIPT="$BASE_DIR/scripts/openclaw_copilot_recovery.sh"

chmod +x \
    "$WATCHDOG_SCRIPT" \
    "$RECOVERY_SCRIPT" \
    "$BASE_DIR/scripts/copilot_server.sh" \
    "$BASE_DIR/scripts/openclaw_process.sh"

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl bootout "gui/$(id -u)/com.openclaw.copilot-watchdog" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"
        echo "Installed: Copilot watchdog every 3 minutes"
        echo "  Watchdog:  $WATCHDOG_SCRIPT"
        echo "  Recovery:  $RECOVERY_SCRIPT"
        echo "  Logs:      $BASE_DIR/logs/copilot_watchdog.log"
        echo "             $BASE_DIR/logs/copilot_recovery.log"
        launchctl list | grep openclaw || true
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/com.openclaw.copilot-watchdog" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Removed Copilot watchdog schedule."
        ;;
    test-watchdog)
        echo "Running watchdog check now..."
        bash "$WATCHDOG_SCRIPT"
        tail -10 "$BASE_DIR/logs/copilot_watchdog.log"
        ;;
    test-recovery)
        echo "Running full recovery now (stops OpenClaw, restarts Copilot, restarts OpenClaw)..."
        bash "$RECOVERY_SCRIPT"
        ;;
    *)
        echo "Usage: $0 [install|uninstall|test-watchdog|test-recovery]"
        exit 1
        ;;
esac
