#!/bin/bash
# Install or remove the OpenClaw deploy watcher (polls GitHub every 5 minutes).

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
PLIST_SRC="$BASE_DIR/scripts/com.openclaw.deploy-watcher.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.deploy-watcher.plist"
DEPLOY_SCRIPT="$BASE_DIR/scripts/deploy_and_restart.sh"
WATCHER_SCRIPT="$BASE_DIR/scripts/deploy_watcher.sh"
ENV_SCRIPT="$BASE_DIR/scripts/openclaw_env.sh"

chmod +x "$DEPLOY_SCRIPT" "$WATCHER_SCRIPT" "$ENV_SCRIPT"

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl bootout "gui/$(id -u)/com.openclaw.deploy-watcher" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"
        echo "Installed: deploy watcher (every 5 minutes)"
        echo "  Script:  $WATCHER_SCRIPT"
        echo "  Logs:    $BASE_DIR/logs/deploy.log"
        launchctl list | grep openclaw.deploy-watcher || true
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/com.openclaw.deploy-watcher" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Removed deploy watcher."
        ;;
    test)
        echo "Running deploy now (manual test)..."
        bash "$DEPLOY_SCRIPT" --force
        ;;
    *)
        echo "Usage: $0 [install|uninstall|test]"
        exit 1
        ;;
esac
