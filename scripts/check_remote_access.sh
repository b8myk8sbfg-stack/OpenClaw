#!/bin/bash
# Run this ON YOUR MAC to see whether SSH / Tailscale remote restart is possible.
# Usage: bash scripts/check_remote_access.sh

set -uo pipefail

BASE_DIR="/Users/evon/OpenClaw"
OK=0
WARN=0
FAIL=0

pass() { echo "  OK   $*"; OK=$((OK + 1)); }
warn() { echo "  WARN $*"; WARN=$((WARN + 1)); }
fail() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

echo "OpenClaw remote access check"
echo "Mac: $(scutil --get ComputerName 2>/dev/null || hostname)"
echo "User: $(whoami)"
echo "Repo: $BASE_DIR"
echo ""

echo "== Tailscale =="
if command -v tailscale >/dev/null 2>&1; then
    pass "tailscale CLI installed"
    if tailscale status >/dev/null 2>&1; then
        pass "tailscale connected"
        echo ""
        tailscale status | head -20
        echo ""
        TS_IP="$(tailscale ip -4 2>/dev/null || true)"
        TS_DNS="$(tailscale status --json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    self=d.get('Self',{})
    dns=(self.get('DNSName') or '').rstrip('.')
    print(dns or self.get('HostName') or '')
except Exception:
    pass
" 2>/dev/null || true)"
        if [[ -n "$TS_IP" ]]; then
            pass "Tailscale IPv4: $TS_IP"
        else
            warn "Could not read Tailscale IPv4"
        fi
        if [[ -n "$TS_DNS" ]]; then
            pass "Tailscale DNS name: $TS_DNS"
            echo ""
            echo "  From another Tailscale device, try:"
            echo "    export OPENCLAW_SSH_HOST='$TS_DNS'"
            echo "    export OPENCLAW_SSH_USER='$(whoami)'"
            echo "    bash scripts/remote_restart_openclaw.sh"
        else
            warn "Could not read Tailscale DNS name"
        fi
    else
        fail "tailscale installed but not connected — open Tailscale app and sign in"
    fi
else
    fail "tailscale not installed"
    echo "       Install: brew install --cask tailscale"
    echo "       Then open Tailscale, sign in, and re-run this script."
fi

echo ""
echo "== SSH (Remote Login) =="
if [[ "$(uname -s)" == "Darwin" ]]; then
  REMOTE_LOGIN="$(sudo -n systemsetup -getremotelogin 2>/dev/null | awk '{print $NF}' || true)"
  if [[ "$REMOTE_LOGIN" == "On" ]]; then
      pass "macOS Remote Login is On"
  elif [[ -z "$REMOTE_LOGIN" ]]; then
      warn "Could not read Remote Login status (sudo may be required)"
      echo "       Check: System Settings → General → Sharing → Remote Login"
  else
      fail "macOS Remote Login is Off"
      echo "       Enable: System Settings → General → Sharing → Remote Login"
      echo "       Or run: sudo systemsetup -setremotelogin on"
  fi
else
  warn "Not macOS — skipping Remote Login check"
fi

if command -v sshd >/dev/null 2>&1 || [[ -f /etc/ssh/sshd_config ]]; then
    pass "SSH server files present"
else
    warn "SSH server may not be running"
fi

echo ""
echo "== SSH keys (for passwordless restart from cloud/other PC) =="
if [[ -f "$HOME/.ssh/authorized_keys" ]]; then
    KEY_COUNT="$(grep -cve '^\s*$' -e '^\s*#' "$HOME/.ssh/authorized_keys" 2>/dev/null || echo 0)"
    pass "authorized_keys exists ($KEY_COUNT key(s))"
else
    warn "No ~/.ssh/authorized_keys yet — passwordless SSH will not work until you add a public key"
fi

echo ""
echo "== OpenClaw process =="
if pgrep -fl "$BASE_DIR/openclaw_main.py" >/dev/null 2>&1; then
    pass "openclaw_main.py is running"
    pgrep -fl "$BASE_DIR/openclaw_main.py" | sed 's/^/       /'
else
    warn "openclaw_main.py is not running"
fi

if [[ -x "$BASE_DIR/scripts/openclaw_daily_restart.sh" ]]; then
    pass "restart script is executable"
else
    warn "missing or non-executable: $BASE_DIR/scripts/openclaw_daily_restart.sh"
fi

echo ""
echo "== Summary =="
echo "  OK=$OK  WARN=$WARN  FAIL=$FAIL"
echo ""
if [[ "$FAIL" -eq 0 && "$WARN" -eq 0 ]]; then
    echo "Remote restart should work after you configure OPENCLAW_SSH_HOST on the caller machine."
elif [[ "$FAIL" -eq 0 ]]; then
    echo "Remote restart is likely possible after fixing the warnings above."
else
    echo "Fix the FAIL items above, then re-run this script."
fi
