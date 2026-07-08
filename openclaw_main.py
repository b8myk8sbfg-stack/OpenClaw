import subprocess
import sys
import time
import os
import json
import base64
import mimetypes
import signal
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

BASE_DIR = "/Users/evon/OpenClaw"
load_dotenv(os.path.join(BASE_DIR, ".env"))

VERSION = "v1.57-VOICE-RFQ-FIX"

# Part prefixes Copilot often hallucinates without reading the image (post-parse guard only).
COMMON_CATALOG_DEFAULT_PREFIXES = (
    "E3Z", "E2E", "E39", "ER6C", "H3JA", "H3CR", "H3Y", "MY2", "MY4", "3RH", "G3NA",
    "3G3M", "3G3MX", "G3MX", "E3X", "E3S",
    "3RT", "3RV", "3RU", "3RP", "3RA", "6ES", "6EP", "6SL", "1FK", "LC1D", "LC1F",
)

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")
COPILOT_EXTRACTION_LOG = os.path.join(BASE_DIR, "logs", "copilot_extraction.log")

RFQ_TABLE_VERIFY_CONFIDENCE = float(os.getenv("OPENCLAW_RFQ_TABLE_VERIFY_CONFIDENCE", "0.55"))
COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")


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

ITEM_EXTRACTION_FIELDS = (
    "brand", "part_no", "description", "product_type", "qty", "source",
    "confidence", "reason", "technical_specs", "catalog_url", "compatible_accessories",
)


def _score_extraction_json_candidate(parsed: dict) -> int:
    """Prefer full extraction envelopes over nested item fragments."""
    if not isinstance(parsed, dict):
        return -1
    score = 0
    for field in REQUIRED_EXTRACTION_FIELDS:
        if field in parsed:
            score += 20
    items = parsed.get("items")
    if isinstance(items, list):
        score += 15
        score += sum(
            10 for item in items
            if isinstance(item, dict) and str(item.get("part_no") or "").strip()
        )
    if str(parsed.get("part_no") or "").strip() and not (
        isinstance(items, list) and items
    ):
        score += 8
    return score


def _extract_item_dict_from_root(parsed: dict) -> dict:
    part_no = str(parsed.get("part_no") or "").strip()
    if not part_no:
        return {}
    item = {}
    for key in ITEM_EXTRACTION_FIELDS:
        if key in parsed:
            item[key] = parsed[key]
    return item


def _coalesce_root_item_into_items(parsed: dict) -> dict:
    """Recover when the JSON parser returned an item object instead of the full envelope."""
    if not isinstance(parsed, dict):
        return parsed
    items = parsed.get("items")
    if isinstance(items, list) and any(
        isinstance(item, dict) and str(item.get("part_no") or "").strip()
        for item in items
    ):
        return parsed
    root_item = _extract_item_dict_from_root(parsed)
    if not root_item:
        return parsed
    out = dict(parsed)
    out["items"] = [root_item]
    if not str(out.get("status") or "").strip() or out.get("status") == "no_products":
        out["status"] = "success"
    if not str(out.get("intent") or "").strip():
        out["intent"] = "rfq_inquiry"
    if not str(out.get("input_type") or "").strip():
        out["input_type"] = "text_message"
    print(
        f"[COPILOT ANALYZE] Recovered item from JSON root fragment: "
        f"{root_item.get('part_no')} x{root_item.get('qty', 1)}"
    )
    return out


