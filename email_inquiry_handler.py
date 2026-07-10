"""Unified email inquiry extraction (attachments + Copilot + OBM engine)."""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openclaw_inquiry_engine import (
    build_plain_quotation_reply,
    build_voltage_selection_reply,
    extract_structured_rfq_items,
    process_structured_items,
)
from openclaw_main import (
    VERSION as OPENCLAW_ENGINE_VERSION,
    analyze_incoming_inquiry_with_copilot,
    build_product_details_for_reply,
)
from openclaw_email_config import get_email_monitor_recipients

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


@dataclass
class EmailInquiryResult:
    inquiry_text: str = ""
    image_path: str = ""
    attachment_paths: List[str] = field(default_factory=list)
    document_items: List[dict] = field(default_factory=list)
    copilot_analysis: dict = field(default_factory=dict)
    copilot_items: List[dict] = field(default_factory=list)
    formatted_rows: List[dict] = field(default_factory=list)
    tbc_by_brand: dict = field(default_factory=dict)
    skipped: List[dict] = field(default_factory=list)
    voltage_selections: List[dict] = field(default_factory=list)
    customer_reply: str = ""
    has_items: bool = False


def strip_email_thread_noise(body: str) -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    patterns = (
        r"\n-{2,}\s*Original Message\s*-{2,}",
        r"\n-{5,}\s*Forwarded message\s*-{5,}",
        r"\nFrom:\s",
        r"\nOn .{5,80} wrote:\s*",
        r"\n_{5,}",
    )
    upper = text
    cut_at = len(text)
    for pattern in patterns:
        match = re.search(pattern, upper, flags=re.I)
        if match and match.start() > 40:
            cut_at = min(cut_at, match.start())
    return text[:cut_at].strip()


def build_inquiry_text(subject: str, body_clean: str) -> str:
    subject = str(subject or "").strip()
    body = strip_email_thread_noise(body_clean)
    if subject:
        return f"Subject: {subject}\n\n{body}".strip()
    return body


def pick_image_attachment(attachment_paths: List[str]) -> str:
    for path in attachment_paths or []:
        if str(path or "").lower().endswith(IMAGE_EXTENSIONS) and os.path.exists(path):
            return path
    return ""


def _build_item_search_context(item: dict, inquiry_text: str) -> str:
    specs = item.get("technical_specs") or []
    if isinstance(specs, str):
        specs = [specs]
    return " ".join(
        chunk.strip()
        for chunk in [inquiry_text or "", " ".join(str(spec) for spec in specs if spec)]
        if chunk and str(chunk).strip()
    )


def _structured_items_from_sources(
    copilot_items: List[dict],
    document_items: List[dict],
    inquiry_text: str,
) -> List[dict]:
    structured_items: List[dict] = []
    seen = set()

    def add_item(brand, part_no, qty, source, technical_specs=None):
        part_no = str(part_no or "").strip().upper()
        part_norm = re.sub(r"[^A-Z0-9]", "", part_no)
        if not part_norm or part_norm in seen:
            return
        seen.add(part_norm)
        structured_items.append({
            "brand": str(brand or "UNKNOWN").strip().upper(),
            "part_no": part_no,
            "desc": part_no,
            "qty": int(qty or 1),
            "norm": part_norm,
            "source": source,
            "technical_specs": technical_specs or [],
            "search_context": "",
        })

    for doc in document_items or []:
        if not isinstance(doc, dict):
            continue
        add_item(
            doc.get("brand"),
            doc.get("part_no"),
            doc.get("qty") or 1,
            "EMAIL_DOCUMENT_EXTRACT",
        )

    for item in copilot_items or []:
        if not isinstance(item, dict):
            continue
        part_no = item.get("part_no")
        row = {
            "brand": str(item.get("brand") or "UNKNOWN").strip().upper(),
            "part_no": str(part_no or "").strip().upper(),
            "desc": str(item.get("description") or part_no or "").strip(),
            "qty": int(item.get("qty") or 1),
            "norm": re.sub(r"[^A-Z0-9]", "", str(part_no or "").upper()),
            "source": "COPILOT_UNIFIED",
            "technical_specs": item.get("technical_specs") or [],
            "product_type": str(item.get("product_type") or "").strip(),
        }
        if not row["norm"] or row["norm"] in seen:
            continue
        seen.add(row["norm"])
        row["search_context"] = _build_item_search_context(item, inquiry_text)
        structured_items.append(row)

    if not structured_items:
        for item in extract_structured_rfq_items(inquiry_text):
            part_norm = item.get("norm") or re.sub(r"[^A-Z0-9]", "", str(item.get("part_no") or "").upper())
            if not part_norm or part_norm in seen:
                continue
            seen.add(part_norm)
            item["search_context"] = inquiry_text
            structured_items.append(item)

    return structured_items


