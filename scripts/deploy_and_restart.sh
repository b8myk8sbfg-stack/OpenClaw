#!/bin/bash
# Pull latest code from GitHub and restart OpenClaw.
# Used by the deploy watcher, GitHub webhook, and manual runs.

set -euo pipefail

BASE_DIR="${OPENCLAW_BASE_DIR:-/Users/evon/OpenClaw}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/openclaw_env.sh"

DEPLOY_REMOTE="${OPENCLAW_DEPLOY_REMOTE:-origin}"
DEPLOY_BRANCH="${OPENCLAW_DEPLOY_BRANCH:-main}"
DEPLOY_REF="${DEPLOY_REMOTE}/${DEPLOY_BRANCH}"
LOG_DIR="$BASE_DIR/logs"
DEPLOY_LOG="$LOG_DIR/deploy.log"
SHA_FILE="$BASE_DIR/.openclaw_deploy_sha"
RESTART_SCRIPT="$BASE_DIR/scripts/openclaw_daily_restart.sh"
FORCE=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

usage() {
    echo "Usage: $0 [--force]"
    echo "  Pull ${DEPLOY_REF} and restart OpenClaw."
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

mkdir -p "$LOG_DIR"

{
    log "=========================================="
    log "OpenClaw deploy starting (ref=${DEPLOY_REF}, force=${FORCE})"
    log "=========================================="

    cd "$BASE_DIR"

    if [[ ! -d .git ]]; then
        log "ERROR: $BASE_DIR is not a git repository"
        exit 1
    fi

    log "Fetching ${DEPLOY_REMOTE}..."
    git fetch "$DEPLOY_REMOTE" "$DEPLOY_BRANCH"

    REMOTE_SHA="$(git rev-parse "$DEPLOY_REF")"
    LAST_SHA="$(cat "$SHA_FILE" 2>/dev/null || true)"

    if [[ "$FORCE" -eq 0 && "$REMOTE_SHA" == "$LAST_SHA" ]]; then
        log "No new commits on ${DEPLOY_REF} (${REMOTE_SHA:0:8}). Skipping."
        exit 0
    fi

    log "Deploying ${REMOTE_SHA:0:8} (previous: ${LAST_SHA:-none})"

    STASHED=0
    if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
        log "Stashing local changes before deploy..."
        git stash push -u -m "openclaw-auto-deploy $(date '+%Y-%m-%d %H:%M:%S')" >/dev/null
        STASHED=1
    fi

    CURRENT_BRANCH="$(git branch --show-current || true)"
    if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
        log "Switching branch: ${CURRENT_BRANCH:-detached} -> ${DEPLOY_BRANCH}"
        if git show-ref --verify --quiet "refs/heads/${DEPLOY_BRANCH}"; then
            git checkout "$DEPLOY_BRANCH"
        else
            git checkout -b "$DEPLOY_BRANCH" "$DEPLOY_REF"
        fi
    fi

    log "Fast-forwarding to ${DEPLOY_REF}..."
    git merge --ff-only "$DEPLOY_REF"

    log "Restarting OpenClaw..."
    if ! bash "$RESTART_SCRIPT"; then
        log "ERROR: Restart failed after deploy. OpenClaw may be stopped."
        exit 1
    fi

    echo "$REMOTE_SHA" > "$SHA_FILE"

    log "Deploy complete."
    if [[ "$STASHED" -eq 1 ]]; then
        log "NOTE: Local changes were stashed. Recover with: git stash list && git stash pop"
    fi
    if [[ -n "$CURRENT_BRANCH" && "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
        log "NOTE: Switched from ${CURRENT_BRANCH} to ${DEPLOY_BRANCH} for deploy."
    fi
} >> "$DEPLOY_LOG" 2>&1

tail -20 "$DEPLOY_LOG"