def _fallback_copilot_items_from_text(message_text: str = "", voice_transcript: str = "") -> list:
    """Parse part numbers directly from voice/text when Copilot JSON has zero items."""
    from inquiry_extraction_helper import extract_clean_items_from_text, extract_brand_from_text

    blob = str(voice_transcript or message_text or "").strip()
    if not blob:
        return []
    items_out = []
    seen = set()
    for item in extract_clean_items_from_text(blob):
        part_no = str(item.get("part_no") or "").strip().upper()
        if not part_no:
            continue
        try:
            qty = max(1, int(item.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        key = (part_no, qty)
        if key in seen:
            continue
        seen.add(key)
        items_out.append({
            "part_no": part_no,
            "qty": qty,
            "brand": str(item.get("brand") or "UNKNOWN").strip().upper(),
            "source": "TRANSCRIPT_FALLBACK",
        })

    if items_out:
        return items_out

    voice_part_qty = re.compile(
        r"\b([A-Z0-9][A-Z0-9\-]{4,40})\??\s+(\d{1,4})\s*"
        r"(?:PCS|PC|PIECES|PIECE|UNIT|UNITS|EA)\b",
        re.I,
    )
    for part_no, qty in voice_part_qty.findall(blob.upper()):
        part_no = part_no.strip("-").upper()
        qty = max(1, int(qty))
        key = (part_no, qty)
        if key in seen:
            continue
        seen.add(key)
        items_out.append({
            "part_no": part_no,
            "qty": qty,
            "brand": extract_brand_from_text(blob),
            "source": "TRANSCRIPT_FALLBACK",
        })
    return items_out


def _maybe_apply_transcript_fallback(
    parsed: dict,
    message_text: str = "",
    voice_transcript: str = "",
) -> dict:
    """Fill items[] from transcript when Copilot JSON parsed to zero items."""
    if not isinstance(parsed, dict):
        return parsed
    if _parsed_items_with_part_no(parsed):
        return parsed
    fallback_items = _fallback_copilot_items_from_text(message_text, voice_transcript)
    if not fallback_items:
        return parsed
    print(
        f"[COPILOT ANALYZE] Transcript fallback extracted "
        f"{len(fallback_items)} item(s) from voice/text"
    )
    out = dict(parsed)
    out["items"] = [
        {
            "brand": item.get("brand") or "UNKNOWN",
            "part_no": item["part_no"],
            "description": "",
            "product_type": "",
            "qty": item["qty"],
            "source": "voice transcript",
            "confidence": 0.9,
            "reason": "Parsed from voice/text transcript after JSON extraction gap",
        }
        for item in fallback_items
    ]
    out["status"] = "success"
    out["intent"] = str(out.get("intent") or "rfq_inquiry")
    out["input_type"] = str(out.get("input_type") or "text_message")
    return out


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

STEP 5 — Technical enrichment (when status=success and items[] is not empty):
After part_no is transcribed from the image, add product expertise for customer quotation replies.
This step uses industrial automation knowledge — it does NOT change part_no, qty, or brand transcription.

Fill technical_summary (plain text, WhatsApp-friendly) covering the primary quoted item(s):
- One-sentence product overview (what it is and typical use)
- Specifications as "Label: value" lines (model, voltage, contact rating, mounting, enclosure, etc.)
- Part-number suffix notes (e.g. what "N", "GS-R", or series letters mean when applicable)
- Pin / terminal configuration if relevant (relay, sensor, connector products)
- Compatible sockets, bases, or accessories commonly ordered with this part
- Typical applications (short bullet list)
- Official catalog or datasheet URL on its own line: Catalog: https://... (manufacturer site only)
- catalog_url MUST be from the SAME manufacturer as the part (e.g. Siemens 3VL → siemens.com only; never omron.com for non-Omron parts)
- technical_summary MUST describe the exact part_no values in items[] — never internal stock IDs or substitute part numbers
- Match product category to part family (3VL/5SY = circuit breaker, MY2/E3Z = relay/sensor — do not describe a breaker as a relay)

Per item, also fill when known:
- technical_specs: array of "Label: value" strings (key electrical/mechanical specs)
- catalog_url: full https:// manufacturer catalog or datasheet link
- compatible_accessories: array of related part numbers (e.g. socket models)

Keep technical_summary under 400 words. Plain text only — no markdown, no ** bold, no code fences.
If you do not know a spec or URL with confidence, omit it — never invent datasheet links.

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
      "reason": "",
      "technical_specs": ["Model: ", "Coil voltage: "],
      "catalog_url": "",
      "compatible_accessories": []
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
- Do NOT return parts from memory or training — only characters visible in the image
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
- Do NOT return parts from model memory unless those exact characters are visible in the image
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
  "items": [{"brand":"","part_no":"","description":"","product_type":"","qty":1,"source":"","confidence":0.0,"reason":"","technical_specs":[],"catalog_url":"","compatible_accessories":[]}],
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
    if analysis.get("analysis_backend") == "openai_vision":
        # OpenAI vision misconfig (401 bad key) is not a Copilot proxy outage.
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


def is_openai_api_key_failure(analysis: dict) -> bool:
    """True when OpenAI vision failed due to missing/invalid OPENAI_API_KEY."""
    if not isinstance(analysis, dict):
        return False
    if analysis.get("analysis_backend") != "openai_vision":
        return False
    try:
        return int(analysis.get("http_status") or 0) == 401
    except (TypeError, ValueError):
        return False


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

    best_parsed = None
    best_score = -1
    for candidate in candidates:
        parsed = _try_parse_json_candidate(candidate)
        if parsed is None:
            continue
        score = _score_extraction_json_candidate(parsed)
        if score > best_score:
            best_score = score
            best_parsed = parsed
    if best_parsed is not None:
        return best_parsed, None, ""

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
    return _coalesce_root_item_into_items(out)


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


def _image_analysis_backend(override: str = None) -> str:
    """Which backend handles image attachments: copilot (local proxy) or openai."""
    if override:
        return str(override).strip().lower()
    raw = os.getenv("OPENCLAW_IMAGE_BACKEND", "copilot").strip().lower()
    if raw in ("openai", "gpt", "gpt-4o", "gpt-4.1", "gpt-4.1-mini"):
        return "openai"
    return "copilot"


def _resolve_openai_api_key() -> str:
    """Return a real OpenAI API key from env, or empty if missing/placeholder."""
    key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return ""
    if key in ("local-bypass", "local-copilot-proxy", "unused", "sk-your-key-here"):
        print(f"[OPENAI ANALYZE] OPENAI_API_KEY is placeholder ({key!r}) — set real sk-... key in .env")
        return ""
    if not key.startswith("sk-"):
        print("[OPENAI ANALYZE] OPENAI_API_KEY does not look valid (must start with sk-)")
        return ""
    return key


def _use_openai_for_image(image_path: str = None, backend: str = None) -> bool:
    if not image_path or not os.path.exists(image_path):
        return False
    if _image_analysis_backend(backend) != "openai":
        return False
    if not _resolve_openai_api_key():
        print("[OPENAI ANALYZE] OPENAI_API_KEY not set — cannot use OpenAI image backend")
        return False
    return True


def _image_compare_copilot_enabled(override: bool = None) -> bool:
    """When OpenAI is primary for images, also run Copilot in parallel for diagnostics."""
    if override is not None:
        return bool(override)
    return os.getenv("OPENCLAW_IMAGE_COMPARE_COPILOT", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _normalize_part_for_comparison(part_no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part_no or "").strip().upper())


def _extraction_item_tuples(items: list) -> list:
    """Stable (part, qty) pairs for backend comparison."""
    tuples = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        part = _normalize_part_for_comparison(item.get("part_no"))
        if not part:
            continue
        try:
            qty = int(item.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        tuples.append((part, max(qty, 1)))
    tuples.sort()
    return tuples


def _format_items_for_comparison(items: list, source: str = "") -> str:
    tuples = _extraction_item_tuples(items)
    if not tuples:
        return "none"
    label = ", ".join(f"{part} x{qty}" for part, qty in tuples)
    if source:
        return f"{label} ({source})"
    return label


def _compare_image_extractions(primary: dict, secondary: dict) -> dict:
    """Compare OpenAI (primary) vs Copilot (secondary) extraction results."""
    primary_items = primary.get("items") or []
    secondary_items = secondary.get("items") or []
    primary_tuples = _extraction_item_tuples(primary_items)
    secondary_tuples = _extraction_item_tuples(secondary_items)

    if primary_tuples and secondary_tuples:
        verdict = "match" if primary_tuples == secondary_tuples else "mismatch"
    elif primary_tuples and not secondary_tuples:
        verdict = "openai_only"
    elif secondary_tuples and not primary_tuples:
        verdict = "copilot_only"
    else:
        verdict = "both_empty"

    openai_label = _format_items_for_comparison(primary_items, "OPENAI_VISION")
    copilot_label = _format_items_for_comparison(secondary_items, "COPILOT_API")
    summary = f"OpenAI: {openai_label} | Copilot: {copilot_label} — {verdict.upper()}"

    return {
        "enabled": True,
        "verdict": verdict,
        "summary": summary,
        "openai": {
            "ok": bool(primary.get("ok")),
            "items": primary_items,
            "label": openai_label,
            "backend": primary.get("analysis_backend") or "openai_vision",
            "error": primary.get("extraction_error") or primary.get("error"),
        },
        "copilot": {
            "ok": bool(secondary.get("ok")),
            "items": secondary_items,
            "label": copilot_label,
            "backend": secondary.get("analysis_backend") or "copilot",
            "error": secondary.get("extraction_error") or secondary.get("error"),
        },
    }


def _append_backend_comparison_log(
    comparison: dict,
    message_text: str = "",
    image_path: str = None,
    image_dims: tuple = None,
) -> None:
    if not comparison or not comparison.get("enabled"):
        return
    note = comparison.get("summary") or ""
    _append_copilot_extraction_log(
        pass_label="backend-compare",
        message_text=message_text,
        image_path=image_path,
        image_dims=image_dims,
        raw=note,
        parsed={
            "verdict": comparison.get("verdict"),
            "openai": comparison.get("openai", {}).get("label"),
            "copilot": comparison.get("copilot", {}).get("label"),
        },
        note=note,
    )


def _run_openai_and_copilot_image_compare(
    message_text: str = "",
    image_path: str = None,
    document_text: str = None,
    voice_transcript: str = None,
    minimal_prompt: bool = False,
) -> dict:
    """Run OpenAI vision (primary) and Copilot API (shadow) in parallel; return OpenAI result."""
    from whatsapp_attachment_processor import read_image_dimensions

    image_dims = read_image_dimensions(image_path) if image_path else None
    print(
        "[IMAGE COMPARE] Running OpenAI vision + Copilot API in parallel "
        f"(OPENCLAW_IMAGE_COMPARE_COPILOT=1, engine={VERSION})"
    )

    openai_result = None
    copilot_result = None

    def _openai_job():
        return analyze_incoming_inquiry_with_openai_vision(
            message_text=message_text,
            image_path=image_path,
            document_text=document_text,
            voice_transcript=voice_transcript,
            minimal_prompt=minimal_prompt,
        )

    def _copilot_job():
        return analyze_incoming_inquiry_with_copilot(
            message_text=message_text,
            image_path=image_path,
            document_text=document_text,
            voice_transcript=voice_transcript,
            single_pass=True,
            minimal_prompt=minimal_prompt,
            image_backend="copilot",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_openai_job): "openai",
            pool.submit(_copilot_job): "copilot",
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"[IMAGE COMPARE] {label} failed: {exc}")
                result = {
                    "attempted": True,
                    "ok": False,
                    "items": [],
                    "error": str(exc),
                    "analysis_backend": label,
                }
            if label == "openai":
                openai_result = result
            else:
                copilot_result = result

    openai_result = openai_result or {
        "attempted": True,
        "ok": False,
        "items": [],
        "analysis_backend": "openai_vision",
        "error": "OpenAI compare job did not return",
    }
    copilot_result = copilot_result or {
        "attempted": True,
        "ok": False,
        "items": [],
        "analysis_backend": "copilot",
        "error": "Copilot compare job did not return",
    }

    comparison = _compare_image_extractions(openai_result, copilot_result)
    openai_result["backend_comparison"] = comparison
    openai_result["copilot_shadow"] = copilot_result

    verdict = comparison.get("verdict", "unknown")
    print(f"[IMAGE COMPARE] {comparison.get('summary')}")
    if verdict == "match":
        print("[IMAGE COMPARE] ✅ Backends agree — OpenAI result used for production")
    elif verdict == "mismatch":
        print(
            "[IMAGE COMPARE] ⚠️ Backends disagree — OpenAI result used for production; "
            "see copilot_extraction.log backend-compare"
        )
    else:
        print(f"[IMAGE COMPARE] Verdict={verdict} — OpenAI result used for production")

    _append_backend_comparison_log(
        comparison,
        message_text=message_text,
        image_path=image_path,
        image_dims=image_dims,
    )
    return openai_result


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


def _degraded_extraction_discard_reason(parsed: dict, image_path: str = None) -> str:
    """
    When WA_Image is degraded (e.g. 688x309), Copilot API often cannot read text.
    Short vision probes return UNREADABLE; the long JSON prompt then hallucinates E3Z/3RT.
    Discard those guesses — return ocr_no_text instead.
    """
    if not image_path or not isinstance(parsed, dict):
        return ""
    from whatsapp_attachment_processor import is_degraded_wa_capture

    degraded, degrade_reason = is_degraded_wa_capture(image_path)
    if not degraded or not _parsed_items_with_part_no(parsed):
        return ""
    conf = _max_table_confidence(parsed)
    parts = _bad_part_numbers_from_parsed(parsed)
    if conf < 0.55:
        return (
            f"degraded capture ({degrade_reason}) — API vision likely unreadable "
            f"(confidence {conf:.0%}), discarding guessed parts {parts}"
        )
    if any(_part_looks_like_catalog_default(p) for p in parts):
        return f"catalog guess on degraded capture ({degrade_reason}): {parts}"
    return ""


def _apply_degraded_extraction_guard(parsed: dict, image_path: str = None) -> dict:
    """Clear hallucinated items when degraded image + low confidence / catalog guess."""
    reason = _degraded_extraction_discard_reason(parsed, image_path=image_path)
    if not reason:
        return parsed
    print(f"[COPILOT ANALYZE] {reason}")
    out = dict(parsed)
    out["items"] = []
    out["status"] = "ocr_no_text"
    return out


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
        str(parsed.get("technical_summary") or "").strip(),
        preserve_urls=True,
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


def _copilot_fresh_chat(client, messages, timeout: float = 60.0, max_tokens: int = 4096):
    """Every Copilot call starts a new upstream conversation — no thread history."""
    return client.chat.completions.create(
        model=COPILOT_MODEL,
        messages=messages,
        extra_body={"conversation_id": None},
        timeout=timeout,
        max_tokens=max_tokens,
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
        image_part = {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{image_b64}",
                "detail": "high",
            },
        }
        # Match Copilot native protocol: image content before text prompt.
        return [image_part, {"type": "text", "text": user_text}]
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
        specs = item.get("technical_specs")
        if isinstance(specs, list):
            cleaned_specs = [str(s).strip() for s in specs if str(s).strip()]
            if cleaned_specs:
                item_out["technical_specs"] = cleaned_specs
        catalog_url = str(item.get("catalog_url") or "").strip()
        if catalog_url.startswith("http"):
            item_out["catalog_url"] = catalog_url
        accessories = item.get("compatible_accessories")
        if isinstance(accessories, list):
            cleaned_acc = [str(a).strip() for a in accessories if str(a).strip()]
            if cleaned_acc:
                item_out["compatible_accessories"] = cleaned_acc
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


def analyze_incoming_inquiry_with_openai_vision(
    message_text: str = "",
    image_path: str = None,
    document_text: str = None,
    voice_transcript: str = None,
    minimal_prompt: bool = False,
) -> dict:
    """Extract RFQ items from an image using OpenAI vision (OPENAI_API_KEY), not Copilot proxy."""
    message_text = str(message_text or "").strip()
    document_text = str(document_text or "").strip()
    voice_transcript = str(voice_transcript or "").strip()

    if not image_path or not os.path.exists(image_path):
        return _minimal_copilot_analysis_result(
            message_text=message_text,
            parse_warning="image file not found for OpenAI vision",
            http_status=200,
        )

    from whatsapp_attachment_processor import read_image_dimensions, validate_image_file

    img_ok, img_reason = validate_image_file(image_path)
    dims = read_image_dimensions(image_path)
    image_dims = dims
    dim_label = f"{dims[0]}x{dims[1]}" if dims else "unknown"
    img_size = os.path.getsize(image_path)

    print("[OPENAI ANALYZE] Image inquiry via OpenAI vision (text/pdf/voice still use Copilot)...")
    print(f"[OPENAI ANALYZE] Model: {OPENAI_VISION_MODEL}")
    print(
        f"[OPENAI ANALYZE] Image: {img_size} bytes, {dim_label} "
        f"({'valid' if img_ok else 'INVALID: ' + img_reason})"
    )
    if not img_ok:
        return _minimal_copilot_analysis_result(
            message_text=message_text,
            parse_warning=f"invalid image: {img_reason}",
            http_status=200,
        )

    api_key = _resolve_openai_api_key()
    if not api_key:
        return {
            "attempted": True,
            "ok": False,
            "error": "OPENAI_API_KEY missing or invalid in .env",
            "http_status": 401,
            "items": [],
            "extraction_error": "OPENAI_API_KEY_INVALID",
            "analysis_backend": "openai_vision",
        }

    client = OpenAI(api_key=api_key, timeout=120.0, max_retries=1)
    user_text = _build_extraction_user_prompt(
        message_text=message_text,
        document_text=document_text,
        voice_transcript=voice_transcript,
        image_path=image_path,
        image_dims=image_dims,
        minimal=minimal_prompt,
    )
    content = _copilot_user_content_with_image(user_text, image_path)

    try:
        response = client.chat.completions.create(
            model=OPENAI_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = (response.choices[0].message.content or "").strip()
        print(f"[OPENAI ANALYZE RAW] {raw[:500]}")
        parsed, parse_error, parse_detail = parse_copilot_json_response(raw)
        if parsed is not None:
            parsed = _normalize_copilot_extraction_json(parsed)
        _append_copilot_extraction_log(
            pass_label="openai-vision",
            message_text=message_text,
            image_path=image_path,
            image_dims=image_dims,
            raw=raw,
            parsed=parsed,
            note=parse_detail or parse_error or "",
        )
        if parsed is None:
            fail = _minimal_copilot_analysis_result(
                message_text=message_text,
                raw=raw,
                parse_warning=f"JSON parse failed: {parse_detail}",
                http_status=200,
                extraction_error="PARSER_ERROR",
            )
            fail["analysis_backend"] = "openai_vision"
            return fail
        valid, missing = _validate_copilot_extraction_json(parsed)
        if not valid:
            fail = _minimal_copilot_analysis_result(
                message_text=message_text,
                raw=raw,
                parse_warning=f"JSON validation failed: {', '.join(missing)}",
                http_status=200,
                extraction_error="JSON_INVALID",
            )
            fail["analysis_backend"] = "openai_vision"
            return fail
        result = _copilot_extraction_result_from_parsed(
            parsed,
            raw,
            message_text=message_text,
            voice_transcript=voice_transcript,
            image_path=image_path,
        )
        result["analysis_backend"] = "openai_vision"
        for item in result.get("items") or []:
            item["source"] = "OPENAI_VISION"
        _append_copilot_extraction_log(
            pass_label="openai-final",
            message_text=message_text,
            image_path=image_path,
            image_dims=image_dims,
            raw=raw,
            parsed=parsed,
            result=result,
            note="openai_vision",
        )
        return result
    except (APIConnectionError, APITimeoutError) as exc:
        print(f"[OPENAI ANALYZE] connection/timeout: {exc}")
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": None,
            "items": [],
            "analysis_backend": "openai_vision",
        }
    except APIStatusError as exc:
        status = getattr(exc, "status_code", None)
        print(f"[OPENAI ANALYZE] API error {status}: {exc}")
        err_code = "OPENAI_API_KEY_INVALID" if status == 401 else "OPENAI_API_ERROR"
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": status,
            "items": [],
            "extraction_error": err_code,
            "analysis_backend": "openai_vision",
        }
    except Exception as exc:
        print(f"[OPENAI ANALYZE] failed: {exc}")
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "http_status": None,
            "items": [],
            "analysis_backend": "openai_vision",
        }


