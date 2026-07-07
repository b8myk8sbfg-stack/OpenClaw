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

VERSION = "v1.38-STRICT-JSON"

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


COPILOT_ANALYZE_INTENTS = {
    "rfq_inquiry",
    "technical_support",
    "purchase_order",
    "replacement_request",
    "repair",
    "delivery_tracking",
    "payment_invoice",
    "supplier_reply",
    "order_confirmation",
    "complaint",
    "greeting",
    "general_chat",
    "junk_ad",
    "junk",
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
        "replacement_request": "technical_support",
        "repair": "technical_support",
        "purchase_order": "purchase_order",
        "po": "purchase_order",
        "junk": "junk_ad",
        "spam": "junk_ad",
        "advertisement": "junk_ad",
    }
    if intent_u in aliases:
        return aliases[intent_u]
    if intent_u in COPILOT_ANALYZE_INTENTS:
        if intent_u == "junk":
            return "junk_ad"
        if intent_u in ("replacement_request", "repair"):
            return "technical_support"
        return intent_u
    return "unknown"


EXTRACTION_ERRORS = frozenset({
    "PARSER_ERROR",
    "JSON_INVALID",
    "OCR_NO_TEXT",
    "NO_PRODUCT_FOUND",
    "NO_FOREGROUND_OBJECT",
    "LOW_CONFIDENCE",
})

REQUIRED_EXTRACTION_FIELDS = ("status", "intent", "input_type", "items")

OPENCLAW_UNIFIED_PROMPT = """CRITICAL RULES (override every other instruction):
1. Never guess. Never autocomplete. Never correct spelling.
2. Never use previous conversations or warehouse/catalog memory.
3. Literal transcription only. If unreadable use ?.
4. Never substitute familiar catalog numbers (e.g. E3Z-D61, H3JA-8A) unless literally visible.
5. Output ONLY valid JSON. No markdown. No prose. No analysis. No code fences. No text before or after JSON.

You are an Industrial RFQ Extraction Engine. Extract literal evidence only — do NOT identify products from memory.

STEP 1 — Classify input_type (choose ONE):
text_message | single_product_photo | multiple_product_photo | rfq_table | purchase_order | invoice | manual | datasheet | panel_photo | document | mixed | unknown

STEP 2 — Determine PRIMARY inquiry object (choose ONE before any extraction):
A. Single handheld object — held in hand or closest to camera
B. RFQ table — spreadsheet/form with rows and columns
C. Nameplate — close-up label only
D. Purchase order — PO lines
E. Panel overview — only if customer clearly quotes panel equipment
F. Manual / G datasheet cover
G. Text/voice message only

Only extract from the PRIMARY inquiry object.

STEP 3 — Extraction rules:

single_product_photo / handheld (A):
- Primary object = held in hand OR nearest to camera
- Ignore ALL background: terminal blocks, relays, sensors, PLCs, control panels, wiring
- Describe shape first, then transcribe label character-by-character
- Never read background equipment labels

rfq_table (B):
- Every visible row = one item in items[]
- qty: from Qty column — never default to 1 if Qty column shows another number
- part_no: from Item/Model/Catalog column; use Picture column only to confirm unreadable text
- Do NOT apply handheld/foreground rules to tables

purchase_order (D): one item per PO line.
panel_photo (E): only products the customer clearly intends to quote.

STEP 4 — intent (choose ONE):
rfq_inquiry | purchase_order | technical_support | replacement_request | repair | complaint | general_chat | greeting | junk | unknown

part_no must be literal transcription. Never normalize. Never improve.

Return ONLY this JSON object (no other text):
{
  "status": "success",
  "intent": "rfq_inquiry",
  "input_type": "single_product_photo",
  "primary_subject": "battery",
  "confidence": 0.99,
  "items": [
    {
      "brand": "",
      "part_no": "ER6C (AA)-3.6V",
      "description": "Lithium battery",
      "product_type": "battery",
      "qty": 1,
      "source": "foreground label",
      "confidence": 0.99,
      "reason": "Foreground label fully readable"
    }
  ],
  "ignored": ["terminal block", "control panel"],
  "technical_summary": "",
  "reasoning": ""
}

If no products found: status="no_products", items=[]. If label unreadable: status="ocr_no_text", items=[].
One product = one item. One RFQ row = one item. One PO line = one item.
"""