def process_unified_email_inquiry(
    *,
    subject: str,
    body_clean: str,
    attachment_paths: Optional[List[str]] = None,
    document_items: Optional[List[dict]] = None,
) -> EmailInquiryResult:
    attachment_paths = list(attachment_paths or [])
    document_items = list(document_items or [])
    inquiry_text = build_inquiry_text(subject, body_clean)
    image_path = pick_image_attachment(attachment_paths)

    print("🤖 [EMAIL] Unified analyze (text + attachments)...")
    if image_path:
        print(f"   🖼️ Image attachment for vision: {image_path}")
    if document_items:
        print(f"   📄 Document extractor items: {len(document_items)}")

    copilot_analysis = analyze_incoming_inquiry_with_copilot(
        message_text=inquiry_text,
        image_path=image_path or None,
    )
    copilot_items = list(copilot_analysis.get("items") or [])
    print(f"   🤖 Copilot items: {len(copilot_items)}")

    structured_items = _structured_items_from_sources(
        copilot_items,
        document_items,
        inquiry_text,
    )
    print(f"   🧩 Structured items for OBM: {len(structured_items)}")

    formatted_rows, tbc_by_brand, skipped, voltage_selections = process_structured_items(
        structured_items
    )

    if voltage_selections:
        customer_reply = build_voltage_selection_reply(
            voltage_selections,
            customer_message=inquiry_text,
        )
    elif formatted_rows:
        product_details = build_product_details_for_reply(
            formatted_rows=formatted_rows,
            copilot_items=copilot_items,
            technical_summary=str(copilot_analysis.get("technical_summary") or "").strip(),
        )
        customer_reply = build_plain_quotation_reply(
            formatted_rows,
            ai_research=product_details,
            customer_message=inquiry_text,
            copilot_items=copilot_items,
        )
    else:
        customer_reply = (
            "Hi,\n\nThank you for your enquiry.\n\n"
            "We received your email but could not detect part numbers to quote. "
            "Please resend with brand, model/part number, and quantity.\n\n"
            "Best regards,\nRobomatics"
        )

    has_items = bool(formatted_rows or voltage_selections or copilot_items or document_items)

    return EmailInquiryResult(
        inquiry_text=inquiry_text,
        image_path=image_path,
        attachment_paths=attachment_paths,
        document_items=document_items,
        copilot_analysis=copilot_analysis,
        copilot_items=copilot_items,
        formatted_rows=formatted_rows,
        tbc_by_brand=tbc_by_brand,
        skipped=skipped,
        voltage_selections=voltage_selections,
        customer_reply=customer_reply,
        has_items=has_items,
    )


def _plain_to_html(text: str) -> str:
    return "<br>".join(html.escape(line) for line in str(text or "").splitlines())


