"""
Save and extract text from email attachments (O365).
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, List

from whatsapp_attachment_processor import extract_items_from_document_text, extract_text_from_document

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_ATTACHMENT_DIR = os.path.join(BASE_DIR, "logs/email_attachments")
EMAIL_IMAGE_DIR = os.path.join(BASE_DIR, "WA_Image/email")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
SUPPORTED_EXTENSIONS = (
    ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".csv",
) + IMAGE_EXTENSIONS


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "attachment"))[:120]


def _is_image_path(path: str) -> bool:
    return str(path or "").lower().endswith(IMAGE_EXTENSIONS)


def ensure_email_attachments_downloaded(message) -> None:
    """Download O365 attachment bytes (required for inline/cid images)."""
    try:
        if getattr(message, "has_attachments", False):
            message.attachments.download_attachments()
    except Exception as exc:
        print(f"⚠️ [EMAIL] Attachment download failed: {exc}")


def save_inline_images_from_html(raw_body: str, ref_prefix: str = "email") -> List[str]:
    """Extract base64 or cid-linked inline images embedded in HTML email bodies."""
    html = str(raw_body or "")
    if not html.strip():
        return []

    os.makedirs(EMAIL_IMAGE_DIR, exist_ok=True)
    saved_paths: List[str] = []

    for match in re.finditer(
        r'src=["\']data:image/([^;\s]+);base64,([^"\']+)["\']',
        html,
        flags=re.I,
    ):
        ext = match.group(1).lower().replace("jpeg", "jpg")
        if ext not in ("png", "jpg", "webp", "gif", "bmp"):
            ext = "png"
        try:
            data = base64.b64decode(match.group(2))
        except Exception:
            continue
        if len(data) < 1024:
            continue
        out_path = os.path.join(
            EMAIL_IMAGE_DIR,
            f"{ref_prefix}_inline_{len(saved_paths) + 1}.{ext}",
        )
        with open(out_path, "wb") as f:
            f.write(data)
        saved_paths.append(out_path)
        print(f"📎 [EMAIL] Saved inline HTML image: {out_path}")

    return saved_paths


def save_email_attachments(message, ref_prefix: str = "email", raw_body: str = "") -> List[str]:
    saved_paths = []
    os.makedirs(EMAIL_ATTACHMENT_DIR, exist_ok=True)
    os.makedirs(EMAIL_IMAGE_DIR, exist_ok=True)

    ensure_email_attachments_downloaded(message)

    attachments = getattr(message, "attachments", None)
    if attachments:
        try:
            attachment_list = list(attachments)
        except Exception:
            attachment_list = []
    else:
        attachment_list = []

    for idx, attachment in enumerate(attachment_list, start=1):
        try:
            name = _safe_filename(getattr(attachment, "name", None) or f"file_{idx}")
            lower = name.lower()
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            is_image = (
                lower.endswith(IMAGE_EXTENSIONS)
                or "image/" in content_type
                or bool(getattr(attachment, "is_inline", False) and lower.endswith(IMAGE_EXTENSIONS))
            )
            if not is_image and not any(lower.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                continue

            target_dir = EMAIL_IMAGE_DIR if is_image else EMAIL_ATTACHMENT_DIR
            out_path = os.path.join(target_dir, f"{ref_prefix}_{name}")
            if not attachment.save(location=target_dir, custom_name=f"{ref_prefix}_{name}"):
                content = getattr(attachment, "content", None)
                if content:
                    with open(out_path, "wb") as f:
                        f.write(base64.b64decode(content))
                else:
                    print(f"⚠️ [EMAIL] Could not save attachment: {name}")
                    continue
            if not os.path.isfile(out_path):
                continue
            saved_paths.append(out_path)
            inline_flag = "inline" if getattr(attachment, "is_inline", False) else "file"
            print(f"📎 [EMAIL] Saved {inline_flag} attachment: {out_path}")
        except Exception as exc:
            print(f"⚠️ [EMAIL] Attachment save failed: {exc}")

    for inline_path in save_inline_images_from_html(raw_body, ref_prefix=ref_prefix):
        if inline_path not in saved_paths:
            saved_paths.append(inline_path)

    return saved_paths


def collect_email_image_paths(attachment_paths: List[str]) -> List[str]:
    """Return saved image paths for OCR (attachments + inline HTML)."""
    image_paths: List[str] = []
    seen = set()
    for path in attachment_paths or []:
        if _is_image_path(path) and path not in seen:
            seen.add(path)
            image_paths.append(path)
    return image_paths


def email_body_has_image_markers(raw_body: str) -> bool:
    html = str(raw_body or "")
    if not html:
        return False
    if re.search(r"<img\b", html, flags=re.I):
        return True
    if re.search(r"data:image/", html, flags=re.I):
        return True
    if re.search(r'src=["\']cid:', html, flags=re.I):
        return True
    return False


def enrich_email_body_from_attachments(body: str, attachment_paths: List[str]) -> Dict[str, Any]:
    combined_text = str(body or "")
    all_items: List[Dict[str, Any]] = []

    for path in attachment_paths:
        if _is_image_path(path):
            continue
        extracted = extract_text_from_document(path)
        if extracted:
            combined_text += f"\n\n[Attachment: {os.path.basename(path)}]\n{extracted[:6000]}"
            all_items.extend(extract_items_from_document_text(extracted, path))

    return {
        "body": combined_text.strip(),
        "attachment_paths": attachment_paths,
        "document_items": all_items,
    }