OPENCLAW_JSON_RETRY_PROMPT = """CRITICAL: Your previous response was NOT valid JSON or failed validation.

Return ONLY one valid JSON object. No markdown. No prose. No code fences. No text before or after.

Required fields: status, intent, input_type, items (array).
Use literal part_no transcription only. Never guess catalog numbers.

Schema:
{
  "status": "success",
  "intent": "rfq_inquiry",
  "input_type": "single_product_photo",
  "primary_subject": "",
  "confidence": 0.0,
  "items": [{"brand":"","part_no":"","description":"","product_type":"","qty":1,"source":"","confidence":0.0,"reason":""}],
  "ignored": [],
  "technical_summary": "",
  "reasoning": ""
}
"""

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
    extraction_error: str = "PARSER_ERROR",
) -> dict:
    """Structured failure result — never masquerade parser errors as successful analyze."""
    prose = str(analysis_text or raw or "").strip()
    return {
        "attempted": True,
        "ok": False,
        "intent": "unknown",
        "confidence": 0.0,
        "input_type": "",
        "extraction_error": extraction_error,
        "reasoning": parse_warning or extraction_error,
        "items": [],
        "technical_summary": _sanitize_whatsapp_reply(prose) if prose else "",
        "analysis_text": prose,
        "is_industrial_automation": True,
        "compatible_brands": [],
        "ignored": [],
        "raw_excerpt": str(raw or prose)[:800],
        "http_status": http_status,
        "parse_warning": parse_warning or extraction_error,
    }


def is_extraction_parse_failure(analysis: dict) -> bool:
    """True when Copilot responded but JSON could not be parsed/validated."""
    if not isinstance(analysis, dict):
        return False
    err = str(analysis.get("extraction_error") or "").upper()
    return err in ("PARSER_ERROR", "JSON_INVALID")


def _strip_markdown_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    return cleaned


def _fix_trailing_commas(json_text: str) -> str:
    """Remove trailing commas before } or ] — common model mistake."""
    return re.sub(r",(\s*[}\]])", r"\1", json_text)


def _try_parse_json_candidate(candidate: str):
    if not candidate:
        return None
    for variant in (candidate, _fix_trailing_commas(candidate)):
        try:
            parsed = json.loads(variant)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _extract_json_substring_first_to_last(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start:end + 1]


def parse_copilot_json_response(raw: str):
    """
    Robust JSON extraction from Copilot output.
    Returns (parsed_dict|None, error_code|None, detail).
    """
    text = _strip_markdown_fences(str(raw or "").strip())
    if not text:
        return None, "JSON_INVALID", "empty response"

    candidates = []
    if text.startswith("{"):
        candidates.append(text)
    substring = _extract_json_substring_first_to_last(text)
    if substring and substring not in candidates:
        candidates.append(substring)
    balanced = _extract_balanced_json_object(text)
    if balanced and balanced not in candidates:
        candidates.append(balanced)
    for match in re.finditer(r"\{", text):
        fragment = _extract_balanced_json_object(text[match.start():])
        if fragment and fragment not in candidates:
            candidates.append(fragment)

    for candidate in candidates:
        parsed = _try_parse_json_candidate(candidate)
        if parsed is not None:
            return parsed, None, ""

    return None, "JSON_INVALID", "no parseable JSON object found"


def _validate_copilot_extraction_json(parsed: dict):
    """Validate required contract fields. Returns (ok, missing_fields)."""
    if not isinstance(parsed, dict):
        return False, ["not_a_dict"]
    missing = [field for field in REQUIRED_EXTRACTION_FIELDS if field not in parsed]
    if missing:
        return False, missing
    if not isinstance(parsed.get("items"), list):
        return False, ["items_not_array"]
    return True, []


