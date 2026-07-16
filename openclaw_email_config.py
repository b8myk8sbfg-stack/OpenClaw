"""Shared Microsoft 365 mailbox configuration for OpenClaw email automation."""

from __future__ import annotations

import os

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_REPLY_MODE_FILE = os.path.join(BASE_DIR, "openclaw_email_reply_mode.txt")

DEFAULT_MONITOR_EMAILS = "evon@robomatics.sg,sales@robomatics.sg"
DEFAULT_MONITOR_ALERT_EMAILS = "stephen@robomatics.sg,annie@robomatics.sg"


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


def get_email_reply_mode() -> str:
    env_mode = os.getenv("OPENCLAW_EMAIL_REPLY_MODE", "").strip().lower()
    if env_mode:
        return env_mode
    try:
        with open(EMAIL_REPLY_MODE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().lower() or "monitor"
    except FileNotFoundError:
        return "monitor"


def customer_email_replies_go_to_monitor() -> bool:
    if get_email_reply_mode() == "live" and os.getenv("OPENCLAW_ALLOW_CUSTOMER_REPLIES", "").strip() == "1":
        return False
    return True


def get_monitor_alert_emails() -> list[str]:
    """Pre-production alert recipients — never the end customer."""
    raw = os.getenv("OPENCLAW_MONITOR_ALERT_EMAILS", DEFAULT_MONITOR_ALERT_EMAILS)
    seen = set()
    emails = []
    for entry in raw.split(","):
        address = entry.strip().lower()
        if address and address not in seen:
            seen.add(address)
            emails.append(address)
    return emails or ["stephen@robomatics.sg"]


def build_monitor_email_body(
    *,
    context: str,
    customer_name: str,
    customer_email: str,
    original_subject: str,
    generated_reply_html: str,
    supplier_suggestions_html: str = "",
) -> str:
    lines = [
        "<p><strong>[OpenClaw Monitor Mode]</strong></p>",
        f"<p><strong>Context:</strong> {context or 'Customer reply'}</p>",
        f"<p><strong>Customer:</strong> {customer_name or '-'}<br>",
        f"<strong>Customer Email:</strong> {customer_email or '-'}<br>",
        f"<strong>Original Subject:</strong> {original_subject or '-'}</p>",
        "<p><strong>Generated Reply (not sent to customer):</strong></p>",
        "<hr>",
        generated_reply_html or "<p>(empty)</p>",
    ]
    if supplier_suggestions_html:
        lines.append(supplier_suggestions_html)
    return "\n".join(lines)
