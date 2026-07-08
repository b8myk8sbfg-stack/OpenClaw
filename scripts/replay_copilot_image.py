#!/usr/bin/env python3
"""Replay a saved WA_Image file through Copilot extraction (for debugging).

Run with OpenClaw's environment (not system python3):

  bash scripts/replay_copilot_image.sh /path/to/image_full.jpg --caption "PLS QUOTE"

  # or:
  cd /Users/evon/OpenClaw && uv run python scripts/replay_copilot_image.py /path/to/image_full.jpg
"""

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from openclaw_main import analyze_incoming_inquiry_with_copilot
    from whatsapp_attachment_processor import read_image_dimensions, validate_image_file
except ModuleNotFoundError as exc:
    print(
        "ERROR: Missing Python dependency for OpenClaw.\n"
        "Do not use system python3. Run instead:\n"
        "  bash scripts/replay_copilot_image.sh <image> [--caption ...]\n"
        "  cd /Users/evon/OpenClaw && uv run python scripts/replay_copilot_image.py <image>",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


def main():
    parser = argparse.ArgumentParser(description="Replay one image through unified Copilot analyze.")
    parser.add_argument("image_path", help="Path to WA_Image file (e.g. *_full.jpg)")
    parser.add_argument(
        "--caption",
        default="MORNING MS AMEERA PLS QUOTE",
        help="Customer caption to include with the image",
    )
    args = parser.parse_args()

    image_path = os.path.expanduser(args.image_path)
    if not os.path.exists(image_path):
        print(f"ERROR: file not found: {image_path}", file=sys.stderr)
        return 1

    ok, reason = validate_image_file(image_path)
    dims = read_image_dimensions(image_path)
    size = os.path.getsize(image_path)
    dim_label = f"{dims[0]}x{dims[1]}" if dims else "unknown"
    print(f"Image: {image_path}")
    print(f"Bytes: {size} | dimensions: {dim_label} | valid: {ok} ({reason})")
    print(f"Caption: {args.caption!r}")
    print("Calling analyze_incoming_inquiry_with_copilot...")
    print("-" * 60)

    result = analyze_incoming_inquiry_with_copilot(
        message_text=args.caption,
        image_path=image_path,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("items") else 1


if __name__ == "__main__":
    raise SystemExit(main())
