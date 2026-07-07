import subprocess
import sys
import time
import os
import json
import base64
import mimetypes
import signal
import re

from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

VERSION = "v1.26-LABEL-FOCUS-MONITOR-PHOTO"

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")


def parse_copilot_confidence(value, default: float = 0.75) -> float:
    """Accept Copilot confidence as 0.9, 90, 'high', etc."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1.0:
            num = num / 100.0
        return max(0.0, min(num, 1.0))
    text = str(value).strip().lower()
    if not text:
        return default
    labels = {
        "very high": 0.95,
        "high": 0.9,
        "medium": 0.75,
        "med": 0.75,
        "moderate": 0.75,
        "low": 0.6,
        "very low": 0.5,
    }
    if text in labels:
        return labels[text]
    try:
        num = float(text.rstrip("%").strip())
        if num > 1.0:
            num = num / 100.0
        return max(0.0, min(num, 1.0))
    except (TypeError, ValueError):
        return default


def _parse_copilot_qty(value, default: int = 1) -> int:
    try:
        qty = int(float(str(value).strip()))
        return max(1, qty)
    except (TypeError, ValueError):
        return max(1, int(default or 1))


def _normalize_part_key(part_no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part_no or "").upper())


def _is_quote_without_part_text(text: str) -> bool:
    """True for captions like 'Quote me 2 pcs' with no embedded part number."""
    text_u = str(text or "").upper().strip()
    if not text_u:
        return False
    if not re.search(r"\b(QUOTE|QUOTATION|RFQ|ENQ|PRICE|PLS QUOTE|KINDLY QUOTE|QUOTE ME)\b", text_u):
        return False
    if re.search(r"[A-Z]{1,4}\d{3,}[A-Z0-9#\-/]*", text_u):
        return False
    if re.search(r"\b(QTY|PCS|PC|PIECES|PIECE|EA|EACH|UNIT|UNITS)\b", text_u):
        return True
    return len(text_u) < 80


def _visual_part_consistent(part_no: str, brand: str, product_type: str) -> bool:
    """Reject obvious vision mismatches between label type and model family."""
    part_u = str(part_no or "").upper().strip()
    part_key = _normalize_part_key(part_u)
    brand_u = str(brand or "").upper().strip()
    type_u = str(product_type or "").upper().strip()

    if not part_u:
        return False

    if (
        "BATTERY" in type_u
        or "LITHIUM" in type_u
        or part_key.startswith("ER")
        or part_key.startswith("CR")
    ):
        return True

    if type_u:
        if "PROXIMITY" in type_u and not part_key.startswith("E2E"):
            print(
                f"[WARN] Visual mismatch: label type {product_type!r} "
                f"does not match part {part_no!r}"
            )
            return False
        if "TIMER" in type_u and not (part_key.startswith("H3J") or part_key.startswith("H3Y")):
            print(
                f"[WARN] Visual mismatch: label type {product_type!r} "
                f"does not match part {part_no!r}"
            )
            return False
        if "TEMPERATURE CONTROLLER" in type_u and not (
            part_key.startswith("E5CC") or part_key.startswith("E5CN")
        ):
            print(
                f"[WARN] Visual mismatch: label type {product_type!r} "
                f"does not match part {part_no!r}"
            )
            return False
        if "LIMIT SWITCH" in type_u and not part_key.startswith("WLD"):
            print(
                f"[WARN] Visual mismatch: label type {product_type!r} "
                f"does not match part {part_no!r}"
            )
            return False

    if brand_u == "OMRON" and part_key.startswith("E5CC") and type_u and "TEMPERATURE" not in type_u:
        print(
            f"[WARN] OMRON part {part_no!r} looks like a temperature controller "
            "but label type was not temperature controller."
        )
        return False

    if part_key.startswith("E2E") and type_u and "PROXIMITY" not in type_u and "SENSOR" not in type_u:
        print(
            f"[WARN] OMRON E2E part {part_no!r} rejected — label type was {product_type!r}, "
            "not a proximity sensor."
        )
        return False

    if (part_key.startswith("H3J") or part_key.startswith("H3Y")) and type_u and "TIMER" not in type_u:
        print(
            f"[WARN] OMRON timer part {part_no!r} rejected — label type was {product_type!r}, "
            "not a timer."
        )
        return False

    return True


COPILOT_ANALYZE_INTENTS = {
    "rfq_inquiry",
    "technical_support",
    "purchase_order",
    "delivery_tracking",
    "payment_invoice",
    "supplier_reply",
    "order_confirmation",
    "complaint",
    "greeting",
    "general_chat",
    "junk_ad",
    "unknown",
}


def _normalize_copilot_intent(intent: str) -> str:
    intent_u = str(intent or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "request_for_quotation": "rfq_inquiry",
        "quotation": "rfq_inquiry",
        "rfq": "rfq_inquiry",
        "quote": "rfq_inquiry",
        "technical_support": "technical_support",
        "equivalent": "technical_support",
        "replacement": "technical_support",
        "purchase_order": "purchase_order",
        "po": "purchase_order",
        "junk": "junk_ad",
        "spam": "junk_ad",
        "advertisement": "junk_ad",
    }
    if intent_u in aliases:
        return aliases[intent_u]
    if intent_u in COPILOT_ANALYZE_INTENTS:
        return intent_u
    return "unknown"


OPENCLAW_UNIFIED_PROMPT = """NEW INQUIRY

Analyze ONLY the attached message/image.
Do NOT use information from previous conversations.

Tasks:
1. Classify the message:
   - Request for Quotation (RFQ)
   - Technical Support
   - Purchase Order
   - Voice Message (transcribe first if WAV)
   - Junk Advertisement
   - Promotion Pamphlet

2. Extract all important details.

3. Determine whether the item is used in Industrial Automation.

4. Check whether it belongs to or is commonly associated with these brands:
Omron, SMC, Burkert, Parker, Legris, Keyence, Siemens, Noeding, Hohner, Baumer, Mitsubishi, Panasonic, Yaskawa, ABB, etc.

5. Provide technical specifications.

If the attached image shows an RFQ / enquiry table (columns such as No, Item, Picture, Qty):
- The customer caption may only say "PLS QUOTE" — part numbers and quantities are IN THE IMAGE.
- Read the Item column text AND any product label/nameplate in the Picture column.
- Read the Qty column for quantity (default 1 if not shown).
- Example row: Item "Allen-Bradley Soft Starter Model 150-C25NBD", Qty 3
  → part_no=150-C25NBD, brand=ALLEN-BRADLEY, qty=3, product_type=SOFT STARTER
- Transcribe model codes character-by-character from labels (e.g. CAT 150-C25NBD).

Return the result in plain text.

On the very last line only (after your plain-text analysis), append one JSON object with no markdown fences:
{"intent":"rfq_inquiry","confidence":0.9,"items":[{"part_no":"MODEL","qty":1,"brand":"BRAND","product_type":"TYPE"}],"technical_summary":"...","is_industrial_automation":true,"compatible_brands":[],"reasoning":"..."}

intent must be one of: rfq_inquiry, technical_support, purchase_order, junk_ad, greeting, general_chat, unknown
confidence must be a number from 0.0 to 1.0
items must be an array of objects with keys part_no, qty, brand, product_type
"""

RFQ_TABLE_FOCUS_PROMPT = """NEW INQUIRY

