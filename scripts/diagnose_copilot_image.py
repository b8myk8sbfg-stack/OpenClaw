#!/usr/bin/env python3
"""Prove whether Copilot API proxy actually reads the attached image bytes.

Run on Mac (requires Windows-Copilot-API on 127.0.0.1:8000):

  bash scripts/diagnose_copilot_image.sh /path/to/image_full.jpg

Compares:
  A) Short vision probe WITH image
  B) Same probe WITHOUT image (text only)
  C) MD5/size of file bytes sent to proxy

If A and B both return E3Z-D61 / 3RT, the model is guessing from memory — not vision.
"""

import argparse
import hashlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from openai import OpenAI
    from openclaw_main import (
        COPILOT_BASE_URL,
        COPILOT_MODEL,
        _copilot_fresh_chat,
        _copilot_user_content_with_image,
    )
    from whatsapp_attachment_processor import describe_image_file, read_image_dimensions
except ModuleNotFoundError:
    print("Run via: bash scripts/diagnose_copilot_image.sh <image>", file=sys.stderr)
    raise SystemExit(1) from None

VISION_PROBE = """You are reading ONE attached image only.

Transcribe every visible catalog/model/part number, brand name, and quantity exactly as printed in the image.
Rules:
- Use ONLY characters you can literally see in the image
- Do NOT guess from memory or training examples
- Do NOT return E3Z-D61, ER6C, H3JA, 3RT2026 unless those EXACT strings are visible
- If you cannot read any text, reply with exactly: UNREADABLE

Reply plain text only in this format:
Brand: ...
Model: ...
Qty: ...
Other visible text: ..."""


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
    import re
    blob = text.upper()
    hits = []
    for token in ("E3Z-D61", "E3Z", "ER6C", "H3JA", "3RT2026", "3RT", "E2E-X5ME1", "3G3MX"):
        if token in blob:
            hits.append(token)
    return hits


def main():
    parser = argparse.ArgumentParser(description="Diagnose Copilot vision vs memory hallucination.")
    parser.add_argument("image_path", help="Path to JPEG/PNG to test")
    args = parser.parse_args()

    image_path = os.path.expanduser(args.image_path)
    if not os.path.exists(image_path):
        print(f"ERROR: not found: {image_path}", file=sys.stderr)
        return 1

    with open(image_path, "rb") as handle:
        raw = handle.read()
    md5 = hashlib.md5(raw).hexdigest()
    size, dims, dim_label = describe_image_file(image_path)

    print("=" * 60)
    print("COPILOT IMAGE VISION DIAGNOSTIC")
    print("=" * 60)
    print(f"File: {image_path}")
    print(f"Bytes: {size} | dimensions: {dim_label} | MD5: {md5}")
    print(f"Proxy: {COPILOT_BASE_URL} | model: {COPILOT_MODEL}")
    print("=" * 60)

    text_only = _call_copilot(VISION_PROBE, image_path=None)
    with_image = _call_copilot(VISION_PROBE, image_path=image_path)

    hits_a = _catalog_tokens(text_only)
    hits_b = _catalog_tokens(with_image)

    print("\n" + "=" * 60)
    print("ANALYSIS")
    print("=" * 60)
    print(f"Text-only catalog tokens: {hits_a or '(none)'}")
    print(f"With-image catalog tokens: {hits_b or '(none)'}")

    if "UNREADABLE" in with_image.upper():
        print("→ With image: Copilot says UNREADABLE (vision may work but text too small/blur)")
    elif "150-C25NBD" in with_image.upper() or "ALLEN" in with_image.upper():
        print("→ With image: Copilot CAN read Allen-Bradley table via API — unified prompt issue")
    elif hits_b and not hits_a:
        print("→ Image attached but model still hallucinates catalog parts (weak/wrong vision)")
    elif hits_b and hits_a:
        print("→ SAME catalog tokens with/without image — likely NOT using image (memory guess)")
    elif not hits_b and "150-C25NBD" not in with_image.upper():
        print("→ Check raw responses above for what Copilot actually returned")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
