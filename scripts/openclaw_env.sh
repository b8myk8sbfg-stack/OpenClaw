#!/bin/bash
# Shared environment for OpenClaw scripts run by launchd, SSH, or webhook.
# launchd provides a minimal PATH, so uv/homebrew must be resolved explicitly.

export HOME="${HOME:-/Users/evon}"
export OPENCLAW_BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"

export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [[ -f "${HOME}/.zprofile" ]]; then
    # shellcheck disable=SC1091
    source "${HOME}/.zprofile"
fi

if [[ -f "${HOME}/.zshrc" ]]; then
    # shellcheck disable=SC1091
    source "${HOME}/.zshrc"
fi

resolve_uv_bin() {
    local candidate
    for candidate in \
        "${UV_BIN:-}" \
        "${OPENCLAW_BASE_DIR}/.venv/bin/uv" \
        "${HOME}/.local/bin/uv" \
        "${HOME}/.cargo/bin/uv" \
        "/opt/homebrew/bin/uv" \
        "/usr/local/bin/uv" \
        "$(command -v uv 2>/dev/null || true)"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}
