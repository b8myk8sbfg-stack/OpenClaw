"""
Save and extract text from email attachments (O365).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from whatsapp_attachment_processor import extract_items_from_document_text, extract_text_from_document

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_ATTACHMENT_DIR = os.path.join(BASE_DIR, "logs/email_attachments")

SUPPORTED_EXTENSIONS = (
    ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".csv",
    ".png", ".jpg", ".jpeg", ".webp",
)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "attachment"))[:120]


def save_email_attachments(message, ref_prefix: str = "email") -> List[str]:
    saved_paths = []
    os.makedirs(EMAIL_ATTACHMENT_DIR, exist_ok=True)

    attachments = getattr(message, "attachments", None)
    if not attachments:
        return saved_paths

    try:
        attachment_list = list(attachments)
    except Exception:
        attachment_list = []

    for idx, attachment in enumerate(attachment_list, start=1):
        try:
            name = _safe_filename(getattr(attachment, "name", None) or f"file_{idx}")
            lower = name.lower()
            if not any(lower.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                continue
            out_path = os.path.join(EMAIL_ATTACHMENT_DIR, f"{ref_prefix}_{name}")
            attachment.save(out_path)
            saved_paths.append(out_path)
            print(f"📎 [EMAIL] Saved attachment: {out_path}")
        except Exception as exc:
            print(f"⚠️ [EMAIL] Attachment save failed: {exc}")

    return saved_paths


def enrich_email_body_from_attachments(body: str, attachment_paths: List[str]) -> Dict[str, Any]:
    combined_text = str(body or "")
    all_items: List[Dict[str, Any]] = []

    for path in attachment_paths:
        extracted = extract_text_from_document(path)
        if extracted:
            combined_text += f"\n\n[Attachment: {os.path.basename(path)}]\n{extracted[:6000]}"
            all_items.extend(extract_items_from_document_text(extracted, path))

    return {
        "body": combined_text.strip(),
        "attachment_paths": attachment_paths,
        "document_items": all_items,
    }
