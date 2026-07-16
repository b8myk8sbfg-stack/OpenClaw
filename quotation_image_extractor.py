"""
Quotation document image extraction via Copilot vision.

Detects quotation PDF/screen photos (vs product labels), extracts line items
using column-aware layout reasoning, validates arithmetic, and converts to RFQ items.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

VERSION = "v1.00-QUOTATION-VISION"

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
COPILOT_API_KEY = os.getenv("COPILOT_API_KEY", "local-copilot-proxy")
COPILOT_TIMEOUT_SECS = float(os.getenv("OPENCLAW_COPILOT_QUOTATION_TIMEOUT", "120"))
MAX_ATTEMPTS = max(1, int(os.getenv("OPENCLAW_QUOTATION_VISION_ATTEMPTS", "2")))

QUOTATION_VISION_ENABLED = os.getenv("OPENCLAW_QUOTATION_IMAGE_VISION", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

_QUOTATION_SIGNALS = (
    re.compile(r"\bQUOTATION\b", re.I),
    re.compile(r"\bOUR\s*REF\b", re.I),
    re.compile(r"\bUNIT\s*PRICE\b", re.I),
    re.compile(r"\bTOTAL\s*PRICE\b", re.I),
    re.compile(r"\bTOTAL\s*AMOUNT\b", re.I),
    re.compile(r"\bDELIVERY\b", re.I),
)

_OUR_REF_RE = re.compile(r"^Q\d{3,}$", re.I)

_QUOTATION_SYSTEM = (
    "You extract data from quotation documents shown in photos. "
    "Use table column alignment — do not read description text as quantity. "
    "Our Ref in the header (e.g. Q001300) is NOT the item code. "
    "Item code is the product code on the first line of the Description column. "
    "Qty comes ONLY from the Qty column (ignore 10K, MOQ, resistance values in description). "
    "Read Unit Price, Total Price, Delivery from their respective columns. "
    "Copy printed numbers exactly — do not guess catalog prices or common delivery terms. "
    "Return ONLY valid JSON."
)

_QUOTATION_USER_PROMPT = """This image is a QUOTATION document (not a product label).

Table columns: NO | Description | Qty | Unit Price | Disc | Total Price | Delivery

Extract ONE line item (or each line if multiple rows):
- item_code: product code in Description column (NOT Our Ref)
- description: product name and specs under item_code
- qty: integer from Qty column only
- unit_price: number from Unit Price column
- total_price: number from Total Price column
- delivery: text from Delivery column (read exactly, e.g. 2-3 WEEKS)
- total_amount: number from Total Amount (RM) footer

Customer WhatsApp caption (for reference only — table qty takes precedence unless caption clearly requests different order qty):
{caption}

