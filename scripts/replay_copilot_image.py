#!/usr/bin/env python3
"""Replay a saved WA_Image file through Copilot extraction (for debugging).

Run with OpenClaw's environment (not system python3):

  # Full OpenClaw pipeline (pass1 + verify/retries):
  bash scripts/replay_copilot_image.sh /path/to/image_full.jpg --caption "PLS QUOTE"

  # Manual Copilot UI parity (single pass, minimal prompt — use this to match your UI test):
  bash scripts/replay_copilot_image.sh /path/to/image_full.jpg --manual

  # Expect Allen-Bradley table row:
  bash scripts/replay_copilot_image.sh /path/to/image_full.jpg --manual \\
    --expect-part 150-C25NBD --expect-qty 3
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
        "  bash scripts/replay_copilot_image.sh <image> [--manual]\n"
        "  cd /Users/evon/OpenClaw && uv run python scripts/replay_copilot_image.py <image>",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


def _check_expectations(result: dict, expect_part: str, expect_qty: int) -> bool:
    items = result.get("items") or []
    if not expect_part and expect_qty is None:
        return bool(items)
    part_u = str(expect_part or "").strip().upper()
    for item in items:
        got_part = str(item.get("part_no") or "").strip().upper()
        got_qty = int(item.get("qty") or 0)
        if part_u and got_part != part_u:
            continue
        if expect_qty is not None and got_qty != expect_qty:
            continue
        print(f"✅ EXPECTATION MET: part_no={got_part} qty={got_qty}")
        return True
    print(
        f"❌ EXPECTATION FAILED: wanted part={part_u or '?'} qty={expect_qty}, "
        f"got {[(i.get('part_no'), i.get('qty')) for i in items]}"
    )
    return False


def main():
    parser = argparse.ArgumentParser(description="Replay one image through unified Copilot analyze.")
    parser.add_argument("image_path", help="Path to WA_Image file (e.g. *_full.jpg)")
    parser.add_argument(
        "--caption",
        default="MORNING MS AMEERA PLS QUOTE",
        help="Customer caption to include with the image",
    )
    parser.add_argument(
        "--no-caption",
        action="store_true",
        help="Send unified prompt + image only (no caption block)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Manual Copilot UI parity: single pass, minimal prompt (no path/dims, no retries)",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="Pass1 only — skip verify/retries (keeps default prompt unless --manual)",
    )
    parser.add_argument(
        "--expect-part",
        default="",
        help="Fail unless extracted part_no matches (e.g. 150-C25NBD)",
    )
    parser.add_argument(
        "--expect-qty",
        type=int,
        default=None,
        help="Fail unless extracted qty matches (e.g. 3)",
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
    caption = "" if args.no_caption else args.caption

    print(f"Image: {image_path}")
    print(f"Bytes: {size} | dimensions: {dim_label} | valid: {ok} ({reason})")
    print(f"Caption: {caption!r}")
    if args.manual:
        print("Mode: MANUAL parity (single pass + minimal prompt — same as Copilot UI test)")
    elif args.single_pass:
        print("Mode: SINGLE_PASS (pass1 only)")
    else:
        print("Mode: FULL pipeline (pass1 + verify/retries)")
    print("Calling analyze_incoming_inquiry_with_copilot...")
    print("-" * 60)

    result = analyze_incoming_inquiry_with_copilot(
        message_text=caption,
        image_path=image_path,
        single_pass=args.manual or args.single_pass,
        minimal_prompt=args.manual,
    )
    print(json.dumps(result, indent=2, default=str))

    has_items = bool(result.get("items"))
    if args.expect_part or args.expect_qty is not None:
        return 0 if _check_expectations(result, args.expect_part, args.expect_qty) else 1
    return 0 if has_items else 1


if __name__ == "__main__":
    raise SystemExit(main())
