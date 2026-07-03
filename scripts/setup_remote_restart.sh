#!/bin/bash
# One-time Mac setup for remote OpenClaw restart over Tailscale + SSH.
# Run ON YOUR MAC: bash scripts/setup_remote_restart.sh
#
# After setup, from any Tailscale-connected machine:
#   export OPENCLAW_SSH_HOST='your-mac.tailXXXX.ts.net'
#   export OPENCLAW_SSH_USER='evon'
#   bash scripts/remote_restart_openclaw.sh

set -euo pipefail

BASE_DIR="/Users/evon/OpenClaw"
USER_NAME="$(whoami)"

echo "OpenClaw remote restart setup"
echo "=============================="

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script is intended for your Mac."
    exit 1
fi

chmod +x "$BASE_DIR/scripts/openclaw_daily_restart.sh"
chmod +x "$BASE_DIR/scripts/check_remote_access.sh"
chmod +x "$BASE_DIR/scripts/remote_restart_openclaw.sh"

echo ""
echo "1) Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "   Installing Tailscale via Homebrew..."
        brew install --cask tailscale
    else
        echo "   Install Tailscale manually: https://tailscale.com/download/mac"
        exit 1
    fi
fi

echo "   Open the Tailscale app, sign in, and keep this Mac online."
echo "   When connected, note your machine name from: tailscale status"

echo ""
echo "2) macOS Remote Login (SSH)"
echo "   Enable in: System Settings → General → Sharing → Remote Login"
read -r -p "   Press Enter after Remote Login is ON (or Ctrl+C to abort)..."

if sudo systemsetup -getremotelogin 2>/dev/null | grep -qi "On"; then
    echo "   Remote Login is On."
else
    echo "   Attempting: sudo systemsetup -setremotelogin on"
    sudo systemsetup -setremotelogin on
fi

echo ""
echo "3) SSH key for remote caller (optional but recommended)"
KEY_PATH="$HOME/.ssh/openclaw_remote_ed25519"
if [[ ! -f "$KEY_PATH" ]]; then
    read -r -p "   Create local key pair at $KEY_PATH? [Y/n] " CREATE_KEY
    CREATE_KEY="${CREATE_KEY:-Y}"
    if [[ "$CREATE_KEY" =~ ^[Yy]$ ]]; then
        mkdir -p "$HOME/.ssh"
        chmod 700 "$HOME/.ssh"
        ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "openclaw-remote-restart"
        touch "$HOME/.ssh/authorized_keys"
        chmod 600 "$HOME/.ssh/authorized_keys"
        grep -qF "$(cat "${KEY_PATH}.pub")" "$HOME/.ssh/authorized_keys" 2>/dev/null || \
            cat "${KEY_PATH}.pub" >> "$HOME/.ssh/authorized_keys"
        echo "   Public key added to ~/.ssh/authorized_keys"
        echo ""
        echo "   Copy the PRIVATE key to your cloud/other machine:"
        echo "     ${KEY_PATH}"
        echo "   Then on that machine:"
        echo "     export OPENCLAW_SSH_KEY='${KEY_PATH}'"
    fi
else
    echo "   Key already exists: $KEY_PATH"
fi

echo ""
echo "4) Connectivity check"
bash "$BASE_DIR/scripts/check_remote_access.sh"

echo ""
echo "Setup complete."
echo ""
echo "On a remote machine (also on Tailscale), configure:"
echo "  export OPENCLAW_SSH_HOST='<your-mac-tailscale-dns-name>'"
echo "  export OPENCLAW_SSH_USER='${USER_NAME}'"
echo "  export OPENCLAW_SSH_KEY='~/.ssh/openclaw_remote_ed25519'   # if you created the key"
echo "  bash scripts/remote_restart_openclaw.sh"