def analyze_incoming_inquiry_with_copilot(
    message_text: str = "",
    image_path: str = None,
    document_text: str = None,
    voice_transcript: str = None,
    single_pass: bool = None,
    minimal_prompt: bool = None,
    image_backend: str = None,
) -> dict:
    """Copilot-first unified analysis: classify intent + extract parts from text/image/voice/doc together."""
    if os.getenv("OPENCLAW_COPILOT_FIRST", "1").strip().lower() in ("0", "false", "no", "off"):
        return {"attempted": False, "ok": False, "items": []}

    message_text = str(message_text or "").strip()
    document_text = str(document_text or "").strip()
    voice_transcript = str(voice_transcript or "").strip()

    if not any([message_text, image_path, document_text, voice_transcript]):
        return {"attempted": False, "ok": False, "items": []}

    # Images → OpenAI vision when OPENCLAW_IMAGE_BACKEND=openai (saves Copilot tokens; better vision).
    if _use_openai_for_image(image_path, backend=image_backend):
        minimal = _copilot_manual_parity_prompt_enabled(minimal_prompt)
        if _image_compare_copilot_enabled():
            return _run_openai_and_copilot_image_compare(
                message_text=message_text,
                image_path=image_path,
                document_text=document_text,
                voice_transcript=voice_transcript,
                minimal_prompt=minimal,
            )
        return analyze_incoming_inquiry_with_openai_vision(
            message_text=message_text,
            image_path=image_path,
            document_text=document_text,
            voice_transcript=voice_transcript,
            minimal_prompt=minimal,
        )

    single_pass = _copilot_single_pass_enabled(single_pass)
    minimal_prompt = _copilot_manual_parity_prompt_enabled(minimal_prompt)

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

        parsed = _apply_degraded_extraction_guard(parsed, image_path=image_path)
        parsed = _maybe_apply_transcript_fallback(
            parsed,
            message_text=message_text,
            voice_transcript=voice_transcript,
        )

        if single_pass:
            catalog_parts = _bad_part_numbers_from_parsed(parsed)
            if _suspect_catalog_default_extraction(parsed, message_text) or any(
                _part_looks_like_catalog_default(p) for p in catalog_parts
            ):
                print(
                    f"[COPILOT ANALYZE] SINGLE_PASS rejected catalog hallucination "
                    f"({', '.join(catalog_parts)}) — Copilot likely did not read the image"
                )
                trace_lines.append(
                    f"  REJECTED catalog guess: {catalog_parts} (confidence "
                    f"{_max_table_confidence(parsed):.0%})"
                )
                parsed = dict(parsed)
                parsed["items"] = []
                parsed["status"] = "no_products"
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

        parsed = _maybe_apply_transcript_fallback(
            parsed,
            message_text=message_text,
            voice_transcript=voice_transcript,
        )

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


