"""Shared Microsoft 365 mailbox configuration for OpenClaw email automation."""

from __future__ import annotations

import os

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_REPLY_MODE_FILE = os.path.join(BASE_DIR, "openclaw_email_reply_mode.txt")

DEFAULT_MONITOR_EMAILS = "evon@robomatics.sg,sales@robomatics.sg"


def get_monitored_mailboxes() -> list[str]:
    """Mailboxes polled for unread customer inquiries and supplier replies."""
    raw = os.getenv("OPENCLAW_MONITOR_EMAILS", DEFAULT_MONITOR_EMAILS)
    seen = set()
    mailboxes = []
    for entry in raw.split(","):
        address = entry.strip().lower()
        if address and address not in seen:
            seen.add(address)
            mailboxes.append(address)
    return mailboxes or ["evon@robomatics.sg"]


def get_primary_mailbox() -> str:
    """Primary mailbox for outbound alerts when no per-inbox context exists."""
    return get_monitored_mailboxes()[0]