def _classify_empty_extraction(parsed: dict, image_path: str = None) -> str:
    """Map valid JSON with zero items to a specific extraction error."""
    status = str(parsed.get("status") or "").strip().lower()
    if status in ("ocr_no_text", "no_text", "unreadable"):
        return "OCR_NO_TEXT"
    if status in ("no_products", "no_product", "not_found"):
        return "NO_PRODUCT_FOUND"
    input_type = str(parsed.get("input_type") or "").strip().lower()
    if image_path and input_type in ("single_product_photo", "multiple_product_photo"):
        if not str(parsed.get("primary_subject") or "").strip():
            return "NO_FOREGROUND_OBJECT"
    confidence = parse_copilot_confidence(parsed.get("confidence"), default=0.75)
    if confidence < 0.45:
        return "LOW_CONFIDENCE"
    return "NO_PRODUCT_FOUND"


def _build_extraction_user_prompt(
    message_text: str = "",
    document_text: str = "",
    voice_transcript: str = "",
    base_prompt: str = None,
) -> str:
    prompt_parts = [base_prompt or OPENCLAW_UNIFIED_PROMPT]
    if voice_transcript:
        prompt_parts.append(
            "Attached voice message (already transcribed from WAV):\n"
            f"{voice_transcript}"
        )
    if message_text:
        prompt_parts.append(f"Attached customer message/caption:\n{message_text}")
    if document_text:
        prompt_parts.append(f"Attached document text:\n{document_text[:4000]}")
    return "\n\n".join(prompt_parts)


def _copilot_extraction_result_from_parsed(
    parsed: dict,
    raw: str,
    message_text: str = "",
    voice_transcript: str = "",
    image_path: str = None,
) -> dict:
    """Build normalized analyze result from validated Copilot JSON."""
    intent = _normalize_copilot_intent(parsed.get("intent"))
    confidence = parse_copilot_confidence(parsed.get("confidence"), default=0.75)
    input_type = str(parsed.get("input_type") or "").strip()
    primary_subject = str(parsed.get("primary_subject") or "").strip()
    ignored = parsed.get("ignored") or []
    if not isinstance(ignored, list):
        ignored = [str(ignored)]
    technical_summary = _sanitize_whatsapp_reply(
        str(parsed.get("technical_summary") or "").strip()
    )

    items_out = _parse_copilot_items_from_dict(
        parsed,
        message_text=message_text,
        voice_transcript=voice_transcript,
        input_type=input_type,
    )
    reasoning = _build_copilot_reasoning(parsed, items_out)

    if _is_equivalent_support_request(message_text):
        intent = "technical_support"
        if not reasoning or re.search(r"\brfq\b", reasoning, re.I):
            reasoning = (
                "Equivalent/replacement request — technical support, "
                "reading product label from photo."
            )

    extraction_error = ""
    if not items_out:
        extraction_error = _classify_empty_extraction(parsed, image_path=image_path)
        print(
            f"[COPILOT ANALYZE] Zero items after validation — "
            f"extraction_error={extraction_error}"
        )

    status = str(parsed.get("status") or "").strip().lower()
    ok = bool(items_out) and status not in ("error", "ocr_no_text", "no_products")

    result = {
        "attempted": True,
        "ok": ok,
        "intent": intent,
        "confidence": max(0.0, min(confidence, 1.0)),
        "input_type": input_type,
        "primary_subject": primary_subject,
        "extraction_error": extraction_error or None,
        "reasoning": reasoning,
        "items": items_out,
        "technical_summary": technical_summary,
        "analysis_text": "",
        "is_industrial_automation": True,
        "compatible_brands": [],
        "ignored": ignored,
        "raw_excerpt": raw[:800],
        "parse_warning": extraction_error or None,
    }
    print(
        f"[COPILOT ANALYZE] intent={intent} ({confidence:.0%}) | "
        f"input_type={input_type or 'n/a'} | items={len(items_out)} | "
        f"error={extraction_error or 'none'} | {reasoning[:60]}"
    )
    return result


def _extract_json_from_copilot_text(raw: str):
    """Legacy wrapper — returns (prose, parsed) for any remaining callers."""
    parsed, error, _detail = parse_copilot_json_response(raw)
    if parsed is not None:
        return "", parsed
    return str(raw or "").strip(), {}


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