PART_PREFIX_BRANDS = (
    (("3VL", "3VA", "5SY", "5SL", "5SP", "6ES", "6EP", "6SL", "3RT", "3RV"), "SIEMENS"),
    (("E3Z", "E2E", "E39", "MY2", "MY4", "H3CR", "H3Y", "G2R", "G7T"), "OMRON"),
    (("150-C", "1492-", "1734-", "1756-", "1769-"), "ALLEN BRADLEY"),
    (("LC1D", "LC1F", "GV2", "GV3"), "SCHNEIDER"),
    (("E3S", "E3X"), "OMRON"),
)

BRAND_CATALOG_DOMAINS = {
    "SIEMENS": ("siemens.com", "mall.industry.siemens.com"),
    "OMRON": ("omron.com", "ia.omron.com", "industrial.omron.com"),
    "ALLEN BRADLEY": ("rockwellautomation.com", "ab.com"),
    "ROCKWELL": ("rockwellautomation.com", "ab.com"),
    "SCHNEIDER": ("se.com", "schneider-electric.com"),
    "ABB": ("abb.com", "new.abb.com"),
    "SMC": ("smcworld.com", "smc.eu"),
    "FESTO": ("festo.com",),
    "MITSUBISHI": ("mitsubishielectric.com",),
}


