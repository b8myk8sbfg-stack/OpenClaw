#!/usr/bin/env python3
"""OpenClaw staff Dashboard — shows a scannable QR of the server URL.

Staff can open this page on the Mac display and scan the QR with a phone
instead of typing the Tailscale / LAN URL by hand.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg

BASE_DIR = Path(os.getenv("OPENCLAW_BASE_DIR", "/Users/evon/OpenClaw"))
PORT = int(os.getenv("OPENCLAW_DASHBOARD_PORT", "8787"))
BIND_HOST = os.getenv("OPENCLAW_DASHBOARD_BIND", "0.0.0.0")
PUBLIC_URL_ENV = os.getenv("OPENCLAW_DASHBOARD_URL", "").strip()


def log(message: str) -> None:
    print(f"[openclaw-dashboard] {message}", flush=True)


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def tailscale_dns_name() -> str:
    raw = _run(["tailscale", "status", "--json"])
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    self_info = data.get("Self") or {}
    dns = str(self_info.get("DNSName") or "").rstrip(".")
    if dns:
        return dns
    return str(self_info.get("HostName") or "").strip()


def tailscale_ipv4() -> str:
    out = _run(["tailscale", "ip", "-4"])
    return out.splitlines()[0].strip() if out else ""


def lan_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return ""


def openclaw_running() -> bool:
    out = _run(["pgrep", "-fl", str(BASE_DIR / "openclaw_main.py")])
    if out:
        return True
    # Fallback match when BASE_DIR differs between machines.
    return bool(_run(["pgrep", "-fl", "openclaw_main.py"]))


def preferred_host() -> tuple[str, str]:
    """Return (host, source_label)."""
    if PUBLIC_URL_ENV:
        parsed = urlparse(PUBLIC_URL_ENV if "://" in PUBLIC_URL_ENV else f"http://{PUBLIC_URL_ENV}")
        host = parsed.hostname or PUBLIC_URL_ENV
        return host, "OPENCLAW_DASHBOARD_URL"

    dns = tailscale_dns_name()
    if dns:
        return dns, "tailscale-dns"

    ts_ip = tailscale_ipv4()
    if ts_ip:
        return ts_ip, "tailscale-ip"

    lan = lan_ipv4()
    if lan:
        return lan, "lan-ip"

    return "127.0.0.1", "loopback"


def dashboard_url(host: Optional[str] = None) -> str:
    if PUBLIC_URL_ENV:
        if "://" in PUBLIC_URL_ENV:
            return PUBLIC_URL_ENV.rstrip("/")
        return f"http://{PUBLIC_URL_ENV.rstrip('/')}"

    resolved_host = host or preferred_host()[0]
    # Default dashboard port is shown explicitly so phone browsers open the right service.
    if PORT == 80:
        return f"http://{resolved_host}"
    if PORT == 443:
        return f"https://{resolved_host}"
    return f"http://{resolved_host}:{PORT}"


def build_info() -> dict:
    host, source = preferred_host()
    url = dashboard_url(host)
    return {
        "service": "OpenClaw Dashboard",
        "url": url,
        "host": host,
        "port": PORT,
        "url_source": source,
        "tailscale_dns": tailscale_dns_name() or None,
        "tailscale_ip": tailscale_ipv4() or None,
        "lan_ip": lan_ipv4() or None,
        "openclaw_running": openclaw_running(),
        "base_dir": str(BASE_DIR),
    }


def qr_svg_bytes(payload: str) -> bytes:
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(payload, image_factory=factory, box_size=10, border=2)
    return img.to_string()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OpenClaw Dashboard</title>
  <style>
    :root {
      --bg: #0f1a17;
      --panel: #16241f;
      --ink: #e8f2ec;
      --muted: #9bb5a8;
      --accent: #3d9a6a;
      --accent-soft: rgba(61, 154, 106, 0.18);
      --warn: #c9852c;
      --ok: #3d9a6a;
      --bad: #c45c4a;
      --line: rgba(232, 242, 236, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 10% -10%, #1d3a30 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #24352f 0%, transparent 45%),
        var(--bg);
      display: grid;
      place-items: center;
      padding: 2rem 1.25rem;
    }
    main {
      width: min(720px, 100%);
      background: color-mix(in srgb, var(--panel) 92%, black);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 2rem 1.75rem 1.75rem;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
    }
    .brand {
      font-size: 0.85rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent);
      font-weight: 600;
      margin: 0 0 0.65rem;
    }
    h1 {
      margin: 0;
      font-size: clamp(1.6rem, 4vw, 2.1rem);
      font-weight: 650;
      letter-spacing: -0.02em;
    }
    .lede {
      margin: 0.7rem 0 0;
      color: var(--muted);
      line-height: 1.45;
      max-width: 38rem;
    }
    .qr-wrap {
      margin: 1.75rem 0 1.25rem;
      display: grid;
      gap: 1.25rem;
      justify-items: center;
      text-align: center;
    }
    .qr-frame {
      background: #fff;
      border-radius: 14px;
      padding: 1rem;
      width: min(280px, 70vw);
      aspect-ratio: 1;
      display: grid;
      place-items: center;
    }
    .qr-frame object, .qr-frame img {
      width: 100%;
      height: 100%;
    }
    .url {
      font-family: "IBM Plex Mono", "SF Mono", ui-monospace, monospace;
      font-size: clamp(0.9rem, 2.5vw, 1.05rem);
      word-break: break-all;
      background: var(--accent-soft);
      border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      width: 100%;
    }
    .meta {
      display: grid;
      gap: 0.65rem;
      margin-top: 1.5rem;
      padding-top: 1.25rem;
      border-top: 1px solid var(--line);
    }
    .row {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
      font-size: 0.95rem;
    }
    .label { color: var(--muted); }
    .value { font-weight: 560; }
    .status-ok { color: var(--ok); }
    .status-bad { color: var(--bad); }
    .hint {
      margin: 1.25rem 0 0;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.4;
    }
    button.copy {
      appearance: none;
      border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
      background: transparent;
      color: var(--ink);
      border-radius: 8px;
      padding: 0.45rem 0.8rem;
      cursor: pointer;
      font: inherit;
      font-size: 0.9rem;
    }
    button.copy:hover { background: var(--accent-soft); }
    .actions { display: flex; gap: 0.6rem; flex-wrap: wrap; justify-content: center; }
  </style>
</head>
<body>
  <main>
    <p class="brand">OpenClaw</p>
    <h1>Staff Dashboard</h1>
    <p class="lede">Scan the QR code with your phone to open this server — no manual URL typing.</p>

    <div class="qr-wrap">
      <div class="qr-frame">
        <object id="qr" type="image/svg+xml" data="/qr.svg" aria-label="QR code for OpenClaw server URL">QR</object>
      </div>
      <div class="url" id="url">Loading…</div>
      <div class="actions">
        <button class="copy" type="button" id="copyBtn">Copy URL</button>
      </div>
    </div>

    <div class="meta">
      <div class="row"><span class="label">OpenClaw process</span><span class="value" id="proc">…</span></div>
      <div class="row"><span class="label">URL source</span><span class="value" id="source">…</span></div>
      <div class="row"><span class="label">Tailscale</span><span class="value" id="tailscale">…</span></div>
      <div class="row"><span class="label">LAN IP</span><span class="value" id="lan">…</span></div>
    </div>
    <p class="hint">Keep this page open on the Mac display. Phones must be on the same Tailscale network (or LAN) as this Mac.</p>
  </main>
  <script>
    async function refresh() {
      const res = await fetch("/api/info", { cache: "no-store" });
      const info = await res.json();
      document.getElementById("url").textContent = info.url;
      const proc = document.getElementById("proc");
      proc.textContent = info.openclaw_running ? "Running" : "Not running";
      proc.className = "value " + (info.openclaw_running ? "status-ok" : "status-bad");
      document.getElementById("source").textContent = info.url_source;
      document.getElementById("tailscale").textContent =
        info.tailscale_dns || info.tailscale_ip || "unavailable";
      document.getElementById("lan").textContent = info.lan_ip || "unavailable";
      document.getElementById("qr").data = "/qr.svg?v=" + encodeURIComponent(info.url);
      window.__dashboardUrl = info.url;
    }
    document.getElementById("copyBtn").addEventListener("click", async () => {
      const url = window.__dashboardUrl || document.getElementById("url").textContent;
      try {
        await navigator.clipboard.writeText(url);
        document.getElementById("copyBtn").textContent = "Copied";
        setTimeout(() => { document.getElementById("copyBtn").textContent = "Copy URL"; }, 1500);
      } catch (err) {
        document.getElementById("copyBtn").textContent = "Copy failed";
      }
    });
    refresh();
    setInterval(refresh, 15000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log(fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/dashboard"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path in ("/api/info", "/info"):
            payload = json.dumps(build_info(), indent=2).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if path in ("/qr.svg", "/qr"):
            info = build_info()
            svg = qr_svg_bytes(info["url"])
            self._send(200, svg, "image/svg+xml; charset=utf-8")
            return
        if path in ("/health", "/healthz"):
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        self.send_error(404, "not found")


def main() -> int:
    info = build_info()
    server = ThreadingHTTPServer((BIND_HOST, PORT), DashboardHandler)
    log(f"listening on http://{BIND_HOST}:{PORT}")
    log(f"staff URL (QR target): {info['url']} [{info['url_source']}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
