import subprocess
import sys
import time
import os
import json
import base64
import mimetypes
import signal
import re

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

VERSION = "v1.03-LOCAL-OCR-COPILOT"

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o")

_RFQ_SYSTEM_INSTRUCTION = (
    "You are an industrial automation data extraction assistant. "
    "Visually inspect the provided industrial product photo, label, nameplate, barcode sticker, "
    "and/or customer text. Extract EVERY distinct manufacturer part number visible. "
    "Read printed model/order codes exactly as shown on the label/nameplate in THIS photo only. "
    "Read character-by-character; do not substitute a different catalog number. "
    "OMRON proximity sensors use E2E- (example E2E-X5E1). OMRON temperature controllers use E5CC-/E5CN-. "
    "OMRON programmable controllers use CPM1A- (example CPM1A-30CDR-D-V1). "
    "If the label says PROXIMITY SENSOR or shows E2E-, never return E5CC/E5CN. "
    "Match the brand field to the visible manufacturer logo (OMRON, SMC, etc.). "
    "Never reuse a part number from chat history or from a different message. "
    "If multiple labelled products appear in one photo, return one JSON object per distinct part number. "
    "For relays, solenoids, coils, and power products, voltage and AC/DC are mandatory: "
    "include them in 'part_no' exactly as shown (for example 'MY2N-GS-R 24VDC'). "
    "Never substitute another voltage variant. "
    "Use customer caption for quantity hints: 'Quote 2 pcs' with two visible parts often means qty 1 each; "
    "a single visible part with '2 pcs' means qty 2. "
    "Return STRICTLY a raw JSON array of objects with keys 'part_no', 'qty', 'brand', and 'product_type'. "
    "product_type is the visible product description on the label (example: PROXIMITY SENSOR, LIMIT SWITCH). "
    "Quantity must be a positive integer. Do not guess missing part numbers. "
    "If quantity is not visible and caption is absent, use 1. If brand is not visible, use 'UNKNOWN'. "
    "Do not include markdown, backticks, or conversational text. "
    'Example: [{"part_no": "E2E-X5E1", "qty": 1, "brand": "OMRON", "product_type": "PROXIMITY SENSOR"}, '
    '{"part_no": "P36203010#1", "qty": 1, "brand": "SMC", "product_type": "CYLINDER"}]'
)

_RFQ_OCR_SYSTEM_INSTRUCTION = (
    "You are an industrial automation data extraction assistant. "
    "The customer sent a product photo, but you receive ONLY local OCR text (JSON) plus the caption — "
    "not the image itself. Parse the OCR lines carefully; read model/order codes character-by-character. "
    "OMRON proximity sensors use E2E-. OMRON temperature controllers use E5CC-/E5CN-. "
    "OMRON programmable controllers use CPM1A-. "
    "Extract EVERY distinct manufacturer part number present in the OCR output. "
    "Use customer caption for quantity hints. "
    "Return STRICTLY a raw JSON array of objects with keys 'part_no', 'qty', 'brand', and 'product_type'. "
    "product_type is the product description visible in OCR (example: PROGRAMMABLE CONTROLLER). "
    "Quantity must be a positive integer. Do not guess missing part numbers. "
    "If quantity is not visible and caption is absent, use 1. If brand is not visible, use 'UNKNOWN'. "
    "Do not include markdown, backticks, or conversational text."
)


def _ai_fallback_enabled() -> bool:
    """Return True when OpenAI fallback is configured and allowed."""
    if not os.getenv("OPENAI_API_KEY"):
        return False
    mode = os.getenv("OPENCLAW_AI_FALLBACK", "openai").strip().lower()
    return mode not in ("0", "false", "no", "off", "none")


def _copilot_error_details(exc: Exception) -> dict:
    """Normalize Copilot proxy failures for logging and monitor alerts."""
    if isinstance(exc, APIStatusError):
        body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        return {
            "status": exc.status_code,
            "type": str(err.get("type") or "api_error"),
            "message": str(err.get("message") or exc),
        }
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return {
            "status": 0,
            "type": "connection_error",
            "message": str(exc),
        }
    return {
        "status": 0,
        "type": "unknown_error",
        "message": str(exc),
    }


def _should_fallback_to_openai(exc: Exception) -> bool:
    if not _ai_fallback_enabled():
        return False
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (502, 503, 504, 429) or exc.status_code >= 500
    return isinstance(exc, (APIConnectionError, APITimeoutError))