Analyze ONLY the attached RFQ / enquiry table image.
Do NOT use information from previous conversations.

The WhatsApp caption may only say "please quote". All part numbers are IN THIS IMAGE.

Read every table row (columns: No, Item, Picture, Qty):
- part_no from the Item column and/or the product label in the Picture column
- qty from the Qty column
- brand from Item text or the nameplate (e.g. ALLEN-BRADLEY, OMRON)

Example: Allen-Bradley Soft Starter 150-C25NBD, Qty 3
→ part_no=150-C25NBD, brand=ALLEN-BRADLEY, qty=3, product_type=SOFT STARTER

Return plain-text analysis, then on the last line only append JSON (no markdown):
{"items":[{"part_no":"150-C25NBD","qty":3,"brand":"ALLEN-BRADLEY","product_type":"SOFT STARTER"}],"technical_summary":"..."}
"""

LABEL_FOCUS_PROMPT = """NEW INQUIRY

Analyze ONLY the attached product label/nameplate image.
Do NOT use information from previous conversations.

The customer is quoting ONE product shown in this photo (e.g. "quote me 1 pce").
Read the exact model/code printed on the label character-by-character.

Examples:
- Lithium battery label ER6C 3.6V → part_no=ER6C 3.6V, product_type=LITHIUM BATTERY
- Timer H3JA-8A → part_no=H3JA-8A, product_type=TIMER
- Sensor E3Z-T61 only if those exact characters are printed on the label

Do NOT guess E3Z, E2E, or E3Z-T61-L unless those exact characters appear on THIS label.

Return plain-text analysis, then on the last line only append JSON (no markdown):
{"items":[{"part_no":"ER6C 3.6V","qty":1,"brand":"TOSHIBA","product_type":"LITHIUM BATTERY"}],"technical_summary":"..."}
"""


def _is_single_product_photo_inquiry(message_text: str) -> bool:
    """True for 'quote me 1 pce' style — one product label photo, not an RFQ table."""
    text = str(message_text or "").upper()
    return bool(
        re.search(
            r"\b(QUOTE ME|(?:^|\s)1\s*(?:PCE|PC|PCS|PIECE|PIECES)|ONE PCE|ONE PC)\b",
            text,
        )
    )


def _should_run_rfq_table_focus(message_text: str = "", analysis_text: str = "") -> bool:
    """RFQ table focus only for table-style quote requests — not single label photos."""
    if _is_single_product_photo_inquiry(message_text):
        return False
    blob = f"{message_text}\n{analysis_text}".upper()
    return bool(re.search(r"\b(PLS QUOTE|KINDLY QUOTE|MORNING MS|RFQ|QTY\s*3)\b", blob))


def _items_need_label_reverify(items: list, message_text: str) -> bool:
    """Re-read label when Copilot returns a sensor part on a single-product photo."""
    if not _is_single_product_photo_inquiry(message_text) or not items:
        return False
    for item in items:
        part_key = re.sub(r"[^A-Z0-9]", "", str(item.get("part_no") or "").upper())
        ptype = str(item.get("product_type") or "").upper()
        if part_key.startswith(("E3Z", "E2E", "E39")):
            if "SENSOR" not in ptype and "PHOTO" not in ptype and "PROXIMITY" not in ptype:
                return True
    return False


def _parse_caption_qty(text: str, default: int = 1) -> int:
    """Parse qty from caption without loading the warehouse engine."""
    text_u = str(text or "").upper()
    for pattern in (
        r"\b(?:QTY|QUANTITY)\s*[:;]?\s*(\d{1,4})\b",
        r"\b(\d{1,4})\s*(?:PCS|PC|PCE|PIECES|PIECE|UNIT|UNITS|EA|EACH)\b",
    ):
        match = re.search(pattern, text_u)
        if match:
            qty = int(match.group(1))
            if qty > 0:
                return qty
    try:
        return max(1, int(default or 1))
    except (TypeError, ValueError):
        return 1


def _extract_balanced_json_object(text: str) -> str:
    """Return the first complete {...} substring using brace balancing."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return ""


def is_copilot_transport_failure(analysis: dict) -> bool:
    """True only when the Copilot proxy/API is down — not JSON/parse gaps on HTTP 200."""
    if not isinstance(analysis, dict) or not analysis.get("attempted"):
        return False
    if analysis.get("ok") is not False:
        return False
    status = analysis.get("http_status")
    if status is None:
        return True
    try:
        code = int(status)
    except (TypeError, ValueError):
        return True
    if code == 400:
        error = str(analysis.get("error") or "").lower()
        if "image data" in error or "invalid_request" in error:
            return False
    return code >= 400


def _minimal_copilot_analysis_result(
    message_text: str = "",
    analysis_text: str = "",
    raw: str = "",
    parse_warning: str = "",
    http_status: int = 200,
) -> dict:
    """Best-effort analyze result so RFQ routing can continue after HTTP 200."""
    prose = str(analysis_text or raw or "").strip()
    inferred_intent = _infer_intent_from_prose(prose, message_text)
    return {
        "attempted": True,
        "ok": True,
        "intent": inferred_intent,
        "confidence": 0.7,
        "reasoning": parse_warning or "Copilot response could not be fully parsed.",
        "items": [],
        "technical_summary": _sanitize_whatsapp_reply(prose),
        "analysis_text": prose,
        "is_industrial_automation": True,
        "compatible_brands": [],
        "raw_excerpt": str(raw or prose)[:800],
        "http_status": http_status,
        "parse_warning": parse_warning or None,
    }


def _infer_intent_from_prose(prose: str, message_text: str = "") -> str:
    """Best-effort intent when Copilot returns plain text without JSON."""
    blob = f"{prose}\n{message_text}".upper()
    if re.search(r"\b(REQUEST FOR QUOTATION|RFQ|QUOTATION|QUOTE|PLS QUOTE|QUOTE ME)\b", blob):
        return "rfq_inquiry"
    if re.search(r"\b(TECHNICAL SUPPORT|EQUIVALENT|REPLACEMENT|SUBSTITUTE|WIRING|FAULT)\b", blob):
        return "technical_support"
    if re.search(r"\b(PURCHASE ORDER|\bPO\b|ORDER PLACEMENT)\b", blob):
        return "purchase_order"
    if re.search(r"\b(JUNK|ADVERTISEMENT|SPAM|PROMOTION|PAMPHLET)\b", blob):
        return "junk_ad"
    return "unknown"


def _copilot_fresh_chat(client, messages, timeout: float = 60.0):
    """Every Copilot call starts a new upstream conversation — no thread history."""
    return client.chat.completions.create(
        model=COPILOT_MODEL,
        messages=messages,
        extra_body={"conversation_id": None},
        timeout=timeout,
    )


