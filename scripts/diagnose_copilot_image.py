#!/usr/bin/env python3
"""Prove whether Copilot API proxy actually reads the attached image bytes.

Run on Mac (requires Windows-Copilot-API on 127.0.0.1:8000):

  bash scripts/diagnose_copilot_image.sh /path/to/image_full.jpg
  bash scripts/diagnose_copilot_image.sh /path/to/image_full.jpg --unified

Compares:
  A) Short vision probe WITHOUT image
  B) Short vision probe WITH image
  C) Optional: full OPENCLAW unified JSON prompt WITH image (--unified)

Check Copilot proxy terminal for:
  [copilot] chat/completions image: 31199 bytes, prompt_chars=...
"""

import argparse
import hashlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from openclaw_main import (
        COPILOT_BASE_URL,
        COPILOT_MODEL,
        OPENCLAW_UNIFIED_PROMPT,
        _build_extraction_user_prompt,
        _copilot_fresh_chat,
        _copilot_user_content_with_image,
        analyze_incoming_inquiry_with_copilot,
    )
    from openai import OpenAI
    from whatsapp_attachment_processor import describe_image_file, is_degraded_wa_capture
except ModuleNotFoundError:
    print("Run via: bash scripts/diagnose_copilot_image.sh <image>", file=sys.stderr)
    raise SystemExit(1) from None

VISION_PROBE = """You are reading ONE attached image only.

Transcribe every visible catalog/model/part number, brand name, and quantity exactly as printed in the image.
Rules:
- Use ONLY characters you can literally see in the image
- Do NOT guess from memory or training examples
- If you cannot read any text, reply with exactly: UNREADABLE

Reply plain text only:
Brand: ...
Model: ...
Qty: ..."""


def _call_copilot(prompt: str, image_path: str = None) -> str:
    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=120.0,
        max_retries=1,
    )
    content = _copilot_user_content_with_image(prompt, image_path)
    has_image = image_path and os.path.exists(image_path)
    label = "with-image" if has_image else "text-only"
    print(f"\n--- Copilot call ({label}) ---")
    response = _copilot_fresh_chat(
        client,
        [{"role": "user", "content": content}],
        timeout=120.0 if has_image else 60.0,
    )
    text = (response.choices[0].message.content or "").strip()
    print(text[:2000])
    return text


def _catalog_tokens(text: str) -> list:
    blob = text.upper()
    hits = []
    for token in ("E3Z-D61", "E3Z", "ER6C", "H3JA", "3RT2026", "3RT", "E2E-X5ME1", "3G3MX", "150-C25NBD"):
        if token in blob:
            hits.append(token)
    return hits


def main():
    parser = argparse.ArgumentParser(description="Diagnose Copilot vision vs memory hallucination.")
    parser.add_argument("image_path", help="Path to JPEG/PNG to test")
    parser.add_argument(
        "--unified",
        action="store_true",
        help="Also run full OPENCLAW unified JSON extraction (pass1 only)",
    )
    args = parser.parse_args()

    image_path = os.path.expanduser(args.image_path)
    if not os.path.exists(image_path):
        print(f"ERROR: not found: {image_path}", file=sys.stderr)
        return 1

    with open(image_path, "rb") as handle:
        raw = handle.read()
    md5 = hashlib.md5(raw).hexdigest()
    size, dims, dim_label = describe_image_file(image_path)
    degraded, degrade_reason = is_degraded_wa_capture(image_path)

    print("=" * 60)
    print("COPILOT IMAGE VISION DIAGNOSTIC")
    print("=" * 60)
    print(f"File: {image_path}")
    print(f"Bytes: {size} | dimensions: {dim_label} | MD5: {md5}")
    print(f"Degraded capture: {degraded} ({degrade_reason})")
    print(f"Proxy: {COPILOT_BASE_URL} | model: {COPILOT_MODEL}")
    print("=" * 60)

    text_only = _call_copilot(VISION_PROBE, image_path=None)
    with_image = _call_copilot(VISION_PROBE, image_path=image_path)

    hits_text = _catalog_tokens(text_only)
    hits_image = _catalog_tokens(with_image)

    unified_raw = ""
    if args.unified:
        print("\n--- Copilot call (unified JSON pass1) ---")
        result = analyze_incoming_inquiry_with_copilot(
            message_text="MORNING MS AMEERA PLS QUOTE",
            image_path=image_path,
            single_pass=True,
            minimal_prompt=True,
        )
        unified_raw = str(result.get("raw_excerpt") or "")
        print(unified_raw[:2000])
        hits_unified = _catalog_tokens(unified_raw)
    else:
        hits_unified = []

    print("\n" + "=" * 60)
    print("ANALYSIS")
    print("=" * 60)
    print(f"Text-only tokens: {hits_text or '(none)'}")
    print(f"Short probe + image tokens: {hits_image or '(none)'}")
    if args.unified:
        print(f"Unified JSON tokens: {hits_unified or '(none)'}")

    if "UNREADABLE" in with_image.upper() and "UNREADABLE" in text_only.upper():
        print("→ Image IS attached (API responded differently than text-only).")
        print("→ At 688x309 the API honestly cannot read text (UNREADABLE).")
        if args.unified and hits_unified:
            print("→ BUT unified JSON prompt INVENTS parts anyway — that is the E3Z/3RT bug.")
            print("→ Fix: reject guesses on degraded captures (v1.50+) OR get full-res image.")
        elif not args.unified:
            print("→ Re-run with --unified to see JSON prompt hallucinate vs UNREADABLE.")
    elif "150-C25NBD" in with_image.upper():
        print("→ API CAN read Allen-Bradley on this file — compare with manual UI.")
    elif hits_image and not hits_text:
        print("→ Image attached but returns catalog tokens — weak/wrong vision.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