def _parse_rfq_json_array(raw_content: str, image_path: str = None) -> list:
    raw_content = str(raw_content or "").strip()
    if raw_content.startswith("```"):
        lines = raw_content.splitlines()
        raw_content = "\n".join(lines[1:-1]).strip()

    parsed = json.loads(raw_content)
    if not isinstance(parsed, list):
        raise ValueError("AI response must be a JSON array")

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
            extracted_items.append({"part_no": part_no, "qty": qty, "brand": brand})
    return extracted_items


def _build_rfq_user_content(raw_email_body: str, image_path: str = None):
    user_text = (
        "Identify every industrial part in THIS customer message only. "
        "Read only the attached photo and caption below — ignore all prior chat context. "
        f"Customer caption/text:\n{raw_email_body or '(none)'}"
    )
    if not image_path:
        return user_text

    with open(image_path, "rb") as image_file:
        image_b64 = base64.b64encode(image_file.read()).decode("ascii")
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
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


def _build_ocr_copilot_user_content(raw_email_body: str, ocr_payload: dict) -> str:
    from local_ocr import ocr_payload_to_json

    return (
        "The customer sent a product label/nameplate photo. "
        "Local OCR extracted the printed text below as JSON. "
        "Use ONLY this OCR output and the customer caption — do not invent part numbers.\n\n"
        f"Customer caption/text:\n{raw_email_body or '(none)'}\n\n"
        f"OCR result (JSON):\n{ocr_payload_to_json(ocr_payload)}"
    )


def _call_copilot_rfq(
    client: OpenAI,
    system_instruction: str,
    user_content,
    parse_image_path: str = None,
) -> list:
    response = client.chat.completions.create(
        model=COPILOT_MODEL,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content},
        ],
    )
    raw_content = (response.choices[0].message.content or "").strip()
    print(f"[COPILOT RAW] {raw_content}")
    return _parse_rfq_json_array(raw_content, image_path=parse_image_path)


def _normalize_part_key(part_no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part_no or "").upper())


def _visual_part_consistent(part_no: str, brand: str, product_type: str) -> bool:
    """Reject obvious vision mismatches between label type and model family."""
    part_u = str(part_no or "").upper().strip()
    part_key = _normalize_part_key(part_u)
    brand_u = str(brand or "").upper().strip()
    type_u = str(product_type or "").upper().strip()

    if not part_u:
        return False

    if type_u:
        if "PROXIMITY" in type_u and not part_key.startswith("E2E"):
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

    return True


def extract_rfq_with_copilot(raw_email_body: str = "", image_path: str = None) -> list:
    """Extract RFQ items from text and/or an image through the local Copilot proxy."""
    result = unified_analyze(raw_email_body, image_path=image_path)
    return result.get("items") or []


def _extract_rfq_with_copilot_only(raw_email_body: str = "", image_path: str = None) -> dict:
    """
    Copilot-only extraction. Image inquiries prefer local OCR → Copilot text
    to save vision tokens; falls back to Copilot vision when OCR is empty.
    """
    if not str(raw_email_body or "").strip() and not image_path:
        return {"items": [], "route": "none", "ocr_used": False}

    from local_ocr import extract_text_from_image, has_usable_ocr_text, ocr_enabled

    print("[API READ] Sending payload to local Copilot server...")
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=30.0,
        max_retries=1,
    )

    try:
        if image_path and ocr_enabled():
            ocr_payload = extract_text_from_image(image_path)
            if has_usable_ocr_text(ocr_payload):
                print(
                    f"[OCR] Extracted {len(ocr_payload.get('lines') or [])} line(s) "
                    f"from {image_path} — routing text-only to Copilot"
                )
                items = _call_copilot_rfq(
                    client,
                    _RFQ_OCR_SYSTEM_INSTRUCTION,
                    _build_ocr_copilot_user_content(raw_email_body, ocr_payload),
                    parse_image_path=None,
                )
                if items:
                    return {"items": items, "route": "ocr_copilot", "ocr_used": True}
                print("[OCR] Copilot found no parts from OCR text — retrying with Copilot vision...")
            else:
                reason = ocr_payload.get("error") or "no_text_detected"
                print(f"[OCR] Skipping OCR route ({reason}) — using Copilot vision")

        route = "copilot_visual" if image_path else "copilot_text"
        items = _call_copilot_rfq(
            client,
            _RFQ_SYSTEM_INSTRUCTION,
            _build_rfq_user_content(raw_email_body, image_path),
            parse_image_path=image_path,
        )
        return {"items": items, "route": route, "ocr_used": False}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] Copilot returned invalid JSON data: {exc}")
        raise
    except Exception as exc:
        print(f"[ERROR] Failed to communicate with local Copilot server: {exc}")
        raise