Return JSON only:
{{"item_code":"","description":"","qty":0,"unit_price":0.00,"total_price":0.00,"delivery":"","total_amount":0.00}}"""

_RETRY_PROMPT_SUFFIX = (
    "\n\nPREVIOUS ATTEMPT FAILED VALIDATION: {reason}\n"
    "Re-read the TABLE COLUMNS carefully.\n"
    "- Qty is the number under the Qty column (e.g. 2 PCE → qty=2). Never use 10K from description.\n"
    "- unit_price × qty must equal total_price.\n"
    "- Item code is in the Description column, NOT Our Ref in the header.\n"
    "Return corrected JSON only."
)


def quotation_vision_enabled() -> bool:
    return QUOTATION_VISION_ENABLED


def detect_image_mime(image_path: str) -> str:
    """Detect JPEG/PNG from magic bytes (WhatsApp often saves JPEG as .png)."""
    with open(image_path, "rb") as handle:
        header = handle.read(12)
    if header.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    guessed, _ = __import__("mimetypes").guess_type(image_path)
    return guessed or "image/jpeg"


def is_quotation_document(ocr_text: str) -> bool:
    """True when OCR text looks like a quotation table, not a product label."""
    text = str(ocr_text or "").strip()
    if not text:
        return False
    hits = sum(1 for pattern in _QUOTATION_SIGNALS if pattern.search(text))
    return hits >= 2


def is_our_ref_token(token: str) -> bool:
    return bool(_OUR_REF_RE.match(str(token or "").strip()))


def _parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper()
    text = re.sub(r"^RM\s*", "", text)
    text = text.replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_int_qty(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip().upper()
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    qty = int(match.group(1))
    return qty if qty > 0 else None


def _normalize_key(data: dict, *candidates: str) -> Any:
    for key in candidates:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def normalize_quotation_payload(data: dict) -> dict:
    """Normalize Copilot JSON with alternate key casing."""
    if not isinstance(data, dict):
        return {}

    # Copilot sometimes nests under items[]
    if "items" in data and isinstance(data["items"], list) and data["items"]:
        first = data["items"][0]
        if isinstance(first, dict):
            data = {**data, **first}

    item_code = str(
        _normalize_key(data, "item_code", "Item Code", "itemCode", "part_no", "Part No") or ""
    ).strip().upper()
    description = str(
        _normalize_key(data, "description", "Description", "product_type") or ""
    ).strip()
    qty = _parse_int_qty(_normalize_key(data, "qty", "Qty", "quantity", "Quantity"))
    unit_price = _parse_money(_normalize_key(data, "unit_price", "Unit Price", "unitPrice"))
    total_price = _parse_money(_normalize_key(data, "total_price", "Total Price", "totalPrice"))
    delivery = str(_normalize_key(data, "delivery", "Delivery") or "").strip()
    total_amount = _parse_money(
        _normalize_key(data, "total_amount", "Total Amount", "totalAmount")
    )

    return {
        "item_code": item_code,
        "description": description,
        "qty": qty,
        "unit_price": unit_price,
        "total_price": total_price,
        "delivery": delivery,
        "total_amount": total_amount,
    }


def validate_quotation_fields(data: dict) -> tuple[bool, str]:
    """Sanity-check extracted quotation fields (layout + arithmetic)."""
    item_code = str(data.get("item_code") or "").strip()
    if not item_code:
        return False, "missing item_code"
    if is_our_ref_token(item_code):
        return False, f"item_code looks like Our Ref ({item_code}), not a product code"

    qty = data.get("qty")
    unit_price = data.get("unit_price")
    total_price = data.get("total_price")

    if not qty or qty <= 0:
        return False, "invalid qty"
    if qty >= 10 and re.search(r"\b10\s*K\b", str(data.get("description") or ""), re.I):
        return False, f"qty={qty} may be confused with 10K resistance value"

    if unit_price is not None and total_price is not None and unit_price > 0 and total_price > 0:
        expected = round(qty * unit_price, 2)
        actual = round(total_price, 2)
        tolerance = max(0.02, actual * 0.02)
        if abs(expected - actual) > tolerance:
            return False, f"arithmetic mismatch: {qty} x {unit_price} = {expected}, not {actual}"

    if total_price and data.get("total_amount"):
        footer = round(data["total_amount"], 2)
        line = round(total_price, 2)
        tolerance = max(0.02, footer * 0.02)
        if abs(footer - line) > tolerance:
            return False, f"total_amount {footer} does not match line total {line}"

    generic_delivery = {"EX-STOCK", "EX STOCK", "READY STOCK", "IN STOCK"}
    delivery = str(data.get("delivery") or "").strip().upper()
    if delivery in generic_delivery and unit_price and total_price:
        # Allow ex-stock only if arithmetic already passed; flag as weak
        pass

    return True, ""


def extract_json_from_response(text: str) -> dict | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines).strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return normalize_quotation_payload(parsed)


def _build_image_user_content(prompt: str, image_path: str) -> list:
    with open(image_path, "rb") as handle:
        image_b64 = base64.b64encode(handle.read()).decode("ascii")
    mime = detect_image_mime(image_path)
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{image_b64}", "detail": "high"},
        },
    ]


def _copilot_vision_extract(image_path: str, caption: str, retry_reason: str = "") -> dict | None:
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=COPILOT_API_KEY,
        timeout=COPILOT_TIMEOUT_SECS,
        max_retries=0,
    )
    prompt = _QUOTATION_USER_PROMPT.format(caption=caption or "(none)")
    if retry_reason:
        prompt += _RETRY_PROMPT_SUFFIX.format(reason=retry_reason)

    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": _QUOTATION_SYSTEM},
                {"role": "user", "content": _build_image_user_content(prompt, image_path)},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        print(f"[QUOTATION VISION] Copilot raw: {raw[:500]}")
        return extract_json_from_response(raw)
    except Exception as exc:
        print(f"[QUOTATION VISION] Copilot error: {exc}")
        return None


def quotation_to_rfq_items(data: dict, caption: str) -> list[dict]:
    """Convert validated quotation extraction to standard RFQ item dicts."""
    from inquiry_extraction_helper import extract_qty_from_caption, is_plausible_part_no

    item_code = str(data.get("item_code") or "").strip().upper()
    if not item_code or not is_plausible_part_no(item_code):
        return []
    if is_our_ref_token(item_code):
        return []

    document_qty = int(data.get("qty") or 1)
    caption_qty = extract_qty_from_caption(caption)
    inquiry_qty = caption_qty if caption_qty else document_qty

    description = str(data.get("description") or "").strip()
    return [{
        "part_no": item_code,
        "qty": inquiry_qty,
        "brand": "UNKNOWN",
        "product_type": description or "QUOTATION ITEM",
        "source": "quotation_vision",
        "quotation_meta": {
            "document_qty": document_qty,
            "caption_qty": caption_qty,
            "unit_price": data.get("unit_price"),
            "total_price": data.get("total_price"),
            "delivery": data.get("delivery"),
            "total_amount": data.get("total_amount"),
        },
    }]


def try_extract_quotation_image(image_path: str, caption: str = "") -> dict | None:
    """
    Detect quotation document and extract via Copilot vision with validation + retry.

    Returns dict compatible with _extract_rfq_with_copilot_only:
      {"items": [...], "route": "copilot_quotation_vision", "ocr_used": bool}
    or None when not a quotation / extraction failed.
    """
    if not quotation_vision_enabled():
        return None
    if not image_path or not os.path.isfile(image_path):
        return None

    try:
        from local_ocr import extract_text_from_image, has_usable_ocr_text
    except ImportError:
        return None

    ocr_payload = extract_text_from_image(image_path)
    ocr_text = str(ocr_payload.get("full_text") or "")
    if not is_quotation_document(ocr_text):
        return None

    print(f"[QUOTATION VISION] Document detected — routing to Copilot vision ({VERSION})")

    validated: dict | None = None
    last_reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_reason = last_reason if attempt > 1 else ""
        print(f"[QUOTATION VISION] Attempt {attempt}/{MAX_ATTEMPTS}")
        parsed = _copilot_vision_extract(image_path, caption, retry_reason=retry_reason)
        if not parsed:
            last_reason = "malformed JSON"
            continue
        ok, reason = validate_quotation_fields(parsed)
        if ok:
            validated = parsed
            break
        print(f"[QUOTATION VISION] Validation failed: {reason}")
        last_reason = reason

    if not validated:
        print("[QUOTATION VISION] Extraction failed — falling back to label OCR route")
        return None

    items = quotation_to_rfq_items(validated, caption)
    if not items:
        print("[QUOTATION VISION] Could not build RFQ items from validated extraction")
        return None

    print(
        f"[QUOTATION VISION] Extracted {items[0]['part_no']} "
        f"qty={items[0]['qty']} (doc={validated.get('qty')}) "
        f"delivery={validated.get('delivery')}"
    )
    return {
        "items": items,
        "route": "copilot_quotation_vision",
        "ocr_used": has_usable_ocr_text(ocr_payload),
        "quotation_fields": validated,
    }
