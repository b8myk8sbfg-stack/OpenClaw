#!/bin/bash
# Install GitHub webhook listener + optional Tailscale Serve exposure.

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
PLIST_SRC="$BASE_DIR/scripts/com.openclaw.deploy-webhook.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.deploy-webhook.plist"
WEBHOOK_SCRIPT="$BASE_DIR/scripts/github_deploy_webhook.py"
ENV_FILE="$BASE_DIR/.env"
PORT="${GITHUB_DEPLOY_WEBHOOK_PORT:-9876}"

chmod +x "$WEBHOOK_SCRIPT"

ensure_secret() {
    if [[ -f "$ENV_FILE" ]] && grep -q '^GITHUB_DEPLOY_WEBHOOK_SECRET=' "$ENV_FILE" 2>/dev/null; then
        echo "  Webhook secret already present in .env"
        return
    fi
    SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
    echo "" >> "$ENV_FILE"
    echo "GITHUB_DEPLOY_WEBHOOK_SECRET=$SECRET" >> "$ENV_FILE"
    echo "GITHUB_DEPLOY_WEBHOOK_PORT=$PORT" >> "$ENV_FILE"
    echo ""
    echo "  Added GITHUB_DEPLOY_WEBHOOK_SECRET to $ENV_FILE"
    echo "  Save this secret for GitHub webhook configuration:"
    echo "    $SECRET"
}

load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        set +a
    fi
}

case "${1:-install}" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
        ensure_secret
        load_env

        chmod +x "$BASE_DIR/scripts/start_deploy_webhook.sh"
        cp "$PLIST_SRC" "$PLIST_DST"

        launchctl bootout "gui/$(id -u)/com.openclaw.deploy-webhook" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || \
            launchctl load "$PLIST_DST"

        echo ""
        echo "Installed: GitHub deploy webhook on 127.0.0.1:${PORT}"
        echo "  Health: curl http://127.0.0.1:${PORT}/health"
        echo ""
        echo "Expose via Tailscale (recommended):"
        echo "  tailscale serve --bg --https=443 http://127.0.0.1:${PORT}"
        echo ""
        echo "Then in GitHub → Settings → Webhooks → Add webhook:"
        echo "  Payload URL: https://evons-macbook-air-1.tail62128b.ts.net/webhook"
        echo "  Content type: application/json"
        echo "  Secret: value of GITHUB_DEPLOY_WEBHOOK_SECRET in .env"
        echo "  Events: Just the push event"
        launchctl list | grep openclaw.deploy-webhook || true
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/com.openclaw.deploy-webhook" 2>/dev/null || \
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        tailscale serve reset 2>/dev/null || true
        echo "Removed deploy webhook."
        ;;
    test)
        load_env
        curl -fsS "http://127.0.0.1:${PORT}/health"
        echo ""
        ;;
    *)
        echo "Usage: $0 [install|uninstall|test]"
        exit 1
        ;;
esac