def _extract_rfq_with_openai(raw_email_body: str = "", image_path: str = None) -> list:
    """OpenAI fallback for RFQ extraction when the Copilot proxy is unavailable."""
    if not _ai_fallback_enabled():
        return []

    if image_path:
        from image_inquiry_analyzer import analyze_inquiry_image

        print("[FALLBACK] Copilot unavailable — trying OpenAI vision extraction...")
        analysis = analyze_inquiry_image(image_path, caption_text=raw_email_body or "")
        items = []
        for item in analysis.get("items") or []:
            part_no = str(item.get("part_no") or "").strip().upper()
            try:
                qty = int(item.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            if part_no and qty > 0:
                items.append({
                    "part_no": part_no,
                    "qty": qty,
                    "brand": str(item.get("brand") or "UNKNOWN").strip().upper(),
                })
        return items

    if not str(raw_email_body or "").strip():
        return []

    print("[FALLBACK] Copilot unavailable — trying OpenAI text extraction...")
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=45.0, max_retries=1)
    response = client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        messages=[
            {"role": "system", "content": _RFQ_SYSTEM_INSTRUCTION},
            {"role": "user", "content": _build_rfq_user_content(raw_email_body, image_path=None)},
        ],
        temperature=0.1,
    )
    raw_content = (response.choices[0].message.content or "").strip()
    print(f"[OPENAI RAW] {raw_content}")
    return _parse_rfq_json_array(raw_content, image_path=None)


def unified_analyze(raw_email_body: str = "", image_path: str = None) -> dict:
    """
    Unified RFQ extraction:
      1. Image → local OCR → Copilot text (primary, saves tokens)
      2. Copilot vision/text if OCR empty
      3. OpenAI vision/text only when Copilot fails (secondary fallback)
    """
    if not str(raw_email_body or "").strip() and not image_path:
        return {
            "items": [],
            "source": "none",
            "route": "none",
            "ocr_used": False,
            "copilot_failed": False,
            "fallback_used": False,
            "error": None,
        }

    copilot_error = None
    copilot_exc = None
    copilot_route = "none"
    ocr_used = False
    try:
        copilot_result = _extract_rfq_with_copilot_only(raw_email_body, image_path=image_path)
        items = copilot_result.get("items") or []
        copilot_route = copilot_result.get("route") or "copilot"
        ocr_used = bool(copilot_result.get("ocr_used"))
        return {
            "items": items,
            "source": "copilot" if items else "none",
            "route": copilot_route,
            "ocr_used": ocr_used,
            "copilot_failed": False,
            "fallback_used": False,
            "error": None,
        }
    except Exception as exc:
        copilot_exc = exc
        copilot_error = _copilot_error_details(exc)
        print(
            f"[WARN] Copilot unified_analyze failed "
            f"(status={copilot_error['status']}, type={copilot_error['type']})"
        )

    if not _should_fallback_to_openai(copilot_exc):
        return {
            "items": [],
            "source": "none",
            "route": copilot_route,
            "ocr_used": ocr_used,
            "copilot_failed": True,
            "fallback_used": False,
            "error": copilot_error,
        }

    try:
        items = _extract_rfq_with_openai(raw_email_body, image_path=image_path)
        return {
            "items": items,
            "source": "openai" if items else "none",
            "route": "openai_vision" if image_path else "openai_text",
            "ocr_used": ocr_used,
            "copilot_failed": True,
            "fallback_used": True,
            "error": copilot_error,
        }
    except Exception as fallback_exc:
        print(f"[ERROR] OpenAI fallback failed: {fallback_exc}")
        return {
            "items": [],
            "source": "none",
            "route": "openai_failed",
            "ocr_used": ocr_used,
            "copilot_failed": True,
            "fallback_used": True,
            "error": copilot_error,
        }


def build_copilot_malfunction_alert(
    operation: str,
    customer_name: str,
    error: dict,
    caption: str = "",
    original_message: str = "",
) -> str:
    """Build a monitor alert when Copilot fails during unified_analyze."""
    lines = [
        "[OpenClaw Copilot Malfunction] Please Check",
        "",
        f"Operation: {operation}",
        f"Customer: {customer_name or '-'}",
    ]
    if error:
        status = error.get("status")
        if status:
            lines.append(f"HTTP status: {status}")
        lines.append(f"Error: {error.get('message') or error}")
    if caption:
        lines.extend(["", f"Caption: {caption}"])
    lines.extend([
        "──────────────────────────────",
        "Original Message",
        original_message or "(empty)",
    ])
    return "\n".join(lines)


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
        "Keep the answer under 180 words. Plain text only. No markdown code fences."
    )
    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an industrial automation product specialist. "
                        "Give accurate, practical summaries for sales staff."
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
        return text
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
    print("   Running Email + WhatsApp Automation")
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
