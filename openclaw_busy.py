"""
OpenClaw busy / turn flags — coordinate WhatsApp and Email without fixed timers.

Files (under /Users/evon/OpenClaw):
  openclaw_busy.flag        {"busy": 1, "channel": "whatsapp", "task": "...", "since": "..."}
  openclaw_channel_turn.flag  "whatsapp" | "email"
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Optional

BASE_DIR = "/Users/evon/OpenClaw"
BUSY_FLAG_FILE = os.path.join(BASE_DIR, "openclaw_busy.flag")
CHANNEL_TURN_FILE = os.path.join(BASE_DIR, "openclaw_channel_turn.flag")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def get_channel_turn(default: str = "whatsapp") -> str:
    try:
        with open(CHANNEL_TURN_FILE, "r", encoding="utf-8") as handle:
            value = (handle.read() or "").strip().lower()
        return value if value in ("whatsapp", "email") else default
    except Exception:
        return default


def set_channel_turn(channel: str) -> None:
    channel = str(channel or "").strip().lower()
    if channel not in ("whatsapp", "email"):
        return
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(CHANNEL_TURN_FILE, "w", encoding="utf-8") as handle:
        handle.write(channel)
    print(f"🔁 [BUSY] Channel turn → {channel}")


def flip_channel_turn() -> str:
    nxt = "email" if get_channel_turn() == "whatsapp" else "whatsapp"
    set_channel_turn(nxt)
    return nxt


def is_channel_turn(channel: str) -> bool:
    return get_channel_turn() == str(channel or "").strip().lower()


def set_busy(channel: str, task: str = "", detail: str = "") -> None:
    payload = {
        "busy": 1,
        "channel": str(channel or "").strip().lower(),
        "task": str(task or "").strip(),
        "detail": str(detail or "").strip(),
        "since": _now_iso(),
        "pid": os.getpid(),
    }
    _write_json(BUSY_FLAG_FILE, payload)
    print(f"🚦 [BUSY] ON | channel={payload['channel']} | task={payload['task'] or '-'}")


def clear_busy() -> None:
    if os.path.exists(BUSY_FLAG_FILE):
        try:
            os.remove(BUSY_FLAG_FILE)
        except OSError:
            _write_json(BUSY_FLAG_FILE, {"busy": 0, "cleared_at": _now_iso()})
    print("🚦 [BUSY] OFF")


def is_busy() -> bool:
    data = _read_json(BUSY_FLAG_FILE)
    if not data:
        return False
    try:
        return int(data.get("busy") or 0) == 1
    except (TypeError, ValueError):
        return False


def busy_info() -> dict:
    return _read_json(BUSY_FLAG_FILE)


def wait_until_idle(poll_seconds: float = 2.0, label: str = "") -> None:
    import time

    while is_busy():
        info = busy_info()
        task = info.get("task") or "-"
        channel = info.get("channel") or "-"
        suffix = f" ({label})" if label else ""
        print(f"⏸️ [BUSY] Waiting{suffix} — {channel} busy: {task}")
        time.sleep(max(0.5, float(poll_seconds)))
