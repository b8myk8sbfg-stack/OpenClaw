import subprocess
import sys
import time
import os
import json
import base64
import mimetypes
import signal
import re

from openai import OpenAI

VERSION = "v1.01-UNIFIED-RUNNER"

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")


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
    if not str(raw_email_body or "").strip() and not image_path:
        return []

    print("[API READ] Sending raw data payload to local Copilot server...")
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=30.0,
        max_retries=1,
    )
    system_instruction = (
        "You are an industrial automation data extraction assistant. "
        "Visually inspect the provided industrial product photo, label, nameplate, barcode sticker, "
        "and/or customer text. Extract EVERY distinct manufacturer part number visible. "
        "Read printed model/order codes exactly as shown on the label/nameplate in THIS photo only. "
        "Read character-by-character; do not substitute a different catalog number. "
        "OMRON proximity sensors use E2E- (example E2E-X5E1). OMRON temperature controllers use E5CC-/E5CN-. "
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

    try:
        user_text = (
            "Identify every industrial part in THIS customer message only. "
            "Read only the attached photo and caption below — ignore all prior chat context. "
            f"Customer caption/text:\n{raw_email_body or '(none)'}"
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

        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
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
                extracted_items.append({"part_no": part_no, "qty": qty, "brand": brand})
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
