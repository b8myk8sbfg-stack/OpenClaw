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
STALE_BUSY_SECONDS = int(os.getenv("OPENCLAW_BUSY_STALE_SECONDS", "300"))


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


def clear_stale_busy() -> bool:
    """Drop a leftover busy flag from a crashed or hung worker."""
    data = _read_json(BUSY_FLAG_FILE)
    if not data or int(data.get("busy") or 0) != 1:
        return False

    since = str(data.get("since") or "").strip()
    age_seconds = STALE_BUSY_SECONDS + 1
    if since:
        try:
            started = datetime.datetime.fromisoformat(since)
            age_seconds = (datetime.datetime.now() - started).total_seconds()
        except Exception:
            age_seconds = STALE_BUSY_SECONDS + 1

    if age_seconds < STALE_BUSY_SECONDS:
        return False

    print(
        "⚠️ [BUSY] Clearing stale busy flag "
        f"({age_seconds:.0f}s old | channel={data.get('channel') or '-'} "
        f"| task={data.get('task') or '-'})"
    )
    clear_busy()
    return True


def is_busy() -> bool:
    clear_stale_busy()
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
        since = info.get("since") or "-"
        suffix = f" ({label})" if label else ""
        throttled_log(
            f"busy-wait:{label or 'default'}",
            f"⏸️ [BUSY] Waiting{suffix} — {channel} busy: {task} (since {since})",
            interval=60.0,
        )
        time.sleep(max(0.5, float(poll_seconds)))


_throttle_log_times: dict = {}


def throttled_log(key: str, message: str, interval: float = 30.0) -> None:
    """Print at most once per interval to avoid log spam during channel turns."""
    import time

    now = time.time()
    last = _throttle_log_times.get(key, 0.0)
    if now - last >= interval:
        print(message)
        _throttle_log_times[key] = now
