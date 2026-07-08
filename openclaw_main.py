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

VERSION = "v1.48-MANUAL-PARITY-TEST"

# Part prefixes Copilot often hallucinates without reading the image (post-parse guard only).
COMMON_CATALOG_DEFAULT_PREFIXES = (
    "E3Z", "E2E", "E39", "ER6C", "H3JA", "H3CR", "H3Y", "MY2", "MY4", "3RH", "G3NA",
    "3G3M", "3G3MX", "G3MX", "E3X", "E3S",
    "3RT", "3RV", "3RU", "3RP", "3RA", "6ES", "6EP", "6SL", "1FK", "LC1D", "LC1F",
)

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")
COPILOT_EXTRACTION_LOG = os.path.join(BASE_DIR, "logs", "copilot_extraction.log")

RFQ_TABLE_VERIFY_CONFIDENCE = float(os.getenv("OPENCLAW_RFQ_TABLE_VERIFY_CONFIDENCE", "0.55"))
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

# Copilot returned valid JSON but could not read products — not an API outage.
BUSINESS_EXTRACTION_ERRORS = frozenset({
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
4. Never substitute familiar catalog numbers from model memory unless those exact characters are visible in the image.
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
- If the image shows a grid/table with column headers (No, Item, Picture, Qty, Description, Model) → input_type MUST be rfq_table, NOT single_product_photo
- Wide landscape screenshots of spreadsheet rows are almost always rfq_table
- Count visible rows — if only ONE row is visible, return exactly ONE item in items[]
- Never invent extra rows that are not visible in the image
- Every visible row = one item in items[]
- qty: from Qty column — never default to 1 if Qty column shows another number
- part_no: from Item/Model/Catalog column; use Picture column nameplate to confirm
- Do NOT apply handheld/foreground/battery rules to tables
- Do NOT return status ocr_no_text for rfq_table when ANY column header or cell text is visible
- If some characters are unclear, use ? in part_no — still return status success with items[]

purchase_order (D): one item per PO line.
panel_photo (E): only products the customer clearly intends to quote.

STEP 4 — intent (choose ONE):
rfq_inquiry | purchase_order | technical_support | replacement_request | repair | complaint | general_chat | greeting | junk | unknown

part_no must be literal transcription. Never normalize. Never improve.

Return ONLY this JSON object (no other text). Use YOUR OWN readings — do NOT copy example values:
{
  "status": "success",
  "intent": "rfq_inquiry",
  "input_type": "rfq_table",
  "primary_subject": "rfq table row",
  "confidence": 0.0,
  "items": [
    {
      "brand": "",
      "part_no": "",
      "description": "",
      "product_type": "",
      "qty": 1,
      "source": "table row",
      "confidence": 0.0,
      "reason": ""
    }
  ],
  "ignored": [],
  "technical_summary": "",
  "reasoning": ""
}

If no products found: status="no_products", items=[].
For rfq_table with ANY visible cell or header text: status MUST be "success" with items[] — never ocr_no_text.
For nameplate-only photos with zero readable characters: status="ocr_no_text", items=[].
One product = one item. One RFQ row = one item. One PO line = one item.
"""

OPENCLAW_TABLE_RETRY_PROMPT = """CRITICAL RE-ANALYSIS — your previous classification was wrong.

The attached image is a WIDE landscape RFQ / quotation TABLE (columns such as No, Item, Picture, Qty).
You must NOT classify this as single_product_photo or a handheld battery.

Rules:
- input_type MUST be rfq_table
- Count visible rows — if only ONE row is visible, return exactly ONE item
- Never invent extra rows that are not visible in the image
- part_no: literal transcription from Item/Model column and/or nameplate in Picture column
- qty: from Qty column for that row
- brand: from Item text or nameplate (manufacturer name visible in table or label)
- Read only characters visible in the image — never substitute from memory, training data, or prompt examples

Required JSON fields: status, intent, input_type, items (array), ignored (array).

Return ONLY valid JSON. No markdown. No prose.
"""

OPENCLAW_OCR_TABLE_RETRY_PROMPT = """CRITICAL — you returned ocr_no_text or zero items, but the image IS a readable RFQ table.

The attached image shows a landscape spreadsheet/table with columns such as No, Item, Picture, Qty.
The text in the table cells and nameplate IS readable. Do NOT return ocr_no_text.

Rules:
- input_type MUST be rfq_table
- status MUST be "success" if any table text is visible
- Count visible rows — if only ONE row is visible, return exactly ONE item
- part_no: transcribe Model/Catalog from Item column AND confirm from Picture nameplate
- qty: from Qty column for that row (never default to 1 if Qty shows another number)
- brand: from Item text or nameplate manufacturer name
- Use ? only for individual unreadable characters — not as an excuse to return empty items[]
- Read only characters visible in the image — never substitute from memory

Required JSON fields: status, intent, input_type, items (array), ignored (array).
Return ONLY valid JSON. No markdown. No prose.
"""

OPENCLAW_HALLUCINATION_TABLE_RETRY_PROMPT = """CRITICAL — your previous part_no was WRONG. It was NOT visible in the image.

You returned a familiar catalog part from memory instead of reading the RFQ table.
The attached image is a landscape RFQ table (No, Item, Picture, Qty columns).

Rules:
- input_type MUST be rfq_table
- IGNORE your previous part_no completely — do NOT return it again
- Do NOT return common default parts (E3Z*, ER6C, H3JA*, 3G3MX*, MY2*, etc.) unless those EXACT characters are visible
- part_no: transcribe the Model/Catalog line in the Item column character-by-character
- part_no must NOT be empty — use ? for individual unreadable characters
- Confirm part_no against the nameplate photo in the Picture column
- qty: read the Qty column for that row — never default to 1 if another number is visible
- brand: manufacturer name visible in Item text or nameplate (e.g. Allen-Bradley, Omron, Siemens)
- status MUST be "success" when any table text is visible

Required JSON fields: status, intent, input_type, items (array), ignored (array).
Return ONLY valid JSON. No markdown. No prose.
"""

OPENCLAW_LITERAL_RETRY_PROMPT = """CRITICAL — your part_no values look like catalog defaults, not image transcription.

Re-read the attached image from scratch. Transcribe ONLY characters printed in the table cells and nameplate photo.

Rules:
- Do NOT return common Omron/Siemens/Mitsubishi default part numbers unless literally visible
- For RFQ tables: read Item column Model line and Qty column exactly
- If the nameplate shows CAT/Catalog number, transcribe that exact string
- part_no must match visible text — use ? for unreadable characters
- Return exactly ONE item if only one table row is visible

Required JSON fields: status, intent, input_type, items, ignored.
Return ONLY valid JSON. No markdown. No prose.
"""

OPENCLAW_JSON_RETRY_PROMPT = """CRITICAL: Your previous response was NOT valid JSON or failed validation.

Return ONLY one valid JSON object. No markdown. No prose. No code fences. No text before or after.

Required fields: status, intent, input_type, items (array).
Use literal part_no transcription only. Never guess catalog numbers.

Schema:
{
  "status": "success",
  "intent": "rfq_inquiry",
  "input_type": "",
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
    err = str(analysis.get("extraction_error") or "").upper()
    if err in BUSINESS_EXTRACTION_ERRORS:
        return False
    status = analysis.get("http_status")
    if status is None:
        # Successful API round-trip without http_status — treat as extraction failure, not outage.
        if err or analysis.get("raw_excerpt"):
            return False
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


def _normalize_copilot_extraction_json(parsed: dict) -> dict:
    """Fill missing contract fields so valid extractions are not discarded."""
    if not isinstance(parsed, dict):
        return {}
    out = dict(parsed)
    items = out.get("items")
    if not isinstance(items, list):
        items = []
        out["items"] = items
    if not str(out.get("status") or "").strip():
        out["status"] = "success" if items else "no_products"
    if not str(out.get("intent") or "").strip():
        out["intent"] = "rfq_inquiry"
    if not str(out.get("input_type") or "").strip():
        out["input_type"] = "unknown"
    if "ignored" not in out or not isinstance(out.get("ignored"), list):
        out["ignored"] = list(out.get("ignored") or []) if out.get("ignored") else []
    if out.get("confidence") is None:
        out["confidence"] = 0.75
    if "primary_subject" not in out:
        out["primary_subject"] = ""
    if "technical_summary" not in out:
        out["technical_summary"] = ""
    if "reasoning" not in out:
        out["reasoning"] = ""
    return out


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


def _copilot_single_pass_enabled(override: bool = None) -> bool:
    if override is not None:
        return bool(override)
    return os.getenv("OPENCLAW_COPILOT_SINGLE_PASS", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _copilot_manual_parity_prompt_enabled(override: bool = None) -> bool:
    if override is not None:
        return bool(override)
    return os.getenv("OPENCLAW_COPILOT_MANUAL_PARITY", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _build_extraction_user_prompt(
    message_text: str = "",
    document_text: str = "",
    voice_transcript: str = "",
    base_prompt: str = None,
    image_path: str = None,
    image_dims: tuple = None,
    minimal: bool = False,
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
    if image_path and not minimal:
        dim_label = (
            f"{image_dims[0]}x{image_dims[1]}"
            if image_dims and image_dims[0] and image_dims[1]
            else "unknown"
        )
        landscape = (
            image_dims
            and image_dims[0] > 0
            and image_dims[1] > 0
            and image_dims[0] > image_dims[1] * 1.35
        )
        prompt_parts.append(
            f"Attached image file: {image_path}\n"
            f"Image dimensions: {dim_label}"
            + (" (landscape — check for RFQ table with No/Item/Picture/Qty columns)" if landscape else "")
        )
    return "\n\n".join(prompt_parts)


def _is_landscape_rfq_image(image_dims: tuple = None, message_text: str = "") -> bool:
    """True when image layout or caption suggests an RFQ table screenshot."""
    if not image_dims or len(image_dims) < 2:
        return False
    width, height = int(image_dims[0] or 0), int(image_dims[1] or 0)
    if width > 0 and height > 0 and width > height * 1.35:
        return True
    caption_u = str(message_text or "").upper()
    return bool(re.search(r"\b(QUOTE|RFQ|QUOTATION|PLS QUOTE)\b", caption_u)) and width > height


def _parsed_items_with_part_no(parsed: dict) -> list:
    if not isinstance(parsed, dict):
        return []
    return [
        item for item in (parsed.get("items") or [])
        if isinstance(item, dict) and str(item.get("part_no") or "").strip()
    ]


def _should_retry_empty_table_extraction(
    parsed: dict,
    image_dims: tuple = None,
    image_path: str = None,
    message_text: str = "",
) -> bool:
    """Retry when Copilot classifies a table but returns ocr_no_text / zero items."""
    if not image_path or not isinstance(parsed, dict):
        return False
    if _parsed_items_with_part_no(parsed):
        return False
    status = str(parsed.get("status") or "").strip().lower()
    input_type = str(parsed.get("input_type") or "").strip().lower()
    landscape = _is_landscape_rfq_image(image_dims, message_text)
    if input_type == "rfq_table":
        return True
    if landscape and status in ("ocr_no_text", "no_products", "no_text", ""):
        return True
    return False


def _should_adopt_ocr_table_retry(parsed_retry: dict) -> bool:
    """Adopt OCR table retry when it returns at least one part_no."""
    if not isinstance(parsed_retry, dict):
        return False
    status = str(parsed_retry.get("status") or "").strip().lower()
    if status in ("ocr_no_text", "no_products", "error"):
        return False
    return len(_parsed_items_with_part_no(parsed_retry)) >= 1


def _max_table_confidence(parsed: dict) -> float:
    """Highest confidence on the extraction JSON or any item row."""
    if not isinstance(parsed, dict):
        return 0.0
    confidences = []
    if "confidence" in parsed:
        confidences.append(parse_copilot_confidence(parsed.get("confidence"), default=0.0))
    for item in parsed.get("items") or []:
        if isinstance(item, dict) and "confidence" in item:
            confidences.append(parse_copilot_confidence(item.get("confidence"), default=0.0))
    return max(confidences) if confidences else 0.0


def _should_verify_rfq_table_extraction(
    parsed: dict,
    image_dims: tuple = None,
    message_text: str = "",
) -> bool:
    """Re-read landscape RFQ tables when pass1 looks unreliable."""
    if not isinstance(parsed, dict) or not _parsed_items_with_part_no(parsed):
        return False
    if str(parsed.get("input_type") or "").strip().lower() != "rfq_table":
        return False
    if not _is_landscape_rfq_image(image_dims, message_text):
        return False
    if _suspect_catalog_default_extraction(parsed, message_text):
        return True
    if _max_table_confidence(parsed) < RFQ_TABLE_VERIFY_CONFIDENCE:
        return True
    for item in parsed.get("items") or []:
        if isinstance(item, dict) and not str(item.get("brand") or "").strip():
            return True
    return False


def _bad_part_numbers_from_parsed(parsed: dict) -> list:
    if not isinstance(parsed, dict):
        return []
    return [
        str(item.get("part_no") or "").strip().upper()
        for item in (parsed.get("items") or [])
        if isinstance(item, dict) and str(item.get("part_no") or "").strip()
    ]


def _is_adoptable_table_extraction(parsed_retry: dict) -> bool:
    """True when retry JSON has real part_no values that are not catalog-default guesses."""
    if not _should_adopt_ocr_table_retry(parsed_retry):
        return False
    if _suspect_catalog_default_extraction(parsed_retry):
        return False
    for part in _bad_part_numbers_from_parsed(parsed_retry):
        if _part_looks_like_catalog_default(part):
            return False
    return True


def _should_adopt_fresh_unified_table_retry(parsed_before: dict, parsed_retry: dict) -> bool:
    """Adopt a fresh unified-prompt re-read after a failed correction pass."""
    if not _is_adoptable_table_extraction(parsed_retry):
        return False
    before_parts = set(_bad_part_numbers_from_parsed(parsed_before))
    retry_parts = set(_bad_part_numbers_from_parsed(parsed_retry))
    if not retry_parts or retry_parts == before_parts:
        return False
    return True


def _should_adopt_table_verification_retry(parsed_before: dict, parsed_retry: dict) -> bool:
    """Adopt table verify retry when it beats a weak or hallucinated pass1."""
    if not _is_adoptable_table_extraction(parsed_retry):
        return False
    if _suspect_catalog_default_extraction(parsed_before):
        return _should_adopt_table_hallucination_retry(parsed_before, parsed_retry)
    before_parts = _bad_part_numbers_from_parsed(parsed_before)
    before_conf = _max_table_confidence(parsed_before)
    retry_conf = _max_table_confidence(parsed_retry)
    if before_conf < RFQ_TABLE_VERIFY_CONFIDENCE:
        if retry_conf > before_conf:
            return True
        if set(_bad_part_numbers_from_parsed(parsed_retry)) != set(before_parts):
            return True
    return False


def _run_pass3d_unified_table_retry(
    parsed_before: dict,
    *,
    message_text: str,
    document_text: str,
    voice_transcript: str,
    image_path: str,
    image_dims: tuple,
    trace_lines: list,
    _call_and_parse,
) -> tuple:
    """Fresh unified-prompt re-read — mirrors manual Copilot UI test (no negative prior context)."""
    print("[COPILOT ANALYZE] pass3c failed — pass3d fresh unified prompt (manual-test parity)")
    unified_prompt = _build_extraction_user_prompt(
        message_text=message_text,
        document_text=document_text,
        voice_transcript=voice_transcript,
        base_prompt=OPENCLAW_UNIFIED_PROMPT,
        image_path=image_path,
        image_dims=image_dims,
    )
    raw3d, parsed3d, err3d, det3d = _call_and_parse(unified_prompt, "pass3d-unified-table-retry")
    if parsed3d is not None and _should_adopt_fresh_unified_table_retry(parsed_before, parsed3d):
        print(
            f"[COPILOT ANALYZE] Adopting pass3d-unified-table-retry: "
            f"{', '.join(_bad_part_numbers_from_parsed(parsed3d))}"
        )
        _append_extraction_trace(
            trace_lines, "pass3d-unified-table-retry", raw3d, parsed3d,
            adopted=True,
        )
        return raw3d, parsed3d
    if parsed3d is not None:
        print(
            "[COPILOT ANALYZE] pass3d-unified-table-retry not adopted — "
            f"parts={_bad_part_numbers_from_parsed(parsed3d)}"
        )
        _append_extraction_trace(trace_lines, "pass3d-unified-table-retry", raw3d, parsed3d, adopted=False)
    else:
        trace_lines.append("  pass3d-unified-table-retry failed")
    return None, None


def _should_adopt_table_hallucination_retry(parsed_before: dict, parsed_retry: dict) -> bool:
    """Adopt table hallucination retry only when part_no changes away from bad guesses."""
    if not _should_adopt_ocr_table_retry(parsed_retry):
        return False
    if _suspect_catalog_default_extraction(parsed_retry):
        return False
    bad_parts = set(_bad_part_numbers_from_parsed(parsed_before))
    retry_parts = _bad_part_numbers_from_parsed(parsed_retry)
    if not retry_parts:
        return False
    if all(part in bad_parts for part in retry_parts):
        return False
    for part in retry_parts:
        if _part_looks_like_catalog_default(part):
            return False
    return True


def _suspect_table_misread(
    parsed: dict,
    image_dims: tuple = None,
    message_text: str = "",
) -> bool:
    """Detect likely rfq_table misclassified as handheld/battery (no OCR — layout + JSON signals)."""
    if not isinstance(parsed, dict) or not image_dims:
        return False
    width, height = image_dims
    if width <= 0 or height <= 0:
        return False
    input_type = str(parsed.get("input_type") or "").strip().lower()
    if input_type == "rfq_table":
        return False
    if width <= height * 1.35:
        return False
    caption_u = str(message_text or "").upper()
    quote_caption = bool(re.search(r"\b(QUOTE|RFQ|QUOTATION|PLS QUOTE)\b", caption_u))
    primary = str(parsed.get("primary_subject") or "").strip().lower()
    items = parsed.get("items") or []
    battery_item = any(
        isinstance(it, dict)
        and (
            str(it.get("product_type") or "").lower() == "battery"
            or _part_looks_like_catalog_default(str(it.get("part_no") or ""))
            and str(it.get("part_no") or "").upper().startswith("ER6C")
        )
        for it in items
    )
    handheld = input_type in ("single_product_photo", "multiple_product_photo", "", "unknown")
    return handheld and (quote_caption or primary == "battery" or battery_item)


def _should_adopt_table_retry(parsed_handheld: dict, parsed_table: dict) -> bool:
    """Prefer rfq_table retry over a misread handheld/battery pass1."""
    if str(parsed_table.get("input_type") or "").strip().lower() != "rfq_table":
        return False
    items = [
        item for item in (parsed_table.get("items") or [])
        if isinstance(item, dict) and str(item.get("part_no") or "").strip()
    ]
    if not items:
        return False
    handheld_part = ""
    handheld_items = parsed_handheld.get("items") or []
    if handheld_items and isinstance(handheld_items[0], dict):
        handheld_part = str(handheld_items[0].get("part_no") or "").strip().upper()
    table_parts = {str(i.get("part_no") or "").strip().upper() for i in items}
    if handheld_part and handheld_part not in table_parts:
        return True
    return len(items) >= 1


def _part_looks_like_catalog_default(part_no: str) -> bool:
    part_u = str(part_no or "").strip().upper()
    if not part_u:
        return False
    return any(part_u.startswith(prefix) for prefix in COMMON_CATALOG_DEFAULT_PREFIXES)


def _suspect_catalog_default_extraction(parsed: dict, message_text: str = "") -> bool:
    """Detect when Copilot likely returned a familiar catalog part instead of reading the image."""
    if not isinstance(parsed, dict):
        return False
    input_type = str(parsed.get("input_type") or "").strip().lower()
    items = [i for i in (parsed.get("items") or []) if isinstance(i, dict)]
    if not items:
        return False
    caption_u = str(message_text or "").upper()
    for item in items:
        part_no = str(item.get("part_no") or "").strip()
        brand = str(item.get("brand") or "").strip().upper()
        if not _part_looks_like_catalog_default(part_no):
            continue
        if input_type == "rfq_table":
            return True
        if brand and brand not in caption_u and input_type in ("single_product_photo", "unknown", ""):
            return True
    return False


def _append_extraction_trace(
    lines: list,
    pass_label: str,
    raw: str,
    parsed: dict,
    adopted: bool = False,
    note: str = "",
) -> None:
    lines.append(f"  [{pass_label}] adopted={adopted}" + (f" | {note}" if note else ""))
    if isinstance(parsed, dict):
        items = parsed.get("items") or []
        parts = [
            f"{i.get('part_no')} x{i.get('qty', 1)}"
            for i in items[:4]
            if isinstance(i, dict)
        ]
        lines.append(
            f"    input_type={parsed.get('input_type')} items={len(items)} "
            f"parts={', '.join(parts) or '-'}"
        )
        if _suspect_catalog_default_extraction(parsed):
            lines.append("    ⚠️ SUSPECTED_CATALOG_DEFAULT (common part prefix without image evidence)")


def _prune_table_retry_items(items: list, misread_part: str = "") -> list:
    """Drop obvious catalog-hallucination rows when table retry returns too many items."""
    if len(items) <= 1:
        return items
    misread_u = str(misread_part or "").strip().upper()
    kept = []
    for item in items:
        if not isinstance(item, dict):
            continue
        part_u = str(item.get("part_no") or "").strip().upper()
        if misread_u and part_u == misread_u:
            continue
        if _part_looks_like_catalog_default(part_u):
            continue
        kept.append(item)
    return kept if kept else items


def _append_copilot_extraction_log(
    pass_label: str,
    message_text: str = "",
    image_path: str = None,
    image_dims: tuple = None,
    raw: str = "",
    parsed: dict = None,
    result: dict = None,
    note: str = "",
    trace_lines: list = None,
) -> None:
    """Single consolidated log for every Copilot extraction pass."""
    try:
        os.makedirs(os.path.dirname(COPILOT_EXTRACTION_LOG), exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        dim_label = (
            f"{image_dims[0]}x{image_dims[1]}"
            if image_dims and image_dims[0] and image_dims[1]
            else "n/a"
        )
        img_bytes = os.path.getsize(image_path) if image_path and os.path.exists(image_path) else 0
        lines = [
            "=" * 88,
            f"{stamp} | {pass_label} | engine={VERSION}",
            f"image: {image_path or 'none'}",
            f"dimensions: {dim_label}",
            f"image_bytes: {img_bytes}",
            f"caption: {(message_text or '')[:240]}",
        ]
        if trace_lines:
            lines.append("TRACE:")
            lines.extend(trace_lines)
        if note:
            lines.append(f"note: {note}")
        lines.append("RAW:")
        lines.append(str(raw or ""))
        if parsed is not None:
            lines.append("PARSED:")
            try:
                lines.append(json.dumps(parsed, indent=2, ensure_ascii=False))
            except (TypeError, ValueError):
                lines.append(str(parsed))
        if result is not None:
            item_summary = ", ".join(
                f"{it.get('part_no')} x{it.get('qty', 1)}"
                for it in (result.get("items") or [])[:6]
            )
            lines.append(
                f"RESULT: ok={result.get('ok')} intent={result.get('intent')} "
                f"input_type={result.get('input_type')} items={len(result.get('items') or [])} "
                f"error={result.get('extraction_error')} | {item_summary}"
            )
        lines.append("")
        with open(COPILOT_EXTRACTION_LOG, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"[COPILOT LOG] → {COPILOT_EXTRACTION_LOG} ({pass_label})")
    except OSError as exc:
        print(f"[COPILOT LOG] Could not write {COPILOT_EXTRACTION_LOG}: {exc}")


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
        "http_status": 200,
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
    single_pass: bool = None,
    minimal_prompt: bool = None,
) -> dict:
    """Copilot-first unified analysis: classify intent + extract parts from text/image/voice/doc together."""
    if os.getenv("OPENCLAW_COPILOT_FIRST", "1").strip().lower() in ("0", "false", "no", "off"):
        return {"attempted": False, "ok": False, "items": []}

    single_pass = _copilot_single_pass_enabled(single_pass)
    minimal_prompt = _copilot_manual_parity_prompt_enabled(minimal_prompt)

    message_text = str(message_text or "").strip()
    document_text = str(document_text or "").strip()
    voice_transcript = str(voice_transcript or "").strip()

    if not any([message_text, image_path, document_text, voice_transcript]):
        return {"attempted": False, "ok": False, "items": []}

    print("[COPILOT ANALYZE] Unified incoming message analysis (text + attachment together)...")
    if single_pass:
        print("[COPILOT ANALYZE] Mode: SINGLE_PASS (pass1 only — no verify/retries)")
    if minimal_prompt:
        print("[COPILOT ANALYZE] Mode: MANUAL_PARITY prompt (unified prompt + caption only, no file path/dims)")
    print(f"[COPILOT LOG] Consolidated log: {COPILOT_EXTRACTION_LOG}")
    image_dims = None
    if image_path:
        if os.path.exists(image_path):
            from whatsapp_attachment_processor import (
                validate_image_file,
                read_image_dimensions,
                is_degraded_wa_capture,
            )

            img_ok, img_reason = validate_image_file(image_path)
            img_size = os.path.getsize(image_path)
            dims = read_image_dimensions(image_path)
            image_dims = dims
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
            degraded, degrade_reason = is_degraded_wa_capture(image_path)
            if degraded:
                print(
                    f"[COPILOT ANALYZE] ⚠️ DEGRADED WA_Image capture ({degrade_reason}) — "
                    "same file sent to Copilot and monitor; vision may hallucinate"
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
        image_path=image_path,
        image_dims=image_dims,
        minimal=minimal_prompt,
    )
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
        if parsed_obj is not None:
            parsed_obj = _normalize_copilot_extraction_json(parsed_obj)
        _append_copilot_extraction_log(
            pass_label=label,
            message_text=message_text,
            image_path=image_path,
            image_dims=image_dims,
            raw=response_raw,
            parsed=parsed_obj,
            note=parse_detail or parse_err or "",
        )
        return response_raw, parsed_obj, parse_err, parse_detail

    raw = ""
    trace_lines = []
    try:
        raw, parsed, parse_error, parse_detail = _call_and_parse(user_text, "pass1")
        _append_extraction_trace(trace_lines, "pass1", raw, parsed, adopted=True, note="initial")

        if parsed is None:
            print(f"[COPILOT ANALYZE] JSON parse failed ({parse_error}): {parse_detail}")
            if single_pass:
                fail = _minimal_copilot_analysis_result(
                    message_text=message_text,
                    analysis_text=raw,
                    raw=raw,
                    parse_warning=f"JSON parse failed: {parse_detail}",
                    http_status=200,
                    extraction_error="PARSER_ERROR",
                )
                _append_copilot_extraction_log(
                    pass_label="final",
                    message_text=message_text,
                    image_path=image_path,
                    image_dims=image_dims,
                    raw=raw,
                    parsed=None,
                    result=fail,
                    note="PARSER_ERROR single_pass",
                )
                return fail
            retry_prompt = (
                _build_extraction_user_prompt(
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    base_prompt=OPENCLAW_JSON_RETRY_PROMPT,
                    image_path=image_path,
                    image_dims=image_dims,
                )
                + f"\n\nYour invalid response was:\n{raw[:1200]}"
            )
            raw, parsed, parse_error, parse_detail = _call_and_parse(retry_prompt, "pass2-json-retry")
            _append_extraction_trace(
                trace_lines, "pass2-json-retry", raw, parsed,
                adopted=bool(parsed), note=parse_detail or parse_error or "",
            )

        if parsed is None:
            print(f"[COPILOT ANALYZE] PARSER_ERROR after retry: {parse_detail}")
            fail = _minimal_copilot_analysis_result(
                message_text=message_text,
                analysis_text=raw,
                raw=raw,
                parse_warning=f"JSON parse failed after retry: {parse_detail}",
                http_status=200,
                extraction_error="PARSER_ERROR",
            )
            _append_copilot_extraction_log(
                pass_label="final",
                message_text=message_text,
                image_path=image_path,
                image_dims=image_dims,
                raw=raw,
                parsed=None,
                result=fail,
                note="PARSER_ERROR",
            )
            return fail

        valid, missing = _validate_copilot_extraction_json(parsed)
        if not valid:
            print(f"[COPILOT ANALYZE] JSON_INVALID — missing/invalid fields: {missing}")
            fail = _minimal_copilot_analysis_result(
                message_text=message_text,
                raw=raw,
                parse_warning=f"JSON validation failed: {', '.join(missing)}",
                http_status=200,
                extraction_error="JSON_INVALID",
            )
            _append_copilot_extraction_log(
                pass_label="final",
                message_text=message_text,
                image_path=image_path,
                image_dims=image_dims,
                raw=raw,
                parsed=parsed,
                result=fail,
                note=f"JSON_INVALID: {missing}",
            )
            return fail

        if single_pass:
            result = _copilot_extraction_result_from_parsed(
                parsed,
                raw,
                message_text=message_text,
                voice_transcript=voice_transcript,
                image_path=image_path,
            )
            _append_copilot_extraction_log(
                pass_label="final",
                message_text=message_text,
                image_path=image_path,
                image_dims=image_dims,
                raw=raw,
                parsed=parsed,
                result=result,
                trace_lines=trace_lines,
                note="single_pass manual parity",
            )
            return result

        if _suspect_table_misread(parsed, image_dims=image_dims, message_text=message_text):
            prev_type = parsed.get("input_type")
            prev_part = ""
            items_in = parsed.get("items") or []
            if items_in and isinstance(items_in[0], dict):
                prev_part = items_in[0].get("part_no", "")
            print(
                f"[COPILOT ANALYZE] Suspect table misread "
                f"(landscape {image_dims}, got {prev_type} / {prev_part}) — rfq_table retry"
            )
            table_prompt = (
                _build_extraction_user_prompt(
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    base_prompt=OPENCLAW_TABLE_RETRY_PROMPT,
                    image_path=image_path,
                    image_dims=image_dims,
                )
                + f"\n\nYour incorrect prior classification: input_type={prev_type}, part_no={prev_part}"
            )
            raw2, parsed2, err2, det2 = _call_and_parse(table_prompt, "pass3-table-retry")
            if parsed2 is not None and _should_adopt_table_retry(parsed, parsed2):
                pruned = _prune_table_retry_items(
                    parsed2.get("items") or [],
                    misread_part=prev_part,
                )
                if len(pruned) != len(parsed2.get("items") or []):
                    print(
                        f"[COPILOT ANALYZE] Pruned table retry items "
                        f"{len(parsed2.get('items') or [])} → {len(pruned)}"
                    )
                    parsed2["items"] = pruned
                print(
                    f"[COPILOT ANALYZE] Adopting pass3-table-retry: "
                    f"rfq_table with {len(parsed2.get('items') or [])} item(s)"
                )
                raw, parsed = raw2, parsed2
                _append_extraction_trace(
                    trace_lines, "pass3-table-retry", raw2, parsed2,
                    adopted=True, note=f"replaced pass1 {prev_part}",
                )
            elif parsed2 is not None:
                print(
                    "[COPILOT ANALYZE] pass3-table-retry not adopted — "
                    f"input_type={parsed2.get('input_type')}, items={len(parsed2.get('items') or [])}"
                )
                _append_extraction_trace(trace_lines, "pass3-table-retry", raw2, parsed2, adopted=False)

        if _should_retry_empty_table_extraction(
            parsed,
            image_dims=image_dims,
            image_path=image_path,
            message_text=message_text,
        ):
            prev_status = parsed.get("status")
            print(
                f"[COPILOT ANALYZE] Empty table / ocr_no_text on readable landscape image "
                f"(status={prev_status}, input_type={parsed.get('input_type')}) — OCR table retry"
            )
            ocr_prompt = (
                _build_extraction_user_prompt(
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    base_prompt=OPENCLAW_OCR_TABLE_RETRY_PROMPT,
                    image_path=image_path,
                    image_dims=image_dims,
                )
                + f"\n\nYour prior empty response: status={prev_status}, "
                f"input_type={parsed.get('input_type')}, items=[]"
            )
            raw3b, parsed3b, err3b, det3b = _call_and_parse(ocr_prompt, "pass3b-ocr-table-retry")
            if parsed3b is not None and _should_adopt_ocr_table_retry(parsed3b):
                print(
                    f"[COPILOT ANALYZE] Adopting pass3b-ocr-table-retry: "
                    f"{len(_parsed_items_with_part_no(parsed3b))} item(s)"
                )
                raw, parsed = raw3b, parsed3b
                _append_extraction_trace(
                    trace_lines, "pass3b-ocr-table-retry", raw3b, parsed3b,
                    adopted=True, note=f"replaced empty status={prev_status}",
                )
            elif parsed3b is not None:
                print(
                    "[COPILOT ANALYZE] pass3b-ocr-table-retry not adopted — "
                    f"status={parsed3b.get('status')}, items={len(parsed3b.get('items') or [])}"
                )
                _append_extraction_trace(trace_lines, "pass3b-ocr-table-retry", raw3b, parsed3b, adopted=False)

        if _should_verify_rfq_table_extraction(
            parsed, image_dims=image_dims, message_text=message_text,
        ):
            prior_parts = _bad_part_numbers_from_parsed(parsed)
            prior_conf = _max_table_confidence(parsed)
            if _suspect_catalog_default_extraction(parsed, message_text):
                verify_reason = f"catalog-default parts {prior_parts}"
            elif prior_conf < RFQ_TABLE_VERIFY_CONFIDENCE:
                verify_reason = f"low confidence ({prior_conf:.0%})"
            else:
                verify_reason = "missing brand on table row"
            print(
                f"[COPILOT ANALYZE] rfq_table verify ({verify_reason}) — pass3c table re-read"
            )
            trace_lines.append(f"  WHY: rfq_table verify — {verify_reason}")
            table_fix_prompt = (
                _build_extraction_user_prompt(
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    base_prompt=OPENCLAW_HALLUCINATION_TABLE_RETRY_PROMPT,
                    image_path=image_path,
                    image_dims=image_dims,
                )
                + f"\n\nYour prior part_no reading (may be wrong): {', '.join(prior_parts)}"
                + f"\nPrior confidence was {prior_conf:.0%}. Re-read the table from scratch."
            )
            raw3c, parsed3c, err3c, det3c = _call_and_parse(
                table_fix_prompt, "pass3c-hallucination-table-retry",
            )
            if parsed3c is not None and _should_adopt_table_verification_retry(parsed, parsed3c):
                print(
                    f"[COPILOT ANALYZE] Adopting pass3c-hallucination-table-retry: "
                    f"{', '.join(_bad_part_numbers_from_parsed(parsed3c))}"
                )
                raw, parsed = raw3c, parsed3c
                _append_extraction_trace(
                    trace_lines, "pass3c-hallucination-table-retry", raw3c, parsed3c,
                    adopted=True, note=f"replaced {prior_parts} ({verify_reason})",
                )
            elif parsed3c is not None:
                print(
                    "[COPILOT ANALYZE] pass3c-hallucination-table-retry not adopted — "
                    f"parts={_bad_part_numbers_from_parsed(parsed3c)}"
                )
                _append_extraction_trace(
                    trace_lines, "pass3c-hallucination-table-retry", raw3c, parsed3c,
                    adopted=False,
                )
                pass1_snapshot = dict(parsed)
                raw3d, parsed3d = _run_pass3d_unified_table_retry(
                    pass1_snapshot,
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    image_path=image_path,
                    image_dims=image_dims,
                    trace_lines=trace_lines,
                    _call_and_parse=_call_and_parse,
                )
                if parsed3d is not None:
                    raw, parsed = raw3d, parsed3d
                else:
                    parsed["items"] = []
                    parsed["status"] = "no_products"
                    trace_lines.append(
                        "  Cleared items — pass3c and pass3d did not produce adoptable parts"
                    )
            else:
                pass1_snapshot = dict(parsed)
                raw3d, parsed3d = _run_pass3d_unified_table_retry(
                    pass1_snapshot,
                    message_text=message_text,
                    document_text=document_text,
                    voice_transcript=voice_transcript,
                    image_path=image_path,
                    image_dims=image_dims,
                    trace_lines=trace_lines,
                    _call_and_parse=_call_and_parse,
                )
                if parsed3d is not None:
                    raw, parsed = raw3d, parsed3d
                else:
                    parsed["items"] = []
                    parsed["status"] = "no_products"
                    trace_lines.append("  pass3c failed — pass3d also failed; cleared items")

        elif _suspect_catalog_default_extraction(parsed, message_text=message_text):
            bad_parts = _bad_part_numbers_from_parsed(parsed)
            print(
                f"[COPILOT ANALYZE] Suspected catalog-default hallucination "
                f"({', '.join(bad_parts)}) — handheld literal retry"
            )
            trace_lines.append(
                f"  WHY: part(s) {bad_parts} match common Copilot catalog defaults "
                f"(E3Z/ER6C/H3JA/3G3MX/3RT prefixes) — likely NOT read from image"
            )
            literal_prompt = _build_extraction_user_prompt(
                message_text=message_text,
                document_text=document_text,
                voice_transcript=voice_transcript,
                base_prompt=OPENCLAW_LITERAL_RETRY_PROMPT,
                image_path=image_path,
                image_dims=image_dims,
            )
            raw4, parsed4, err4, det4 = _call_and_parse(literal_prompt, "pass4-literal-retry")
            if parsed4 is not None and not _suspect_catalog_default_extraction(parsed4, message_text):
                if _should_adopt_table_hallucination_retry(parsed, parsed4):
                    raw, parsed = raw4, parsed4
                    _append_extraction_trace(trace_lines, "pass4-literal-retry", raw4, parsed4, adopted=True)
                else:
                    print("[COPILOT ANALYZE] pass4-literal-retry not adopted — same/suspect parts")
                    parsed["items"] = []
                    parsed["status"] = "no_products"
                    _append_extraction_trace(trace_lines, "pass4-literal-retry", raw4, parsed4, adopted=False)
            elif parsed4 is not None:
                kept = [
                    i for i in (parsed4.get("items") or [])
                    if isinstance(i, dict)
                    and not _part_looks_like_catalog_default(str(i.get("part_no") or ""))
                ]
                if kept and _should_adopt_table_hallucination_retry(parsed, {"items": kept}):
                    parsed4["items"] = kept
                    raw, parsed = raw4, parsed4
                    _append_extraction_trace(
                        trace_lines, "pass4-literal-retry", raw4, parsed4,
                        adopted=True, note="pruned catalog defaults",
                    )
                else:
                    parsed["items"] = []
                    parsed["status"] = "no_products"
                    trace_lines.append(
                        "  pass4-literal-retry still returned catalog defaults — cleared items"
                    )
                    _append_extraction_trace(trace_lines, "pass4-literal-retry", raw4, parsed4, adopted=False)
            else:
                parsed["items"] = []
                parsed["status"] = "no_products"
                trace_lines.append("  pass4-literal-retry failed — cleared items")

        valid, missing = _validate_copilot_extraction_json(parsed)
        if not valid:
            print(f"[COPILOT ANALYZE] JSON_INVALID after retries — missing/invalid fields: {missing}")
            fail = _minimal_copilot_analysis_result(
                message_text=message_text,
                raw=raw,
                parse_warning=f"JSON validation failed: {', '.join(missing)}",
                http_status=200,
                extraction_error="JSON_INVALID",
            )
            _append_copilot_extraction_log(
                pass_label="final",
                message_text=message_text,
                image_path=image_path,
                image_dims=image_dims,
                raw=raw,
                parsed=parsed,
                result=fail,
                note=f"JSON_INVALID: {missing}",
            )
            return fail

        result = _copilot_extraction_result_from_parsed(
            parsed,
            raw,
            message_text=message_text,
            voice_transcript=voice_transcript,
            image_path=image_path,
        )
        _append_copilot_extraction_log(
            pass_label="final",
            message_text=message_text,
            image_path=image_path,
            image_dims=image_dims,
            raw=raw,
            parsed=parsed,
            result=result,
            trace_lines=trace_lines,
        )
        return result
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
            "ABC-12345 Qty:2",
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
            "ABC-12345 Qty:2",
            err or "NO_PRODUCT_FOUND",
        )

    if err == "LOW_CONFIDENCE":
        return (
            "Hi, thank you for your inquiry.\n\n"
            "We received your photo but are not fully confident in the part numbers read. "
            "Please confirm by typing the exact part number(s) and quantity, for example:\n"
            "ABC-12345 Qty:2",
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
        "ABC-12345 Qty:2"
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
