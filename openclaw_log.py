"""Prefix stdout/stderr lines with timestamps for easier log debugging."""

from __future__ import annotations

import os
import sys
from datetime import datetime


class _TimestampStream:
    def __init__(self, stream):
        self._stream = stream
        self._buffer = ""

    def write(self, data):
        if not data:
            return 0
        self._buffer += data
        written = 0
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._stream.write(f"{ts} {line}\n")
                written += len(line) + len(ts) + 2
            else:
                self._stream.write("\n")
                written += 1
        return written

    def flush(self):
        if self._buffer:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._stream.write(f"{ts} {self._buffer}")
            self._buffer = ""
        self._stream.flush()

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()


def timestamped_logging_enabled() -> bool:
    mode = os.getenv("OPENCLAW_LOG_TIMESTAMPS", "1").strip().lower()
    return mode not in ("0", "false", "no", "off", "none")


def enable_timestamped_logging() -> None:
    if not timestamped_logging_enabled():
        return
    if getattr(sys.stdout, "_openclaw_timestamped", False):
        return
    sys.stdout = _TimestampStream(sys.stdout)
    sys.stderr = _TimestampStream(sys.stderr)
    sys.stdout._openclaw_timestamped = True  # type: ignore[attr-defined]
    sys.stderr._openclaw_timestamped = True  # type: ignore[attr-defined]
