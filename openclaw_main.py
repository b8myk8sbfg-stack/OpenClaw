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

VERSION = "v1.08-EQUIVALENT-NO-E2E"

BASE_DIR = "/Users/evon/OpenClaw"

EMAIL_SCRIPT = os.path.join(BASE_DIR, "auto_claw.py")
WHATSAPP_SCRIPT = os.path.join(BASE_DIR, "whatsapp_inbox_watcher.py")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")


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
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
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
            if not _normalize_part_key(item.get("part_no")).startswith("E2E")
        ]
        if filtered:
            return filtered

    has_proximity = any(
        "PROXIMITY" in str(item.get("product_type") or "").upper()
        or "SENSOR" in str(item.get("product_type") or "").upper()
        or _normalize_part_key(item.get("part_no")).startswith("E2E")
        for item in items
    )
    if has_proximity:
        filtered = [
            item for item in items
            if not (
                _normalize_part_key(item.get("part_no")).startswith("H3J")
                or _normalize_part_key(item.get("part_no")).startswith("H3Y")
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
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
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
        "LITHIUM/BATTERY/ER6C/ER17500 = battery — never E2E/H3JA. "
        "PROXIMITY SENSOR/E2E- = proximity sensor — never H3JA/battery. "
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
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
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


def _resolve_visual_items(caption: str, image_path: str = None, copilot_items: list = None) -> list:
    """Merge prior extraction, label OCR, and RFQ visual extraction."""
    items = list(copilot_items or [])
    label_item = {}

    if image_path and os.path.exists(image_path):
        label_item = extract_label_from_image(caption, image_path=image_path)
        if not label_item and _is_equivalent_support_request(caption):
            label_item = extract_timer_label_from_image(caption, image_path=image_path)
        if label_item:
            items = _reconcile_visual_items(items, label_item)
        elif not items:
            print("[COPILOT VISUAL] Label OCR empty — trying structured RFQ visual extraction...")
            items = extract_rfq_with_copilot(caption, image_path=image_path)

    items = _filter_conflicting_visual_items(items)

    if _is_e2e_only_guess(items, label_item):
        if label_item:
            print("[COPILOT VISUAL] Using label OCR over unconfirmed E2E guess(es).")
            items = [label_item]
        elif _is_quote_without_part_text(caption) or _is_equivalent_support_request(caption):
            timer_label = extract_timer_label_from_image(caption, image_path) if image_path else {}
            if timer_label:
                print("[COPILOT VISUAL] Timer OCR recovered label after rejecting E2E guess.")
                items = [timer_label]
            else:
                print("[COPILOT VISUAL] Rejecting unconfirmed E2E guess on photo caption.")
                items = []
    elif _is_unconfirmed_e2e_guess(items, label_item):
        if label_item:
            print("[COPILOT VISUAL] Using label OCR over unconfirmed E2E guess.")
            items = [label_item]
        elif _is_quote_without_part_text(caption):
            print("[COPILOT VISUAL] Rejecting unconfirmed E2E guess on quote caption + photo.")
            items = []

    return items


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
        lines.extend(["", "We currently have these Ex-Stock / available in our warehouse:", warehouse_context])
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


def _fallback_equivalent_photo_reply() -> str:
    return (
        "Hi, thank you for reaching out.\n\n"
        "I received your product photo and I am reading the label to recommend the correct "
        "equivalent / successor part.\n\n"
        "If you can, please also type the exact model number printed on the nameplate "
        "(for example OMRON H3JA-8A) and the time range on the dial (5S, 30S, 3M) so I can "
        "match the right replacement quickly."
    )


def build_technical_support_reply(
    message_text: str = "",
    image_path: str = None,
    copilot_items: list = None,
) -> str:
    """Use Copilot + warehouse stock to answer technical support / equivalent-part questions."""
    if os.getenv("OPENCLAW_COPILOT_TECH_SUPPORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""

    from openclaw_inquiry_engine import build_warehouse_support_context

    message_text = str(message_text or "").strip()
    if not message_text and not image_path:
        return ""

    # Always re-read the photo for tech support — never trust stale RFQ extraction.
    fresh_items = None if image_path else copilot_items
    visual_items = _resolve_visual_items(message_text, image_path=image_path, copilot_items=fresh_items)
    timer_items = [item for item in visual_items if _is_timer_visual_item(item)]

    if timer_items:
        part_refs, warehouse_context = build_warehouse_support_context(
            message_text,
            part_refs=_part_refs_from_copilot_items(timer_items),
        )
        print("[COPILOT TECH SUPPORT] Timer detected — using deterministic equivalent reply")
        return _build_timer_equivalent_reply(timer_items, warehouse_context)

    if image_path and _is_equivalent_support_request(message_text):
        if _is_e2e_only_guess(visual_items):
            print("[COPILOT TECH SUPPORT] Rejecting E2E-only guess on equivalent photo request")
            visual_items = []
        if not visual_items:
            label_item = extract_label_from_image(message_text, image_path=image_path)
            if not label_item:
                label_item = extract_timer_label_from_image(message_text, image_path=image_path)
            if label_item and _is_timer_visual_item(label_item):
                part_refs, warehouse_context = build_warehouse_support_context(
                    message_text,
                    part_refs=_part_refs_from_copilot_items([label_item]),
                )
                return _build_timer_equivalent_reply([label_item], warehouse_context)
            print("[COPILOT TECH SUPPORT] Equivalent photo — no reliable label read, using safe fallback")
            return _fallback_equivalent_photo_reply()

    part_refs = _part_refs_from_copilot_items(visual_items)
    if image_path and _is_equivalent_support_request(message_text) and (
        not part_refs or all(_is_e2e_family_part(part) for part in part_refs)
    ):
        print("[COPILOT TECH SUPPORT] Blocking E2E warehouse reply on equivalent photo")
        return _fallback_equivalent_photo_reply()

    part_refs, warehouse_context = build_warehouse_support_context(
        message_text,
        part_refs=part_refs if part_refs else None,
    )

    if part_refs:
        parts_label = ", ".join(part_refs)
        identification_note = (
            f"Model identified from customer text/label: {parts_label}. "
            "You MUST state this exact model in your reply. "
            "Do NOT substitute a different part family (e.g. never say E2E if the label is H3JA)."
        )
    else:
        parts_label = "(not yet identified from text)"
        identification_note = (
            "No part number in the customer text. You MUST read the attached product "
            "label/nameplate and state the exact printed model number (e.g. OMRON H3JA-8A). "
            "Do not ask the customer to re-send the model if it is clearly visible on the label. "
            "Never guess E2E-X5E1 unless that exact code is printed on the label."
        )

    print(f"[COPILOT TECH SUPPORT] Parts detected: {parts_label}")
    if warehouse_context:
        print("[COPILOT TECH SUPPORT] Warehouse matches found — prioritising in-stock SKUs")

    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=60.0,
        max_retries=1,
    )

    system_prompt = (
        "You are a senior industrial automation technical sales engineer at Robomatics (Malaysia). "
        "Answer customer technical support questions clearly and practically on WhatsApp. "
        "Use ONLY the identified model from the label — never invent or substitute a different catalog number. "
        "NEVER mention E2E-X5E1 or E2E-X5ME1 unless those exact models were identified from the label. "
        "When the customer asks for an equivalent, replacement, or successor part, recommend the "
        "best modern replacement and explain briefly why. "
        "For OMRON H3JA or H3Y timer relays, the usual modern successor is H3CR-A8 (8-pin socket, DPDT). "
        "ALWAYS prioritise recommending parts listed in the warehouse stock section below. "
        "If we have Ex-Stock quantity, say so. Do not recommend external distributors if we stock a suitable item. "
        "Only if the time range on the dial or timing mode is not visible on the label, ask 1-2 short clarifying questions. "
        "Plain text only. No markdown, no hyperlinks, no asterisks. Friendly professional tone. Under 280 words."
    )

    user_prompt = (
        f"Customer message:\n{message_text or '(see attached photo)'}\n\n"
        f"{identification_note}\n\n"
        "Our warehouse stock to PRIORITISE (check these first):\n"
        f"{warehouse_context or '(no matching warehouse stock found — give best technical guidance anyway)'}\n\n"
        "Write the WhatsApp reply to the customer now."
    )

    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = _sanitize_whatsapp_reply(response.choices[0].message.content or "")
        if text.startswith("```"):
            lines = text.splitlines()
            text = _sanitize_whatsapp_reply("\n".join(lines[1:-1]))
        if not text:
            return ""
        if _reply_contradicts_visual_items(text, visual_items):
            if any(_is_timer_visual_item(item) for item in visual_items):
                print("[COPILOT TECH SUPPORT] Regenerating with deterministic timer template")
                return _build_timer_equivalent_reply(
                    [item for item in visual_items if _is_timer_visual_item(item)] or visual_items,
                    warehouse_context,
                )
            if image_path and _is_equivalent_support_request(message_text):
                return _fallback_equivalent_photo_reply()
            return ""
        if not text.lower().startswith("hi"):
            text = f"Hi, thank you for reaching out.\n\n{text}"
        print(f"[COPILOT TECH SUPPORT] Generated {len(text)} char reply")
        return text
    except Exception as exc:
        print(f"[WARN] Copilot technical support failed: {exc}")
        if visual_items and any(_is_timer_visual_item(item) for item in visual_items):
            return _build_timer_equivalent_reply(visual_items, warehouse_context)
        return ""


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