def _parse_copilot_items_from_dict(
    parsed: dict,
    message_text: str = "",
    voice_transcript: str = "",
    input_type: str = "",
) -> list:
    """Normalize items array from Copilot JSON — preserve literal part_no transcription."""
    caption_qty = _parse_caption_qty(message_text or voice_transcript, default=1)
    table_mode = str(input_type or parsed.get("input_type") or "").strip().lower() == "rfq_table"
    items_out = []
    for item in parsed.get("items") or []:
        if not isinstance(item, dict):
            continue
        part_no = str(item.get("part_no") or "").strip()
        if not part_no:
            continue
        if "qty" in item and item.get("qty") is not None and str(item.get("qty")).strip() != "":
            try:
                qty = _parse_copilot_qty(item.get("qty"), default=1)
            except (TypeError, ValueError):
                qty = 1
        elif table_mode:
            qty = 1
            print("[COPILOT ANALYZE] WARN rfq_table row missing qty — defaulting to 1")
        else:
            qty = caption_qty or 1
        qty = max(1, qty)
        brand = str(item.get("brand") or "UNKNOWN").strip().upper()
        product_type = str(item.get("product_type") or "").strip()
        description = str(item.get("description") or "").strip()
        evidence = str(item.get("source") or "").strip()
        item_reason = str(item.get("reason") or "").strip()
        item_confidence = parse_copilot_confidence(item.get("confidence"), default=None)
        item_out = {
            "part_no": part_no,
            "qty": qty,
            "brand": brand,
            "source": "COPILOT_UNIFIED",
        }
        if product_type:
            item_out["product_type"] = product_type
        if description:
            item_out["description"] = description
        if evidence:
            item_out["evidence_source"] = evidence
        if item_reason:
            item_out["reason"] = item_reason
        if item_confidence is not None:
            item_out["confidence"] = item_confidence
        items_out.append(item_out)
    return items_out


