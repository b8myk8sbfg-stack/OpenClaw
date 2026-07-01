import subprocess
import sys
import time
import os
import json
import base64
import mimetypes
import signal

from openai import OpenAI

VERSION = "v1.00-UNIFIED-RUNNER"

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")


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
        "Visually inspect the provided industrial product photo, label, nameplate, screenshot, "
        "and/or customer text. Extract the exact manufacturer Part Number and matching Quantity. "
        "Read printed model/order codes and electrical ratings carefully. For relays, solenoids, "
        "coils, and power products, voltage and AC/DC are mandatory parts of the configuration: "
        "include them in 'part_no' exactly as shown (for example 'MY2N-GS-R 24VDC'). "
        "Never substitute another voltage variant. Prefer the complete code on the product label. "
        "Return STRICTLY a raw JSON array of objects with keys 'part_no', 'qty', and 'brand'. "
        "Quantity must be a positive integer. Do not guess missing part numbers. "
        "If quantity is not visible, use 1. If brand is not visible, use 'UNKNOWN'. "
        "Do not include markdown, backticks, or conversational text. "
        'Example: [{"part_no": "MY2N-GS-R 24VDC", "qty": 10, "brand": "OMRON"}]'
    )

    try:
        user_text = (
            "Identify every industrial part in this customer inquiry. "
            "Use only the current request; never reuse a part number from chat history. "
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
                extracted_items.append({"part_no": part_no, "qty": qty, "brand": brand})
        return extracted_items
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] Copilot returned invalid JSON data: {exc}")
    except Exception as exc:
        print(f"[ERROR] Failed to communicate with local Copilot server: {exc}")
    return []


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