def _extract_json_from_copilot_text(raw: str):
    """Split Copilot prose from trailing (or embedded) JSON object."""
    text = str(raw or "").strip()
    if not text:
        return "", {}

    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()

    lines = text.splitlines()
    for start in range(len(lines) - 1, -1, -1):
        tail = "\n".join(lines[start:]).strip()
        if not tail.startswith("{"):
            continue
        try:
            parsed = json.loads(tail)
            if isinstance(parsed, dict):
                prose = "\n".join(lines[:start]).strip()
                return prose, parsed
        except json.JSONDecodeError:
            candidate = _extract_balanced_json_object(tail)
            if candidate:
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        prose = "\n".join(lines[:start]).strip()
                        remainder = tail[len(candidate):].strip()
                        if remainder:
                            prose = f"{prose}\n{remainder}".strip() if prose else remainder
                        return prose, parsed
                except json.JSONDecodeError:
                    continue

    for match in re.finditer(r"\{", text):
        candidate = _extract_balanced_json_object(text[match.start():])
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and (
            "intent" in parsed or "items" in parsed or "technical_summary" in parsed
        ):
            prose = (text[:match.start()] + text[match.start() + len(candidate):]).strip()
            return prose, parsed

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return "", parsed
    except json.JSONDecodeError:
        pass

    return text, {}