def build_email_monitor_html(
    *,
    customer_name: str,
    customer_email: str,
    mailbox_addr: str,
    subject: str,
    classification_summary: str,
    result: EmailInquiryResult,
    attachment_names: Optional[List[str]] = None,
) -> str:
    attachment_names = attachment_names or []
    item_lines = []
    for item in result.copilot_items:
        part_no = item.get("part_no") or "?"
        qty = item.get("qty") or 1
        brand = item.get("brand") or ""
        item_lines.append(f"- {brand} {part_no} x{qty}".strip())

    rows_html = ""
    for row in result.formatted_rows:
        rows_html += (
            "<tr>"
            f"<td>{html.escape(str(row.get('desc') or ''))}</td>"
            f"<td>{html.escape(str(row.get('qty') or ''))}</td>"
            f"<td>{html.escape(str(row.get('price') or ''))}</td>"
            f"<td>{html.escape(str(row.get('lt') or ''))}</td>"
            "</tr>"
        )

    quote_table = ""
    if rows_html:
        quote_table = (
            "<table border='1' cellpadding='5' style='border-collapse:collapse;'>"
            "<tr><th>Description</th><th>Qty</th><th>Price</th><th>Lead Time</th></tr>"
            f"{rows_html}</table>"
        )

    return (
        "<b>[OpenClaw Email Monitor Mode — Pre-Production]</b><br>"
        "No email was sent to the customer. Review the draft below.<br><br>"
        f"<b>Engine:</b> {html.escape(OPENCLAW_ENGINE_VERSION)}<br>"
        f"<b>Mailbox:</b> {html.escape(mailbox_addr)}<br>"
        f"<b>Customer:</b> {html.escape(customer_name)}<br>"
        f"<b>Customer email:</b> {html.escape(customer_email)} "
        "<span style='color:#b45309;'>(NOT emailed)</span><br>"
        f"<b>Subject:</b> {html.escape(subject)}<br>"
        f"<b>Classification:</b><br>{html.escape(classification_summary).replace(chr(10), '<br>')}<br><br>"
        f"<b>Copilot items:</b> {len(result.copilot_items)}<br>"
        f"{html.escape(chr(10).join(item_lines)).replace(chr(10), '<br>')}<br><br>"
        f"<b>Attachments:</b> {html.escape(', '.join(attachment_names) or '(none)')}<br>"
        f"<b>Vision image:</b> {html.escape(result.image_path or '(none)')}<br><br>"
        "<b>─── Quotation preview (would send to customer) ───</b><br><br>"
        f"{_plain_to_html(result.customer_reply)}<br><br>"
        f"{quote_table}"
    )


def send_email_monitor_preview(
    mailbox,
    *,
    customer_name: str,
    customer_email: str,
    mailbox_addr: str,
    subject: str,
    classification_summary: str,
    result: EmailInquiryResult,
    attachment_names: Optional[List[str]] = None,
) -> bool:
    recipients = get_email_monitor_recipients()
    body = build_email_monitor_html(
        customer_name=customer_name,
        customer_email=customer_email,
        mailbox_addr=mailbox_addr,
        subject=subject,
        classification_summary=classification_summary,
        result=result,
        attachment_names=attachment_names,
    )
    monitor_msg = mailbox.new_message()
    for recipient in recipients:
        monitor_msg.to.add(recipient)
    monitor_msg.subject = f"[OpenClaw Monitor] {subject}"
    monitor_msg.body = body
    monitor_msg.body_type = "html"
    monitor_msg.send()
    print("📣 Email monitor preview sent")
    print(f"   To: {', '.join(recipients)}")
    print(f"   Subject: [OpenClaw Monitor] {subject}")
    return True


def build_supplier_reply_monitor_html(
    *,
    customer_name: str,
    customer_email: str,
    ref_code: str,
    subject: str,
    formatted_rows: List[dict],
) -> str:
    return (
        "<b>[OpenClaw Email Monitor Mode — Supplier Reply]</b><br>"
        "Supplier reply was parsed but <b>no email was sent to the customer</b>.<br><br>"
        f"<b>Ref:</b> {html.escape(ref_code)}<br>"
        f"<b>Customer:</b> {html.escape(customer_name)}<br>"
        f"<b>Customer email:</b> {html.escape(customer_email)}<br>"
        f"<b>Original subject:</b> {html.escape(subject)}<br><br>"
        "<b>Would-have-sent quotation table:</b><br>"
        f"{_plain_to_html(str(formatted_rows))}"
    )