def _infer_brand_from_part_no(part_no: str) -> str:
    upper = str(part_no or "").upper()
    for prefixes, brand in PART_PREFIX_BRANDS:
        for prefix in prefixes:
            if upper.startswith(prefix):
                return brand
    return ""


def _looks_like_obm_stock_id(part_no: str) -> bool:
    """True when value is a numeric warehouse/OBM ID, not a catalog part number."""
    text = str(part_no or "").strip()
    return bool(text) and text.isdigit() and len(text) <= 8


def _extract_catalog_part_from_text(text: str) -> str:
    """Best-effort catalog part number from a description line."""
    blob = str(text or "").upper()
    patterns = (
        r"\b([A-Z]{1,4}\d{1,4}[A-Z0-9]*(?:-[A-Z0-9]+){1,4})\b",
        r"\b(\d{1,3}[A-Z]{1,3}\d{2,}[A-Z0-9\-]+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, blob)
        if match:
            candidate = match.group(1).strip("-")
            if len(_normalize_part_key(candidate)) >= 6:
                return candidate
    return ""


def _research_part_no_from_row(row: dict, copilot_items: list = None) -> tuple:
    """Resolve the customer catalog part number for product research (not OBM stock ID)."""
    customer = str(row.get("customer_part") or "").strip().upper()
    desc = str(row.get("desc") or "").strip()
    brand = str(row.get("brand") or "UNKNOWN").strip().upper()

    if customer and not _looks_like_obm_stock_id(customer):
        return customer, brand if brand != "UNKNOWN" else _infer_brand_from_part_no(customer) or brand

    for item in copilot_items or []:
        if not isinstance(item, dict):
            continue
        part_no = str(item.get("part_no") or "").strip().upper()
        if part_no and not _looks_like_obm_stock_id(part_no):
            item_brand = str(item.get("brand") or brand or "UNKNOWN").strip().upper()
            inferred = _infer_brand_from_part_no(part_no)
            return part_no, item_brand if item_brand != "UNKNOWN" else (inferred or brand)

    from_desc = _extract_catalog_part_from_text(desc)
    if from_desc:
        inferred = _infer_brand_from_part_no(from_desc)
        return from_desc.upper(), inferred or brand

    if customer:
        return customer, brand
    pid = str(row.get("pid") or "").strip().upper()
    return pid, brand


def _brand_catalog_domains(brand: str) -> tuple:
    brand_u = str(brand or "").upper().strip()
    if brand_u in BRAND_CATALOG_DOMAINS:
        return BRAND_CATALOG_DOMAINS[brand_u]
    for key, domains in BRAND_CATALOG_DOMAINS.items():
        if key in brand_u or brand_u in key:
            return domains
    inferred = _infer_brand_from_part_no(brand_u)
    if inferred:
        return BRAND_CATALOG_DOMAINS.get(inferred, ())
    return ()


def _catalog_url_matches_brand(url: str, brand: str) -> bool:
    url_l = str(url or "").lower()
    if not url_l.startswith("http"):
        return False
    domains = _brand_catalog_domains(brand)
    if not domains:
        inferred = _infer_brand_from_part_no(brand)
        domains = _brand_catalog_domains(inferred) if inferred else ()
    if not domains:
        return True
    return any(domain in url_l for domain in domains)


def _research_mentions_part(text: str, part_no: str) -> bool:
    norm_part = _normalize_part_key(part_no)
    norm_text = _normalize_part_key(text)
    if not norm_part or not norm_text:
        return False
    if norm_part in norm_text:
        return True
    check_len = min(8, len(norm_part))
    if check_len >= 5 and norm_part[:check_len] in norm_text:
        return True
    return False


def _sanitize_product_research_text(text: str, part_no: str, brand: str = "UNKNOWN") -> str:
    """Drop hallucinated catalog URLs and research that does not match the quoted part."""
    raw = str(text or "").strip()
    if not raw:
        return ""

    brand_hint = str(brand or "UNKNOWN").strip().upper()
    if brand_hint in ("UNKNOWN", ""):
        brand_hint = _infer_brand_from_part_no(part_no) or brand_hint

    cleaned_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("catalog:") or lower.startswith("datasheet:") or "http://" in lower or "https://" in lower:
            url_match = re.search(r"https?://\S+", stripped)
            if url_match:
                url = url_match.group(0).rstrip(".,)")
                if brand_hint not in ("UNKNOWN", "") and not _catalog_url_matches_brand(url, brand_hint):
                    print(
                        f"[PRODUCT RESEARCH] Dropped off-brand catalog URL for {part_no} "
                        f"({brand_hint}): {url[:80]}"
                    )
                    continue
                cleaned_lines.append(f"Catalog: {url}")
                continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    if not _research_mentions_part(cleaned, part_no):
        if _looks_like_obm_stock_id(part_no):
            print(f"[PRODUCT RESEARCH] Rejected research keyed on OBM stock ID {part_no}")
            return ""
        print(f"[PRODUCT RESEARCH] Rejected research — does not mention {part_no}")
        return ""

    return cleaned


def _product_research_prompt(part_no: str, brand: str = "UNKNOWN") -> str:
    brand_line = f" by {brand}" if brand and brand != "UNKNOWN" else ""
    inferred = _infer_brand_from_part_no(part_no)
    brand_note = ""
    if inferred and inferred != brand:
        brand_note = f"\nManufacturer family: {inferred} (from part number prefix)."
    elif inferred:
        brand_note = f"\nManufacturer: {inferred}."
    return (
        f"Research the EXACT industrial automation catalog part number: {part_no}{brand_line}.\n"
        f"Do NOT substitute a different part number, stock ID, or generic relay/sensor example.{brand_note}\n"
        "Write a technical summary for a sales quotation reply (plain text, WhatsApp-friendly).\n\n"
        "Include ONLY when accurate for THIS exact part number:\n"
        "- One-sentence product overview (correct product category — breaker vs relay vs sensor)\n"
        "- Specifications as 'Label: value' lines\n"
        "- Part-number suffix notes when applicable\n"
        "- Pin / terminal configuration only if relevant to this product type\n"
        "- Compatible accessories commonly ordered with this exact part\n"
        "- Typical applications (short bullet list)\n"
        "- Official manufacturer catalog URL on its own line: Catalog: https://...\n"
        "  (must be the correct manufacturer domain for this brand — never cross-brand links)\n\n"
        f"The summary MUST mention {part_no} by name.\n"
        "Keep under 280 words. Plain text only — no markdown, no ** bold.\n"
        "If unsure of a spec or URL, omit it — never invent links or substitute another part."
    )


def research_part_with_openai(part_no: str, brand: str = "UNKNOWN") -> str:
    """Look up a part with OpenAI and return a technical summary for customer replies."""
    part_no = str(part_no or "").strip().upper()
    brand = str(brand or "UNKNOWN").strip().upper()
    if not part_no:
        return ""

    api_key = _resolve_openai_api_key()
    if not api_key:
        return ""

    print(f"[OPENAI RESEARCH] Looking up {part_no} ({brand})...")
    client = OpenAI(api_key=api_key, timeout=60.0, max_retries=1)
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_RESEARCH_MODEL", OPENAI_VISION_MODEL),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior industrial automation technical sales engineer at Robomatics (Malaysia). "
                        "Give accurate, practical product summaries for quotation replies. "
                        "Plain text only — no markdown. Include official catalog URLs when known."
                    ),
                },
                {"role": "user", "content": _product_research_prompt(part_no, brand)},
            ],
            temperature=0.2,
            max_tokens=900,
        )
        text = (response.choices[0].message.content or "").strip()
        print(f"[OPENAI RESEARCH] {len(text)} chars for {part_no}")
        return _sanitize_product_research_text(
            _sanitize_whatsapp_reply(text, preserve_urls=True),
            part_no,
            brand,
        )
    except Exception as exc:
        print(f"[WARN] OpenAI research failed for {part_no}: {exc}")
        return ""


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
    prompt = _product_research_prompt(part_no, brand)
    try:
        response = _copilot_fresh_chat(
            client,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a senior industrial automation technical sales engineer at Robomatics (Malaysia). "
                        "Give accurate, practical product summaries for quotation replies. "
                        "Plain text only — no markdown. Include official catalog URLs when known."
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
        return _sanitize_product_research_text(
            _sanitize_whatsapp_reply(text, preserve_urls=True),
            part_no,
            brand,
        )
    except Exception as exc:
        print(f"[WARN] Copilot research failed for {part_no}: {exc}")
        return ""


def research_part_for_quotation(part_no: str, brand: str = "UNKNOWN") -> str:
    """Research a part using the configured product-research backend."""
    backend = os.getenv("OPENCLAW_RESEARCH_BACKEND", "").strip().lower()
    if not backend:
        backend = "openai" if _resolve_openai_api_key() else "copilot"
    if backend in ("openai", "gpt", "gpt-4o"):
        text = research_part_with_openai(part_no, brand)
        if text:
            return text
    return research_part_with_copilot(part_no, brand)


def build_ai_research_summary(formatted_rows, copilot_items=None):
    """Build product research notes for the unique customer parts in a quote."""
    sections = []
    seen = set()
    for row in formatted_rows or []:
        part_no, brand = _research_part_no_from_row(row, copilot_items=copilot_items)
        if not part_no or part_no in seen:
            continue
        if _looks_like_obm_stock_id(part_no):
            print(f"[PRODUCT RESEARCH] Skipping OBM stock ID {part_no} — no catalog part resolved")
            continue
        seen.add(part_no)
        notes = research_part_for_quotation(part_no, brand)
        if notes:
            sections.append(notes)
    return "\n\n".join(sections)


def _sanitize_item_catalog_url(url: str, part_no: str, brand: str) -> str:
    url = str(url or "").strip()
    if not url.startswith("http"):
        return ""
    brand_hint = str(brand or "UNKNOWN").strip().upper()
    if brand_hint in ("UNKNOWN", ""):
        brand_hint = _infer_brand_from_part_no(part_no) or brand_hint
    if brand_hint not in ("UNKNOWN", "") and not _catalog_url_matches_brand(url, brand_hint):
        print(f"[PRODUCT RESEARCH] Dropped off-brand item catalog_url for {part_no}: {url[:80]}")
        return ""
    return url


def build_product_details_for_reply(
    formatted_rows=None,
    copilot_items=None,
    technical_summary: str = "",
) -> str:
    """Combine extraction technical_summary, per-item specs, and fallback research."""
    sections = []
    summary = str(technical_summary or "").strip()
    if summary:
        primary_part = ""
        primary_brand = "UNKNOWN"
        if copilot_items:
            primary_part = str(copilot_items[0].get("part_no") or "").strip().upper()
            primary_brand = str(copilot_items[0].get("brand") or "UNKNOWN").strip().upper()
        elif formatted_rows:
            primary_part, primary_brand = _research_part_no_from_row(
                formatted_rows[0], copilot_items=copilot_items
            )
        if primary_part:
            summary = _sanitize_product_research_text(summary, primary_part, primary_brand)
        if summary:
            sections.append(summary)

    seen_parts = set()
    for item in copilot_items or []:
        if not isinstance(item, dict):
            continue
        part_no = str(item.get("part_no") or "").strip().upper()
        if not part_no or part_no in seen_parts:
            continue
        seen_parts.add(part_no)
        brand = str(item.get("brand") or "UNKNOWN").strip().upper()
        if brand in ("UNKNOWN", ""):
            brand = _infer_brand_from_part_no(part_no) or brand

        item_lines = []
        specs = item.get("technical_specs") or []
        if isinstance(specs, list) and specs:
            item_lines.append("Specifications:")
            for spec in specs:
                spec_text = str(spec).strip()
                if spec_text:
                    item_lines.append(f"• {spec_text}")

        accessories = item.get("compatible_accessories") or []
        if isinstance(accessories, list) and accessories:
            item_lines.append("Compatible accessories:")
            for acc in accessories:
                acc_text = str(acc).strip()
                if acc_text:
                    item_lines.append(f"• {acc_text}")

        catalog_url = _sanitize_item_catalog_url(
            str(item.get("catalog_url") or "").strip(),
            part_no,
            brand,
        )
        if catalog_url:
            item_lines.append(f"Catalog: {catalog_url}")

        if item_lines:
            header = f"{part_no}"
            if brand and brand.upper() not in ("UNKNOWN", ""):
                header = f"{brand} {part_no}"
            block = header + "\n" + "\n".join(item_lines)
            if block not in "\n\n".join(sections):
                sections.append(block)

    if sections:
        return "\n\n".join(sections).strip()

    return build_ai_research_summary(formatted_rows, copilot_items=copilot_items)


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




def _sanitize_whatsapp_reply(text: str, preserve_urls: bool = False) -> str:
    """Strip markdown links/formatting Copilot sometimes adds."""
    cleaned = str(text or "").strip()
    if preserve_urls:
        cleaned = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", cleaned)
    else:
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
