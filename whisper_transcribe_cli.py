#!/usr/bin/env python3
"""CLI helper for Copilot server → OpenClaw Whisper (keeps heavy deps in OpenClaw uv env)."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe audio with local Whisper")
    parser.add_argument("audio_path")
    parser.add_argument("--model", default=os.getenv("OPENCLAW_WHISPER_MODEL", "small"))
    parser.add_argument(
        "--language",
        default=os.getenv("OPENCLAW_WHISPER_LANGUAGE", "auto"),
        help="Whisper language code (en, zh, ms) or auto/detect to let Whisper choose",
    )
    parser.add_argument(
        "--prompt",
        default=os.getenv(
            "OPENCLAW_WHISPER_PROMPT",
            "Quote quotation RFQ part number qty pieces meter E3Z-T61 报价 berapa harga pcs unit industrial parts.",
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON with text and detected language")
    args = parser.parse_args()

    import whisper  # noqa: WPS433

    opts = {"fp16": False, "condition_on_previous_text": False}
    if args.language and args.language.lower() not in ("auto", "detect"):
        opts["language"] = args.language.lower()
    if args.prompt.strip():
        opts["initial_prompt"] = args.prompt.strip()

    model = whisper.load_model(args.model)
    result = model.transcribe(args.audio_path, **opts)
    text = str(result.get("text") or "").strip()
    detected = str(result.get("language") or "").strip().lower()
    if args.json:
        print(json.dumps({"text": text, "language": detected or None}, ensure_ascii=False))
    else:
        print(text)
        if detected:
            print(f"WHISPER_LANGUAGE={detected}", file=sys.stderr)
    return 0 if text else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
