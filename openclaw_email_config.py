"""Shared Microsoft 365 mailbox configuration for OpenClaw email automation."""

from __future__ import annotations

import os

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_REPLY_MODE_FILE = os.path.join(BASE_DIR, "openclaw_email_reply_mode.txt")

DEFAULT_MONITOR_EMAILS = "evon@robomatics.sg,sales@robomatics.sg"
DEFAULT_EMAIL_MONITOR_RECIPIENTS = "stephen@robomatics.sg,annie@robomatics.sg"


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


def get_email_monitor_recipients() -> list[str]:
    """Pre-production recipients who receive inquiry previews (no customer email)."""
    raw = os.getenv("OPENCLAW_EMAIL_MONITOR_RECIPIENTS", DEFAULT_EMAIL_MONITOR_RECIPIENTS)
    seen = set()
    recipients = []
    for entry in raw.split(","):
        address = entry.strip().lower()
        if address and address not in seen:
            seen.add(address)
            recipients.append(address)
    return recipients or ["stephen@robomatics.sg"]


def get_email_reply_mode() -> str:
    env_mode = os.getenv("OPENCLAW_EMAIL_REPLY_MODE", "").strip().lower()
    if env_mode:
        return env_mode
    try:
        with open(EMAIL_REPLY_MODE_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip().lower() or "monitor"
    except FileNotFoundError:
        return "monitor"


def email_replies_go_to_monitor() -> bool:
    return get_email_reply_mode() in ("monitor", "debug", "test", "preproduction")


def get_primary_mailbox() -> str:
    """Primary mailbox for outbound alerts/RFQs when no per-inbox context exists."""
    mailboxes = get_monitored_mailboxes()
    return mailboxes[0]
