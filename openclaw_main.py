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

VERSION = "v1.33-COPILOT-SINGLE-PASS"

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

Analyze ONLY this attached message and/or image. Do not use prior conversations or catalog memory.

You are the vision expert. Look at the image yourself and decide what the customer is asking about.

Tasks:
1. Classify intent: rfq_inquiry | technical_support | purchase_order | junk_ad | greeting | general_chat | unknown
2. Identify the product the customer wants quoted or discussed.
3. Transcribe the exact model/part code on that product's label, character by character. Never substitute a different catalog number.
4. Describe what is in the foreground vs background. Quote the foreground subject, not background equipment.
5. Extract quantity from caption or image (default 1).
6. Summarize technical details for the identified product.

Image guidance:
- Photo + short caption (e.g. "quote me 1 pc") usually shows ONE product — the item held in hand or closest to the camera.
- Ignore background wiring, terminal blocks, and panel equipment unless the customer is clearly quoting those.
- RFQ/enquiry tables: read only rows visible in the image; count rows; do not invent extra line items.
- Equivalent/replacement requests: intent=technical_support unless they also ask for price/quote.

Return plain-text analysis first. Include:
- What product is in the foreground (what the customer is quoting)
- Exact label transcription (character by character)
- What background items you are ignoring

On the very last line only, append one JSON object (no markdown fences):
{"intent":"rfq_inquiry","confidence":0.9,"visual_analysis":{"foreground_subject":"...","label_transcription":"...","background_ignored":"..."},"items":[{"part_no":"EXACT-CODE","qty":1,"brand":"BRAND","product_type":"TYPE"}],"technical_summary":"...","is_industrial_automation":true,"compatible_brands":[],"reasoning":"..."}

items: one entry per distinct part the customer is inquiring about. part_no must match the label transcription.
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
            "Attached image: identify the foreground product the customer is quoting. "
            "Transcribe its label exactly. Ignore background panels and wiring unless "
            "the customer is clearly quoting background equipment."
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


        if _is_equivalent_support_request(message_text):
            intent = "technical_support"
            if not reasoning or re.search(r"\brfq\b", reasoning, re.I):
                reasoning = (
                    "Equivalent/replacement request — technical support, "
                    "reading product label from photo."
                )

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
