#!/usr/bin/env python3
"""GitHub push webhook: pull latest main and restart OpenClaw on the Mac."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(os.getenv("OPENCLAW_BASE_DIR", "/Users/evon/OpenClaw"))
DEPLOY_SCRIPT = BASE_DIR / "scripts" / "deploy_and_restart.sh"
SECRET = os.getenv("GITHUB_DEPLOY_WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("GITHUB_DEPLOY_WEBHOOK_PORT", "9876"))
DEPLOY_BRANCH = os.getenv("OPENCLAW_DEPLOY_BRANCH", "main")


def log(message: str) -> None:
    print(f"[github-deploy-webhook] {message}", flush=True)


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    if not SECRET:
        log("WARN: GITHUB_DEPLOY_WEBHOOK_SECRET is not set; rejecting requests")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def should_deploy(payload: dict) -> bool:
    ref = payload.get("ref", "")
    return ref == f"refs/heads/{DEPLOY_BRANCH}"


def run_deploy() -> int:
    if not DEPLOY_SCRIPT.is_file():
        log(f"ERROR: missing deploy script: {DEPLOY_SCRIPT}")
        return 1
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--force"],
        cwd=BASE_DIR,
        check=False,
    )
    return result.returncode


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log(fmt % args)

    def do_GET(self) -> None:
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        if self.path not in ("/", "/webhook", "/github/webhook"):
            self.send_error(404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        event = self.headers.get("X-GitHub-Event", "")
        signature = self.headers.get("X-Hub-Signature-256")

        if not verify_signature(body, signature):
            self.send_error(401, "invalid signature")
            return

        if event == "ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"message":"pong"}\n')
            return

        if event != "push":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"message":"ignored event"}\n')
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        if not should_deploy(payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"message":"ignored ref"}\n')
            return

        log(f"Push to {payload.get('ref')}; running deploy...")
        code = run_deploy()
        if code != 0:
            self.send_error(500, "deploy failed")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"message":"deployed"}\n')


def main() -> int:
    if not BASE_DIR.is_dir():
        log(f"ERROR: base dir not found: {BASE_DIR}")
        return 1
    log(f"Listening on 127.0.0.1:{PORT} (branch={DEPLOY_BRANCH})")
    server = HTTPServer(("127.0.0.1", PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