def _build_copilot_reasoning(parsed: dict, items_out: list) -> str:
    """Build monitor reasoning from Copilot JSON fields."""
    reasoning = str(parsed.get("reasoning") or "").strip()
    input_type = str(parsed.get("input_type") or "").strip()
    if items_out:
        part_labels = ", ".join(
            f"{it.get('part_no')} x{it.get('qty', 1)}" for it in items_out[:4]
        )
        if len(items_out) > 4:
            part_labels += f" (+{len(items_out) - 4} more)"
        prefix = f"Copilot {input_type or 'extract'}: {len(items_out)} item(s)"
        if input_type == "rfq_table":
            prefix = f"Copilot RFQ table: {len(items_out)} row(s)"
        elif input_type == "single_product_photo":
            prefix = "Copilot label read"
        summary = f"{prefix} — {part_labels}"
        if reasoning:
            return f"{summary} | {reasoning[:120]}"
        return summary
    if input_type and reasoning:
        return f"[{input_type}] {reasoning}"
    return reasoning or (f"Copilot input_type={input_type}" if input_type else "")


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
            from whatsapp_attachment_processor import validate_image_file, read_image_dimensions

            img_ok, img_reason = validate_image_file(image_path)
            img_size = os.path.getsize(image_path)
            dims = read_image_dimensions(image_path)
            dim_label = f"{dims[0]}x{dims[1]}" if dims else "unknown"
            print(
                f"[COPILOT ANALYZE] Image file size: {img_size} bytes, "
                f"dimensions: {dim_label} "
                f"({'valid' if img_ok else 'INVALID: ' + img_reason})"
            )
            if dims and (dims[0] < 400 or dims[1] < 400):
                print(
                    f"[COPILOT ANALYZE] Image dimensions {dim_label} — "
                    "resolution alone does not block extraction"
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

    user_text = _build_extraction_user_prompt(
        message_text=message_text,
        document_text=document_text,
        voice_transcript=voice_transcript,
    )
    user_content = _copilot_user_content_with_image(user_text, image_path)
    if image_path and os.path.exists(image_path):
        print(f"[COPILOT ANALYZE] Fresh chat — attached image: {image_path}")

    def _call_and_parse(prompt_text: str, label: str):
        content = _copilot_user_content_with_image(prompt_text, image_path)
        response = _copilot_fresh_chat(
            client,
            [{"role": "user", "content": content}],
            timeout=120.0 if image_path else 60.0,
        )
        response_raw = (response.choices[0].message.content or "").strip()
        print(f"[COPILOT ANALYZE RAW/{label}] {response_raw[:500]}")
        parsed_obj, parse_err, parse_detail = parse_copilot_json_response(response_raw)
        return response_raw, parsed_obj, parse_err, parse_detail

    raw = ""
    try:
        raw, parsed, parse_error, parse_detail = _call_and_parse(user_text, "pass1")

        if parsed is None:
            print(f"[COPILOT ANALYZE] JSON parse failed ({parse_error}): {parse_detail}")
            retry_prompt = (
                _build_extraction_user_prompt(
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    base_prompt=OPENCLAW_JSON_RETRY_PROMPT,
                )
                + f"\n\nYour invalid response was:\n{raw[:1200]}"
            )
            raw, parsed, parse_error, parse_detail = _call_and_parse(retry_prompt, "pass2-json-retry")

        if parsed is None:
            print(f"[COPILOT ANALYZE] PARSER_ERROR after retry: {parse_detail}")
            return _minimal_copilot_analysis_result(
                message_text=message_text,
                analysis_text=raw,
                raw=raw,
                parse_warning=f"JSON parse failed after retry: {parse_detail}",
                http_status=200,
                extraction_error="PARSER_ERROR",
            )

        valid, missing = _validate_copilot_extraction_json(parsed)
        if not valid:
            print(f"[COPILOT ANALYZE] JSON_INVALID — missing/invalid fields: {missing}")
            return _minimal_copilot_analysis_result(
                message_text=message_text,
                raw=raw,
                parse_warning=f"JSON validation failed: {', '.join(missing)}",
                http_status=200,
                extraction_error="JSON_INVALID",
            )

        return _copilot_extraction_result_from_parsed(
            parsed,
            raw,
            message_text=message_text,
            voice_transcript=voice_transcript,
            image_path=image_path,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[WARN] Copilot analyze parse issue (HTTP 200): {exc}")
        return _minimal_copilot_analysis_result(
            message_text=message_text,
            analysis_text=raw,
            raw=raw,
            parse_warning=f"invalid JSON from Copilot: {exc}",
            http_status=200,
            extraction_error="JSON_INVALID",
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
    """Extract RFQ items via the same single-pass Copilot analyze used by WhatsApp."""
    analysis = analyze_incoming_inquiry_with_copilot(
        message_text=raw_email_body,
        image_path=image_path,
    )
    return list(analysis.get("items") or [])


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


def build_photo_confirmation_line(items: list) -> str:
    """One-line visual confirmation using Copilot's product_type — no local part-family rules."""
    if not items:
        return ""
    primary = items[0]
    part_no = str(primary.get("part_no") or "").strip().upper()
    brand = str(primary.get("brand") or "").strip().upper()
    product_type = str(primary.get("product_type") or "").strip()
    if not part_no:
        return ""
    brand_bit = f"{brand} " if brand and brand != "UNKNOWN" else ""
    if product_type:
        return f"From your photo this is a {brand_bit}{part_no} ({product_type.lower()})."
    return f"From your photo this is a {brand_bit}{part_no}."



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




def _sanitize_whatsapp_reply(text: str) -> str:
    """Strip markdown links/formatting Copilot sometimes adds."""
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = cleaned.replace("*", "")
    return cleaned.strip()




def build_extraction_failure_customer_reply(
    copilot_analysis: dict = None,
    image_path: str = None,
) -> tuple:
    """
    Return (reply_text, context_code) for failed extraction.
    Parser failures must NOT ask for a clearer image.
    """
    analysis = copilot_analysis or {}
    err = str(analysis.get("extraction_error") or "").upper()

    if is_extraction_parse_failure(analysis):
        return (
            "Hi, thank you for your inquiry.\n\n"
            "We received your message and our team is reviewing it now. "
            "To speed up the quotation, please also type the part number(s) and quantity, for example:\n"
            "150-C25NBD Qty:3",
            "PARSER_ERROR",
        )

    if err == "OCR_NO_TEXT":
        return (_ask_for_clearer_photo_reply(), "OCR_NO_TEXT")

    if err in ("NO_PRODUCT_FOUND", "NO_FOREGROUND_OBJECT"):
        if image_path:
            return (_ask_for_clearer_photo_reply(), err)
        return (
            "Hi, I received your message but could not detect part numbers.\n\n"
            "Please send in this format:\n"
            "150-C25NBD Qty:3",
            err or "NO_PRODUCT_FOUND",
        )

    if err == "LOW_CONFIDENCE":
        return (
            "Hi, thank you for your inquiry.\n\n"
            "We received your photo but are not fully confident in the part numbers read. "
            "Please confirm by typing the exact part number(s) and quantity, for example:\n"
            "ER6C (AA)-3.6V Qty:1",
            "LOW_CONFIDENCE",
        )

    if image_path:
        return (_ask_for_clearer_photo_reply(), "NO_PRODUCT_FOUND")
    return (
        "Hi, I received your WhatsApp message, but I could not detect item details.\n\n"
        "Please send in this format:\n"
        "150-C25NBD Qty:3",
        "NO_PRODUCT_FOUND",
    )


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
    """Use unified Copilot analysis items + one Copilot reply call (no local re-OCR)."""
    if os.getenv("OPENCLAW_COPILOT_TECH_SUPPORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""

    from openclaw_inquiry_engine import build_warehouse_support_context

    message_text = str(message_text or "").strip()
    if not message_text and not image_path:
        return ""

    visual_items = list(copilot_items or [])
    if visual_items:
        print(f"[COPILOT TECH SUPPORT] Using {len(visual_items)} item(s) from unified analyze")
    elif image_path:
        print("[COPILOT TECH SUPPORT] No prior items — Copilot will read the attached image")
    else:
        print("[COPILOT TECH SUPPORT] Text-only technical support")

    part_refs = _part_refs_from_copilot_items(visual_items)
    part_refs, warehouse_context = build_warehouse_support_context(
        message_text,
        part_refs=part_refs if part_refs else None,
    )

    if part_refs:
        parts_label = ", ".join(part_refs)
        identification_note = (
            f"Unified analysis identified: {parts_label}. "
            "State this exact model in your reply — do not substitute a different part."
        )
    else:
        parts_label = "(read from attached image)"
        identification_note = (
            "Read the attached product photo. Transcribe the foreground label character-by-character "
            "and identify what product the customer is asking about."
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
        "Read the attached product photo if provided — transcribe the label character-by-character. "
        "Focus on the foreground product the customer is asking about; ignore background equipment "
        "unless the customer is clearly quoting it.\n"
        "Use only what you can read on the label — never invent or substitute a different catalog number. "
        "When the customer asks for an equivalent, replacement, or successor, recommend the best "
        "modern replacement and explain briefly why.\n"
        "ALWAYS prioritise parts listed in the warehouse stock section below when we have Ex-Stock. "
        "Never disclose warehouse quantity numbers. "
        "Plain text only. No markdown, no hyperlinks, no asterisks. Friendly professional tone. Under 280 words."
    )

    user_prompt = (
        f"Customer message:\n{message_text or '(see attached product photo)'}\n\n"
        f"{identification_note}\n\n"
        "Our warehouse stock to PRIORITISE (check these first):\n"
        f"{warehouse_context or '(no matching warehouse stock found — give best technical guidance anyway)'}\n\n"
        "Write the WhatsApp reply to the customer now."
    )

    try:
        user_content = _copilot_user_content_with_image(user_prompt, image_path)
        if image_path and os.path.exists(image_path):
            print(f"[COPILOT TECH SUPPORT] Attaching product screenshot: {image_path}")

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
        if not text.lower().startswith("hi"):
            text = f"Hi, thank you for reaching out.\n\n{text}"
        print(f"[COPILOT TECH SUPPORT] Generated {len(text)} char reply")
        return text
    except Exception as exc:
        print(f"[WARN] Copilot technical support failed: {exc}")
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