def _detect_image_mime_from_bytes(data: bytes) -> str:
    """Detect real image MIME from magic bytes (not file extension)."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _copilot_user_content_with_image(user_text: str, image_path: str = None):
    """Build OpenAI user content with optional high-detail image attachment."""
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as image_file:
            raw = image_file.read()
        mime = _detect_image_mime_from_bytes(raw)
        if not mime:
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        image_b64 = base64.b64encode(raw).decode("ascii")
        return [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{image_b64}",
                    "detail": "high",
                },
            },
        ]
    return user_text


def _parse_copilot_items_from_dict(parsed: dict, message_text: str = "", voice_transcript: str = "") -> list:
    """Normalize items array from Copilot JSON."""
    caption_qty = _parse_caption_qty(message_text or voice_transcript, default=1)
    items_out = []
    for item in parsed.get("items") or []:
        if not isinstance(item, dict):
            continue
        part_no = str(item.get("part_no") or "").strip().upper()
        if not part_no:
            continue
        try:
            qty = _parse_copilot_qty(item.get("qty"), default=caption_qty or 1)
        except (TypeError, ValueError):
            qty = caption_qty
        qty = max(1, qty)
        brand = str(item.get("brand") or "UNKNOWN").strip().upper()
        product_type = str(item.get("product_type") or "").strip()
        item_out = {"part_no": part_no, "qty": qty, "brand": brand, "source": "COPILOT_UNIFIED"}
        if product_type:
            item_out["product_type"] = product_type
        items_out.append(item_out)
    return items_out


def _copilot_label_focus_pass(
    client,
    message_text: str = "",
    image_path: str = None,
    voice_transcript: str = "",
) -> list:
    """Fresh Copilot call to read a single product label/nameplate (battery, timer, etc.)."""
    if not image_path or not os.path.exists(image_path):
        return []

    from whatsapp_attachment_processor import validate_image_file

    ok_img, reason = validate_image_file(image_path)
    if not ok_img:
        print(f"[COPILOT LABEL] Skipping focus pass — invalid image: {reason}")
        return []

    parts = [LABEL_FOCUS_PROMPT]
    if message_text:
        parts.append(f"Customer caption:\n{message_text}")
    user_content = _copilot_user_content_with_image("\n\n".join(parts), image_path)

    print("[COPILOT LABEL] Focus pass — read product label/nameplate from image...")
    try:
        response = _copilot_fresh_chat(
            client,
            [{"role": "user", "content": user_content}],
            timeout=120.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT LABEL RAW] {raw[:400]}")
        _prose, parsed = _extract_json_from_copilot_text(raw)
        if not isinstance(parsed, dict):
            return []
        items = _parse_copilot_items_from_dict(parsed, message_text, voice_transcript)
        if items:
            print(f"[COPILOT LABEL] Extracted {len(items)} item(s) from label focus pass")
        return items
    except Exception as exc:
        print(f"[WARN] Copilot label focus pass failed: {exc}")
        return []


def _copilot_rfq_table_focus_pass(
    client,
    message_text: str = "",
    image_path: str = None,
    voice_transcript: str = "",
) -> list:
    """Fresh Copilot call focused on reading RFQ table rows from the image only."""
    if not image_path or not os.path.exists(image_path):
        return []

    from whatsapp_attachment_processor import validate_image_file

    ok_img, reason = validate_image_file(image_path)
    if not ok_img:
        print(f"[COPILOT RFQ TABLE] Skipping focus pass — invalid image: {reason}")
        return []

    parts = [RFQ_TABLE_FOCUS_PROMPT]
    if message_text:
        parts.append(f"Customer caption (quote request only):\n{message_text}")
    user_text = "\n\n".join(parts)
    user_content = _copilot_user_content_with_image(user_text, image_path)

    print("[COPILOT RFQ TABLE] Focus pass — read RFQ table / nameplate from image...")
    try:
        response = _copilot_fresh_chat(
            client,
            [{"role": "user", "content": user_content}],
            timeout=120.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT RFQ TABLE RAW] {raw[:400]}")
        _prose, parsed = _extract_json_from_copilot_text(raw)
        if not isinstance(parsed, dict):
            return []
        items = _parse_copilot_items_from_dict(parsed, message_text, voice_transcript)
        if items:
            print(f"[COPILOT RFQ TABLE] Extracted {len(items)} item(s) from table focus pass")
        return items
    except Exception as exc:
        print(f"[WARN] Copilot RFQ table focus pass failed: {exc}")
        return []


def analyze_incoming_inquiry_with_copilot(
    message_text: str = "",
    image_path: str = None,
    document_text: str = None,
    voice_transcript: str = None,
) -> dict:
    """Copilot-first unified analysis: classify intent + extract parts from text/image/voice/doc together."""
    if os.getenv("OPENCLAW_COPILOT_FIRST", "1").strip().lower() in ("0", "false", "no", "off"):
        return {"attempted": False, "ok": False, "items": []}

    message_text = str(message_text or "").strip()
    document_text = str(document_text or "").strip()
    voice_transcript = str(voice_transcript or "").strip()

    if not any([message_text, image_path, document_text, voice_transcript]):
        return {"attempted": False, "ok": False, "items": []}

    print("[COPILOT ANALYZE] Unified incoming message analysis (text + attachment together)...")
    if image_path:
        if os.path.exists(image_path):
            from whatsapp_attachment_processor import validate_image_file

            img_ok, img_reason = validate_image_file(image_path)
            print(
                f"[COPILOT ANALYZE] Image file size: {os.path.getsize(image_path)} bytes "
                f"({'valid' if img_ok else 'INVALID: ' + img_reason})"
            )
            if not img_ok:
                print("[COPILOT ANALYZE] Skipping corrupt/thumbnail image — analyzing caption/text only.")
                image_path = None
        else:
            print(f"[COPILOT ANALYZE] WARN image_path missing on disk: {image_path}")
            return _minimal_copilot_analysis_result(
                message_text=message_text,
                parse_warning=f"image file not found: {image_path}",
                http_status=200,
            )

    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=90.0 if image_path else 60.0,
        max_retries=1,
    )

    prompt_parts = [OPENCLAW_UNIFIED_PROMPT]
    if voice_transcript:
        prompt_parts.append(
            "Attached voice message (already transcribed from WAV):\n"
            f"{voice_transcript}"
        )
    if message_text:
        prompt_parts.append(f"Attached customer message/caption:\n{message_text}")
    if document_text:
        prompt_parts.append(f"Attached document text:\n{document_text[:4000]}")
    if image_path and os.path.exists(image_path):
        prompt_parts.append(
            "IMPORTANT: The attached image is the customer's RFQ photo. "
            "Read ALL text visible in the image — tables, labels, nameplates. "
            "The caption may only ask for a quote; part numbers are in the image."
        )
    elif not message_text and not voice_transcript and not document_text:
        prompt_parts.append("Attached image: see screenshot (analyze this message only).")

    user_text = "\n\n".join(prompt_parts)
    user_content = _copilot_user_content_with_image(user_text, image_path)
    if image_path and os.path.exists(image_path):
        print(f"[COPILOT ANALYZE] Fresh chat — bubble screenshot only: {image_path}")

    raw = ""
    try:
        response = _copilot_fresh_chat(
            client,
            [{"role": "user", "content": user_content}],
            timeout=120.0 if image_path else 60.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT ANALYZE RAW] {raw[:500]}")
        analysis_text, parsed = _extract_json_from_copilot_text(raw)
        if not isinstance(parsed, dict) or not parsed:
            if analysis_text and len(analysis_text.strip()) >= 40:
                inferred_intent = _infer_intent_from_prose(analysis_text, message_text)
                print(
                    "[COPILOT ANALYZE] Plain-text response without JSON — "
                    f"using prose fallback intent={inferred_intent}"
                )
                parsed = {
                    "intent": inferred_intent,
                    "confidence": 0.7,
                    "items": [],
                    "technical_summary": analysis_text,
                    "is_industrial_automation": True,
                    "compatible_brands": [],
                    "reasoning": "Parsed from plain-text Copilot analysis (no JSON footer).",
                }
            else:
                print(
                    "[COPILOT ANALYZE] No JSON footer — continuing with best-effort prose/caption parse"
                )
                return _minimal_copilot_analysis_result(
                    message_text=message_text,
                    analysis_text=analysis_text,
                    raw=raw,
                    parse_warning="no JSON object in Copilot response",
                    http_status=200,
                )

        intent = _normalize_copilot_intent(parsed.get("intent"))
        confidence = parse_copilot_confidence(parsed.get("confidence"), default=0.75)
        reasoning = str(parsed.get("reasoning") or "").strip()
        technical_summary = _sanitize_whatsapp_reply(
            str(parsed.get("technical_summary") or analysis_text or "").strip()
        )
        is_ia = bool(parsed.get("is_industrial_automation", True))
        compatible_brands = parsed.get("compatible_brands") or []

        items_out = _parse_copilot_items_from_dict(
            parsed, message_text=message_text, voice_transcript=voice_transcript
        )

        if image_path and os.path.exists(image_path):
            if _items_need_label_reverify(items_out, message_text):
                print(
                    "[COPILOT ANALYZE] Sensor-like guess on single-product photo — "
                    "re-reading label"
                )
                items_out = []

            if not items_out:
                if _should_run_rfq_table_focus(message_text, analysis_text):
                    items_out = _copilot_rfq_table_focus_pass(
                        client,
                        message_text=message_text,
                        image_path=image_path,
                        voice_transcript=voice_transcript,
                    )
                    for item in items_out:
                        item["source"] = "COPILOT_RFQ_TABLE"
                else:
                    items_out = _copilot_label_focus_pass(
                        client,
                        message_text=message_text,
                        image_path=image_path,
                        voice_transcript=voice_transcript,
                    )
                    for item in items_out:
                        item["source"] = "COPILOT_LABEL"

        result = {
            "attempted": True,
            "ok": True,
            "intent": intent,
            "confidence": max(0.0, min(confidence, 1.0)),
            "reasoning": reasoning,
            "items": items_out,
            "technical_summary": technical_summary,
            "analysis_text": analysis_text,
            "is_industrial_automation": is_ia,
            "compatible_brands": compatible_brands,
            "raw_excerpt": raw[:800],
        }
        print(
            f"[COPILOT ANALYZE] intent={intent} ({confidence:.0%}) | "
            f"items={len(items_out)} | {reasoning[:80]}"
        )
        if not items_out and image_path:
            print(f"[COPILOT ANALYZE] WARN zero items after parse — raw excerpt: {raw[:300]!r}")
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[WARN] Copilot analyze parse issue (HTTP 200): {exc}")
        return _minimal_copilot_analysis_result(
            message_text=message_text,
            analysis_text=raw,
            raw=raw,
            parse_warning=f"invalid JSON from Copilot: {exc}",
            http_status=200,
        )
    except APIStatusError as exc:
        print(f"[WARN] Copilot analyze HTTP {exc.status_code}: {exc}")
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": exc.status_code,
            "items": [],
        }
    except (APIConnectionError, APITimeoutError) as exc:
        print(f"[WARN] Copilot analyze connection/timeout: {exc}")
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": None,
            "items": [],
        }
    except Exception as exc:
        print(f"[WARN] Copilot unified analyze failed: {exc}")
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": None,
            "items": [],
        }


def extract_rfq_with_copilot(raw_email_body: str = "", image_path: str = None) -> list:
    """Extract RFQ items from text and/or an image through the local Copilot proxy."""
    caption = str(raw_email_body or "").strip()
    if not caption and not image_path:
        return []

    if not image_path:
        if _is_quote_without_part_text(caption):
            print(
                "[WARN] Quote caption without part number requires a product photo — "
                "skipping text-only Copilot extraction to avoid guessing."
            )
            return []
        if not caption:
            return []

    if image_path:
        print(f"[API READ] Visual extraction using image: {image_path}")
    else:
        print("[API READ] Text-only extraction (no image attached)...")

    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=60.0 if image_path else 30.0,
        max_retries=1,
    )
    system_instruction = (
        "You are an industrial automation data extraction assistant. "
        "Visually inspect the provided industrial product photo, label, nameplate, barcode sticker, "
        "and/or customer text. Extract EVERY distinct manufacturer part number visible. "
        "Read printed model/order codes exactly as shown on the label/nameplate in THIS photo only. "
        "Read character-by-character; do not substitute a different catalog number. "
        "Never default to OMRON E2E-X5E1 — only return E2E if those exact characters are printed on the label. "
        "Product families (read what is actually printed): "
        "E2E- = OMRON proximity sensor; H3JA-/H3Y- = OMRON timer; E5CC-/E5CN- = temperature controller; "
        "MY2/MY4 = relay; ER-/CR- = lithium battery (e.g. TOSHIBA ER6C 3.6V). "
        "If the label says TIMER or H3JA-, never return E2E. "
        "If the label says LITHIUM/BATTERY or shows ER6C/ER17500, never return E2E/H3JA. "
        "If the label says PROXIMITY SENSOR or E2E-, never return H3JA/battery models. "
        "Include supply voltage on the part_no when printed (example H3JA-8A AC200-240). "
        "Match the brand field to the visible manufacturer logo (OMRON, SMC, etc.). "
        "Never reuse a part number from chat history or from a different message. "
        "If multiple labelled products appear in one photo, return one JSON object per distinct part number. "
        "For relays, solenoids, coils, and power products, voltage and AC/DC are mandatory: "
        "include them in 'part_no' exactly as shown (for example 'MY2N-GS-R 24VDC'). "
        "Never substitute another voltage variant. "
        "Use customer caption for quantity hints: 'Quote 2 pcs' with two visible parts often means qty 1 each; "
        "a single visible part with '2 pcs' means qty 2. "
        "Return STRICTLY a raw JSON array of objects with keys 'part_no', 'qty', 'brand', and 'product_type'. "
        "product_type is the visible product description on the label (example: TIMER, PROXIMITY SENSOR). "
        "Quantity must be a positive integer. Do not guess missing part numbers. "
        "If quantity is not visible and caption is absent, use 1. If brand is not visible, use 'UNKNOWN'. "
        "Do not include markdown, backticks, or conversational text. "
        'Example: [{"part_no": "H3JA-8A AC200-240", "qty": 1, "brand": "OMRON", "product_type": "TIMER"}, '
        '{"part_no": "ER6C 3.6V", "qty": 1, "brand": "TOSHIBA", "product_type": "LITHIUM BATTERY"}]'
    )

    try:
        user_text = (
            "Identify every industrial part in THIS customer message only. "
            "Read only the attached photo and caption below — ignore all prior chat context. "
            "Transcribe the exact model number printed on the product label/nameplate. "
            f"Customer caption/text:\n{caption or '(none)'}"
        )
        user_content = user_text

        if image_path:
            with open(image_path, "rb") as image_file:
                image_b64 = base64.b64encode(image_file.read()).decode("ascii")
            mime = mimetypes.guess_type(image_path)[0] or "image/png"
            user_content = [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{image_b64}",
                        "detail": "high",
                    },
                },
            ]

        response = _copilot_fresh_chat(
            client,
            [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
        )
        raw_content = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT RAW] {raw_content}")
        if raw_content.startswith("```"):
            lines = raw_content.splitlines()
            raw_content = "\n".join(lines[1:-1]).strip()

        parsed = json.loads(raw_content)
        if not isinstance(parsed, list):
            raise ValueError("Copilot response must be a JSON array")

        extracted_items = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            part_no = str(item.get("part_no") or "").strip().upper()
            try:
                qty = int(item.get("qty"))
            except (TypeError, ValueError):
                continue
            if part_no and qty > 0:
                brand = str(item.get("brand") or "UNKNOWN").strip().upper()
                product_type = str(item.get("product_type") or "").strip()
                if image_path and not product_type:
                    print(
                        f"[WARN] Visual extraction missing product_type for {part_no!r} — rejected"
                    )
                    continue
                if image_path and not _visual_part_consistent(part_no, brand, product_type):
                    continue
                item_out = {"part_no": part_no, "qty": qty, "brand": brand}
                if product_type:
                    item_out["product_type"] = product_type
                extracted_items.append(item_out)
        return extracted_items
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] Copilot returned invalid JSON data: {exc}")
    except Exception as exc:
        print(f"[ERROR] Failed to communicate with local Copilot server: {exc}")
    return []


def research_part_with_copilot(part_no: str, brand: str = "UNKNOWN") -> str:
    """Look up a part with Copilot and return a short technical summary for customer replies."""
    part_no = str(part_no or "").strip().upper()
    brand = str(brand or "UNKNOWN").strip().upper()
    if not part_no:
        return ""

    if os.getenv("OPENCLAW_COPILOT_RESEARCH", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""

    print(f"[COPILOT RESEARCH] Looking up {part_no} ({brand})...")
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=45.0,
        max_retries=1,
    )
    prompt = (
        f"Research the industrial automation part {part_no}"
        f"{f' by {brand}' if brand and brand != 'UNKNOWN' else ''}.\n"
        "Write a concise technical summary suitable for a sales quotation reply.\n"
        "Include:\n"
        "- One-sentence product description\n"
        "- Main specifications as short bullet points\n"
        "- Typical applications (one short bullet list)\n"
        "Keep the answer under 180 words. Plain text only. No markdown, no headers, no hyperlinks."
    )
    try:
        response = _copilot_fresh_chat(
            client,
            [
                {
                    "role": "system",
                    "content": (
                        "You are an industrial automation product specialist. "
                        "Give accurate, practical summaries for sales staff. "
                        "Plain text only — no markdown, no links."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        print(f"[COPILOT RESEARCH] {len(text)} chars for {part_no}")
        return _sanitize_whatsapp_reply(text)
    except Exception as exc:
        print(f"[WARN] Copilot research failed for {part_no}: {exc}")
        return ""


def build_ai_research_summary(formatted_rows):
    """Build Copilot research notes for the unique customer parts in a quote."""
    sections = []
    seen = set()
    for row in formatted_rows or []:
        part_no = str(row.get("customer_part") or row.get("pid") or "").strip().upper()
        if not part_no or part_no in seen:
            continue
        seen.add(part_no)
        brand = str(row.get("brand") or "UNKNOWN").strip().upper()
        notes = research_part_with_copilot(part_no, brand)
        if notes:
            sections.append(f"{part_no}\n{notes}")
    return "\n\n".join(sections)


def _is_equivalent_support_request(message_text: str) -> bool:
    """True when customer wants equivalent/replacement, not a price quote."""
    text_u = str(message_text or "").upper()
    markers = (
        "EQUIVALENT", "REPLACEMENT", "SUBSTITUTE", "ALTERNATIVE",
        "SUCCESSOR", "REPLACE WITH", "COMPATIBLE", "INTERCHANGE",
    )
    if not any(marker in text_u for marker in markers):
        return False
    if re.search(r"\b(QUOTE|QUOTATION|PRICE|HOW MUCH|UNIT PRICE|COST|RFQ)\b", text_u):
        return False
    return True


def _is_e2e_family_part(part_no: str) -> bool:
    return _normalize_part_key(part_no).startswith("E2E")


def _is_sensor_family_part(part_no: str) -> bool:
    """OMRON-style proximity / photoelectric families that are often vision-hallucinated."""
    part_key = _normalize_part_key(part_no)
    return (
        part_key.startswith("E2E")
        or part_key.startswith("E3Z")
        or part_key.startswith("E39")
    )


def _is_battery_visual_item(item: dict) -> bool:
    product_type = str(item.get("product_type") or "").upper()
    part_key = _normalize_part_key(item.get("part_no"))
    return (
        "BATTERY" in product_type
        or "LITHIUM" in product_type
        or part_key.startswith("ER")
        or part_key.startswith("CR")
    )


def build_photo_confirmation_line(items: list) -> str:
    """One-line visual confirmation for RFQ replies when parts came from a photo."""
    if not items:
        return ""
    primary = items[0]
    part_no = str(primary.get("part_no") or "").strip().upper()
    brand = str(primary.get("brand") or "").strip().upper()
    product_type = str(primary.get("product_type") or "").strip()
    if not part_no:
        return ""

    if _is_battery_visual_item(primary):
        noun = product_type or "lithium battery"
        brand_bit = f"{brand} " if brand and brand != "UNKNOWN" else ""
        return f"From your photo this is a {brand_bit}{part_no} {noun.lower()}."

    if _is_timer_visual_item(primary):
        brand_bit = brand if brand and brand != "UNKNOWN" else "OMRON"
        base_match = re.search(r"(H3J[AY]-\d+[A-Z]?)", part_no, re.I)
        base_model = base_match.group(1).upper() if base_match else part_no.split()[0]
        return f"From your photo this is an {brand_bit} {base_model} timer relay."

    brand_bit = f"{brand} " if brand and brand != "UNKNOWN" else ""
    type_bit = f" {product_type.lower()}" if product_type else ""
    return f"From your photo this is a {brand_bit}{part_no}{type_bit}."


def _is_e2e_only_guess(items: list, label_item: dict = None) -> bool:
    """True when every extracted item is E2E but label OCR did not confirm E2E."""
    if not items:
        return False
    if not all(_is_e2e_family_part(item.get("part_no")) for item in items):
        return False
    if label_item and _is_e2e_family_part(label_item.get("part_no")):
        return False
    return True


def _part_refs_from_copilot_items(copilot_items) -> list:
    """Collect unique part numbers from prior visual/text Copilot extraction."""
    refs = []
    seen = set()
    for item in copilot_items or []:
        part_no = str(item.get("part_no") or "").strip().upper()
        if not part_no:
            continue
        key = _normalize_part_key(part_no)
        if key and key not in seen:
            seen.add(key)
            refs.append(part_no)
    return refs


def _is_timer_visual_item(item: dict) -> bool:
    part_key = _normalize_part_key(item.get("part_no"))
    product_type = str(item.get("product_type") or "").upper()
    return "TIMER" in product_type or part_key.startswith("H3J") or part_key.startswith("H3Y")


def _filter_conflicting_visual_items(items: list) -> list:
    """Drop cross-family hallucinations (e.g. E2E when label is a timer)."""
    items = [item for item in (items or []) if isinstance(item, dict) and item.get("part_no")]
    if not items:
        return []

    has_timer = any(_is_timer_visual_item(item) for item in items)
    if has_timer:
        filtered = [
            item for item in items
            if not _normalize_part_key(item.get("part_no")).startswith("E2E")
        ]
        if filtered:
            return filtered

    has_battery = any(
        "BATTERY" in str(item.get("product_type") or "").upper()
        or "LITHIUM" in str(item.get("product_type") or "").upper()
        or _normalize_part_key(item.get("part_no")).startswith("ER")
        for item in items
    )
    if has_battery:
        filtered = [
            item for item in items
            if not _is_sensor_family_part(item.get("part_no"))
            and not _normalize_part_key(item.get("part_no")).startswith("H3J")
            and not _normalize_part_key(item.get("part_no")).startswith("H3Y")
        ]
        if filtered:
            return filtered

    has_proximity = any(
        "PROXIMITY" in str(item.get("product_type") or "").upper()
        or "PHOTOELECTRIC" in str(item.get("product_type") or "").upper()
        or "SENSOR" in str(item.get("product_type") or "").upper()
        or _is_sensor_family_part(item.get("part_no"))
        for item in items
    )
    if has_proximity:
        filtered = [
            item for item in items
            if not (
                _normalize_part_key(item.get("part_no")).startswith("H3J")
                or _normalize_part_key(item.get("part_no")).startswith("H3Y")
                or _is_battery_visual_item(item)
            )
        ]
        if filtered:
            return filtered

    return items


def extract_timer_label_from_image(caption: str = "", image_path: str = None) -> dict:
    """Second-pass OCR focused on OMRON timer nameplates (H3JA/H3Y/H3CR)."""
    if not image_path or not os.path.exists(image_path):
        return {}

    print(f"[COPILOT TIMER OCR] Focused timer label read: {image_path}")
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=60.0,
        max_retries=1,
    )
    system_instruction = (
        "The photo shows an industrial TIMER nameplate (often OMRON H3JA or H3Y). "
        "Transcribe the exact printed model code character-by-character. "
        "Common format: H3JA-8A, H3JA-8C, H3Y-2. "
        "The label also prints TIMER and supply voltage such as 200 to 240VAC. "
        "Never return E2E proximity sensor models. "
        "Return one JSON object: part_no, brand, product_type, supply_voltage."
    )
    user_text = (
        "Read the TIMER model number printed on this nameplate only. "
        "Example correct read: H3JA-8A with TIMER and 200-240VAC. "
        f"Caption: {caption or '(none)'}"
    )
    try:
        with open(image_path, "rb") as image_file:
            image_b64 = base64.b64encode(image_file.read()).decode("ascii")
        mime = mimetypes.guess_type(image_path)[0] or "image/png"
        response = _copilot_fresh_chat(
            client,
            [
                {"role": "system", "content": system_instruction},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
        )
        raw_content = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT TIMER OCR RAW] {raw_content}")
        if raw_content.startswith("```"):
            raw_content = "\n".join(raw_content.splitlines()[1:-1]).strip()
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            return {}
        part_no = str(parsed.get("part_no") or "").strip().upper()
        if not part_no:
            return {}
        brand = str(parsed.get("brand") or "OMRON").strip().upper()
        product_type = str(parsed.get("product_type") or "TIMER").strip().upper()
        if not _is_timer_visual_item({"part_no": part_no, "product_type": product_type}):
            return {}
        item = {"part_no": part_no, "qty": 1, "brand": brand, "product_type": product_type}
        supply_voltage = str(parsed.get("supply_voltage") or "").strip()
        if supply_voltage:
            item["supply_voltage"] = supply_voltage
        print(f"[COPILOT TIMER OCR] Identified {brand} {part_no}")
        return item
    except Exception as exc:
        print(f"[WARN] Timer OCR failed: {exc}")
    return {}


def extract_label_from_image(caption: str = "", image_path: str = None) -> dict:
    """OCR-style label read for tech support — one product label per photo."""
    if not image_path or not os.path.exists(image_path):
        return {}

    print(f"[COPILOT LABEL OCR] Reading nameplate from: {image_path}")
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=60.0,
        max_retries=1,
    )
    system_instruction = (
        "You transcribe industrial product labels from photos (sensors, timers, relays, "
        "batteries, PLC modules, etc.). "
        "Read the largest printed model/order code character-by-character from the nameplate. "
        "Do not guess or substitute a different catalog number. "
        "Never default to OMRON E2E-X5E1 unless those exact characters are printed on the label. "
        "TIMER/H3JA-/H3Y- = timer relay — never E2E. "
        "LITHIUM/BATTERY/ER6C/ER17500 = battery — never E2E/E3Z/H3JA. "
        "PROXIMITY SENSOR/E2E- or PHOTOELECTRIC/E3Z- = sensor — never H3JA/battery/ER6C. "
        "Return STRICTLY one raw JSON object with keys: "
        "part_no, brand, product_type, supply_voltage. "
        "part_no must match the label exactly (include voltage when printed, e.g. ER6C 3.6V). "
        "product_type is the printed description (TIMER, LITHIUM BATTERY, PROXIMITY SENSOR, etc.). "
        "supply_voltage is the printed source rating when visible, else empty string. "
        "No markdown, no array, no extra keys."
    )
    user_text = (
        "Transcribe the product label in THIS photo only. "
        "Read character-by-character what is printed — do not substitute a different catalog number. "
        f"Customer caption:\n{caption or '(none)'}"
    )

    try:
        with open(image_path, "rb") as image_file:
            image_b64 = base64.b64encode(image_file.read()).decode("ascii")
        mime = mimetypes.guess_type(image_path)[0] or "image/png"
        user_content = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{image_b64}",
                    "detail": "high",
                },
            },
        ]
        response = _copilot_fresh_chat(
            client,
            [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
        )
        raw_content = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT LABEL OCR RAW] {raw_content}")
        if raw_content.startswith("```"):
            lines = raw_content.splitlines()
            raw_content = "\n".join(lines[1:-1]).strip()

        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            return {}

        part_no = str(parsed.get("part_no") or "").strip().upper()
        if not part_no:
            return {}

        brand = str(parsed.get("brand") or "UNKNOWN").strip().upper()
        product_type = str(parsed.get("product_type") or "").strip().upper()
        supply_voltage = str(parsed.get("supply_voltage") or "").strip()

        if not _visual_part_consistent(part_no, brand, product_type):
            return {}

        item = {
            "part_no": part_no,
            "qty": 1,
            "brand": brand,
            "product_type": product_type,
        }
        if supply_voltage:
            item["supply_voltage"] = supply_voltage
        print(f"[COPILOT LABEL OCR] Identified {brand} {part_no} ({product_type})")
        return item
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[WARN] Label OCR returned invalid JSON: {exc}")
    except Exception as exc:
        print(f"[WARN] Label OCR failed: {exc}")
    return {}


def _reconcile_visual_items(existing_items: list, label_item: dict) -> list:
    """Prefer dedicated label OCR over generic RFQ extraction."""
    label_part = str(label_item.get("part_no") or "").strip().upper()
    label_key = _normalize_part_key(label_part)
    if not label_key:
        return _filter_conflicting_visual_items(existing_items)

    filtered = []
    for item in existing_items or []:
        part_key = _normalize_part_key(item.get("part_no"))
        if label_key.startswith("H3J") and part_key.startswith("E2E"):
            print(f"[COPILOT TECH SUPPORT] Dropping wrong E2E extraction — label says {label_part}")
            continue
        if _is_timer_visual_item(label_item) and part_key.startswith("E2E"):
            print(f"[COPILOT TECH SUPPORT] Dropping E2E — label OCR says TIMER {label_part}")
            continue
        filtered.append(item)

    if not any(_normalize_part_key(item.get("part_no")) == label_key for item in filtered):
        filtered.insert(0, label_item)
    return _filter_conflicting_visual_items(filtered)


def _is_unconfirmed_e2e_guess(items: list, label_item: dict) -> bool:
    """True when the only extracted part is E2E but label OCR did not confirm it."""
    if not items or len(items) != 1:
        return False
    part_key = _normalize_part_key(items[0].get("part_no"))
    if not part_key.startswith("E2E"):
        return False
    if label_item:
        label_key = _normalize_part_key(label_item.get("part_no"))
        return not label_key.startswith("E2E")
    return True


def finalize_copilot_visual_items(
    caption: str,
    image_path: str = None,
    copilot_items: list = None,
    unified_analyze_ran: bool = False,
) -> list:
    """Return Copilot unified items as-is — no secondary OCR or regex post-filters."""
    return list(copilot_items or [])


def _resolve_visual_items(caption: str, image_path: str = None, copilot_items: list = None) -> list:
    """Deprecated — kept for imports; returns unified items without re-guessing."""
    return finalize_copilot_visual_items(
        caption,
        image_path=image_path,
        copilot_items=copilot_items,
        unified_analyze_ran=True,
    )


def _timer_voltage_note(part_no: str) -> str:
    part_u = str(part_no or "").upper()
    if re.search(r"200[\s\-]*(?:TO[\s\-]*)?240|AC200.?240", part_u):
        return "200-240VAC"
    if re.search(r"100[\s\-]*(?:TO[\s\-]*)?120|AC100.?120", part_u):
        return "100-120VAC"
    if re.search(r"\bDC24\b", part_u):
        return "DC24V"
    if re.search(r"\bAC24\b", part_u):
        return "AC24V"
    return ""


def _build_timer_equivalent_reply(items: list, warehouse_context: str) -> str:
    """Deterministic reply for OMRON H3JA/H3Y timer equivalent requests."""
    primary = items[0]
    part_no = str(primary.get("part_no") or "").strip().upper()
    brand = str(primary.get("brand") or "OMRON").strip().upper()
    base_match = re.search(r"(H3J[AY]-\d+[A-Z]?)", part_no, re.I)
    base_model = base_match.group(1).upper() if base_match else part_no.split()[0]
    voltage_note = _timer_voltage_note(part_no)

    lines = [
        "Hi, thank you for reaching out.",
        "",
        f"From your photo this is an {brand} {base_model} timer relay"
        + (
            f" ({voltage_note}, 8-pin octal base, DPDT contacts, 7A 250VAC resistive)."
            if voltage_note
            else " (8-pin octal base, DPDT contacts)."
        ),
        "",
        f"The modern direct equivalent / successor is the {brand} H3CR-A8 series "
        "(same 8-pin socket, improved accuracy and easier setting).",
    ]

    if warehouse_context:
        lines.extend([
            "",
            "We currently have matching variants Ex-Stock / available in our warehouse, for example:",
            warehouse_context,
        ])
    else:
        lines.extend(["", "We can source the matching H3CR-A8 variant for you."])

    lines.extend([
        "",
        "If you can share the time range on the dial (e.g. 5S, 30S, 3M) and whether "
        "you need ON-delay or OFF-delay, I can match the exact H3CR-A8 variant for you.",
    ])
    return "\n".join(lines)


def _sanitize_whatsapp_reply(text: str) -> str:
    """Strip markdown links/formatting Copilot sometimes adds."""
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = cleaned.replace("*", "")
    return cleaned.strip()


def _reply_mentions_e2e(reply: str) -> bool:
    return bool(re.search(r"\bE2E[\s\-]?X", str(reply or "").upper()))


def _reply_contradicts_visual_items(reply: str, items: list) -> bool:
    visual_has_e2e = any(_is_e2e_family_part(item.get("part_no")) for item in (items or []))
    if not visual_has_e2e and _reply_mentions_e2e(reply):
        print("[COPILOT TECH SUPPORT] Reply mentions E2E but visual extraction did not confirm E2E")
        return True
    for item in items or []:
        if not _is_timer_visual_item(item):
            continue
        part_key = _normalize_part_key(item.get("part_no"))
        if part_key.startswith("H3J") and _reply_mentions_e2e(reply):
            print("[COPILOT TECH SUPPORT] Reply contradicts timer label — mentions E2E")
            return True
    return False


def _ask_for_clearer_photo_reply(caption: str = "") -> str:
    return (
        "Hi, thank you for your message.\n\n"
        "I received your photo but could not read the product label clearly enough to quote accurately.\n\n"
        "Please resend a closer photo of the nameplate/label, or type the exact part number and quantity, for example:\n"
        "ER6C 3.6V Qty:1"
    )


def build_technical_support_reply(
    message_text: str = "",
    image_path: str = None,
    copilot_items: list = None,
) -> str:
    """Use unified Copilot analysis + warehouse stock for technical support replies."""
    if os.getenv("OPENCLAW_COPILOT_TECH_SUPPORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""

    from openclaw_inquiry_engine import build_warehouse_support_context

    message_text = str(message_text or "").strip()
    if not message_text and not image_path:
        return ""

    visual_items = list(copilot_items or [])
    if visual_items:
        print(f"[COPILOT TECH SUPPORT] Using {len(visual_items)} unified analyze item(s)")
    elif image_path:
        print("[COPILOT TECH SUPPORT] No unified items — Copilot will read zoomed screenshot directly")
    else:
        print("[COPILOT TECH SUPPORT] Text-only technical support")

    timer_items = [item for item in visual_items if _is_timer_visual_item(item)]

    if timer_items:
        part_refs, warehouse_context = build_warehouse_support_context(
            message_text,
            part_refs=_part_refs_from_copilot_items(timer_items),
        )
        print("[COPILOT TECH SUPPORT] Timer detected — using deterministic equivalent reply")
        return _build_timer_equivalent_reply(timer_items, warehouse_context)

    part_refs = _part_refs_from_copilot_items(visual_items)
    if image_path and _is_equivalent_support_request(message_text) and (
        not part_refs or all(_is_e2e_family_part(part) for part in part_refs)
    ):
        print("[COPILOT TECH SUPPORT] Blocking E2E-only guess on equivalent photo request")
        if not image_path:
            return _ask_for_clearer_photo_reply(message_text)
        part_refs = []

    part_refs, warehouse_context = build_warehouse_support_context(
        message_text,
        part_refs=part_refs if part_refs else None,
    )

    if part_refs:
        parts_label = ", ".join(part_refs)
        identification_note = (
            f"Model identified from unified analysis: {parts_label}. "
            "You MUST state this exact model in your reply. "
            "Do NOT substitute a different part family (e.g. never say E2E if the label is H3JA or ER6C)."
        )
    else:
        parts_label = "(read from attached zoomed screenshot)"
        identification_note = (
            "Read the attached zoomed WhatsApp product photo. State the exact printed model number "
            "(e.g. OMRON H3JA-8A timer, or ER6C 3.6V lithium battery). "
            "Never guess E2E-X5E1 or E3Z-T61 unless those exact codes are printed on the label."
        )

    print(f"[COPILOT TECH SUPPORT] Parts detected: {parts_label}")
    if warehouse_context:
        print("[COPILOT TECH SUPPORT] Warehouse matches found — prioritising in-stock SKUs")

    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=90.0 if image_path else 60.0,
        max_retries=1,
    )

    system_prompt = (
        "You are a senior industrial automation technical sales engineer at Robomatics (Malaysia). "
        "Answer customer technical support questions clearly and practically on WhatsApp. "
        "Read the attached zoomed product photo if provided — transcribe the label character-by-character. "
        "If a message-bubble screenshot is attached, analyze ONLY that single message — "
        "ignore other products from earlier/later chat media.\n"
        "Use ONLY the identified model from the label — never invent or substitute a different catalog number. "
        "NEVER mention E2E-X5E1 or E3Z-T61 unless those exact models are printed on the label. "
        "ER6C / LITHIUM = battery, not a sensor. H3JA / H3Y = timer, not E2E. "
        "When the customer asks for an equivalent, replacement, or successor part, recommend the "
        "best modern replacement and explain briefly why. "
        "For OMRON H3JA or H3Y timer relays, the usual modern successor is H3CR-A8 (8-pin socket, DPDT). "
        "ALWAYS prioritise recommending parts listed in the warehouse stock section below. "
        "If we have Ex-Stock, say Ex-Stock is available — never disclose warehouse quantity numbers. "
        "Do not recommend external distributors if we stock a suitable item. "
        "Start with 'From your photo this is...' when a photo is attached and the label is readable. "
        "Plain text only. No markdown, no hyperlinks, no asterisks. Friendly professional tone. Under 280 words."
    )

    user_prompt = (
        f"Customer message:\n{message_text or '(see attached zoomed product photo)'}\n\n"
        f"{identification_note}\n\n"
        "Our warehouse stock to PRIORITISE (check these first):\n"
        f"{warehouse_context or '(no matching warehouse stock found — give best technical guidance anyway)'}\n\n"
        "Write the WhatsApp reply to the customer now."
    )

    try:
        user_content = _copilot_user_content_with_image(user_prompt, image_path)
        if image_path and os.path.exists(image_path):
            print(f"[COPILOT TECH SUPPORT] Attaching exact message-bubble screenshot: {image_path}")

        response = _copilot_fresh_chat(
            client,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        text = _sanitize_whatsapp_reply(response.choices[0].message.content or "")
        if text.startswith("```"):
            lines = text.splitlines()
            text = _sanitize_whatsapp_reply("\n".join(lines[1:-1]))
        if not text:
            return _ask_for_clearer_photo_reply(message_text) if image_path else ""
        if _reply_contradicts_visual_items(text, visual_items):
            if any(_is_timer_visual_item(item) for item in visual_items):
                print("[COPILOT TECH SUPPORT] Regenerating with deterministic timer template")
                return _build_timer_equivalent_reply(
                    [item for item in visual_items if _is_timer_visual_item(item)] or visual_items,
                    warehouse_context,
                )
            return _ask_for_clearer_photo_reply(message_text)
        if not text.lower().startswith("hi"):
            text = f"Hi, thank you for reaching out.\n\n{text}"
        print(f"[COPILOT TECH SUPPORT] Generated {len(text)} char reply")
        return text
    except Exception as exc:
        print(f"[WARN] Copilot technical support failed: {exc}")
        if visual_items and any(_is_timer_visual_item(item) for item in visual_items):
            return _build_timer_equivalent_reply(visual_items, warehouse_context)
        return _ask_for_clearer_photo_reply(message_text) if image_path else ""


def run_process(name, script):
    print(f"🚀 Starting {name}...")
    return subprocess.Popen(
        ["uv", "run", "python", script],
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,
    )


def stop_process(proc, name):
    """Stop a service and its browser children gracefully, then force if needed."""
    if proc.poll() is not None:
        return
    print(f"   Stopping {name}...")
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except ProcessLookupError:
        pass


def main():
    print("=" * 90)
    print(f"🤖 OpenClaw Unified Runner {VERSION}")
    print("   Running Email + WhatsApp Automation (busy-flag / turn coordination)")
    print("   Flags: openclaw_busy.flag | openclaw_channel_turn.flag")
    print("=" * 90)

    email_proc = run_process("Email Engine (auto_claw)", EMAIL_SCRIPT)
    wa_proc = run_process("WhatsApp Engine", WHATSAPP_SCRIPT)

    try:
        while True:
            time.sleep(5)

            if email_proc.poll() is not None:
                print("❌ Email engine stopped. Restarting...")
                email_proc = run_process("Email Engine (auto_claw)", EMAIL_SCRIPT)

            if wa_proc.poll() is not None:
                print("❌ WhatsApp engine stopped. Restarting...")
                wa_proc = run_process("WhatsApp Engine", WHATSAPP_SCRIPT)

    except KeyboardInterrupt:
        print("\n🛑 Stopping all services...")

        stop_process(email_proc, "Email Engine")
        stop_process(wa_proc, "WhatsApp Engine")

        print("✅ All stopped.")


if __name__ == "__main__":
    main()
