"""
Local OCR for image inquiries — extracts printed text before Copilot parsing.

Uses Tesseract (via pytesseract). Install on macOS:
    brew install tesseract
"""

import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional

VERSION = "v1.01-OCR-MULTI-PSM"

DEFAULT_LANG = os.getenv("OPENCLAW_OCR_LANG", "eng+chi_sim")
MIN_LINE_CONFIDENCE = float(os.getenv("OPENCLAW_OCR_MIN_CONFIDENCE", "25"))
MIN_UPSCALE_WIDTH = int(os.getenv("OPENCLAW_OCR_MIN_WIDTH", "1400"))


def ocr_enabled() -> bool:
    """Return True when local OCR routing is enabled."""
    mode = os.getenv("OPENCLAW_OCR_ENABLED", "1").strip().lower()
    return mode not in ("0", "false", "no", "off", "none")


def _resolve_tesseract_cmd() -> Optional[str]:
    override = os.getenv("TESSERACT_CMD", "").strip()
    if override and os.path.isfile(override):
        return override
    return shutil.which("tesseract")


def _preprocess_image(image_path: str):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    if image.width < MIN_UPSCALE_WIDTH:
        scale = MIN_UPSCALE_WIDTH / max(image.width, 1)
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    image = image.convert("L")
    image = ImageEnhance.Contrast(image).enhance(1.8)
    image = image.filter(ImageFilter.SHARPEN)
    return image


def _ocr_lines_from_image(image, psm: int) -> List[Dict[str, Any]]:
    import pytesseract

    config = f"--psm {psm} --oem 3"
    data = pytesseract.image_to_data(
        image,
        lang=DEFAULT_LANG,
        config=config,
        output_type=pytesseract.Output.DICT,
    )
    return _lines_from_tesseract_data(data)


def _merge_ocr_lines(line_groups: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged = []
    seen = set()
    for group in line_groups:
        for line in group:
            text = str(line.get("text") or "").strip()
            key = re.sub(r"\s+", " ", text).upper()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(line)
    return merged


def _lines_from_tesseract_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}

    count = len(data.get("text", []))
    for idx in range(count):
        text = str(data["text"][idx] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][idx])
        except (TypeError, ValueError):
            conf = -1.0
        if conf >= 0 and conf < MIN_LINE_CONFIDENCE:
            continue

        block = int(data["block_num"][idx])
        par = int(data["par_num"][idx])
        line = int(data["line_num"][idx])
        key = (block, par, line)
        grouped.setdefault(key, []).append({
            "text": text,
            "confidence": conf,
            "left": int(data["left"][idx]),
            "top": int(data["top"][idx]),
            "width": int(data["width"][idx]),
            "height": int(data["height"][idx]),
        })

    for key in sorted(grouped.keys()):
        parts = grouped[key]
        line_text = " ".join(part["text"] for part in parts).strip()
        if not line_text:
            continue
        confs = [part["confidence"] for part in parts if part["confidence"] >= 0]
        avg_conf = round(sum(confs) / len(confs), 1) if confs else None
        lines.append({
            "text": line_text,
            "confidence": avg_conf,
            "bbox": {
                "left": min(part["left"] for part in parts),
                "top": min(part["top"] for part in parts),
                "width": max(part["left"] + part["width"] for part in parts)
                - min(part["left"] for part in parts),
                "height": max(part["top"] + part["height"] for part in parts)
                - min(part["top"] for part in parts),
            },
        })

    return lines


def extract_text_from_image(image_path: str) -> Dict[str, Any]:
    """
    Run local OCR on an image file.

    Returns JSON-serializable payload:
        {
            "engine": "tesseract",
            "image_path": "...",
            "full_text": "...",
            "lines": [{"text": "...", "confidence": 92.1, "bbox": {...}}, ...],
            "lang": "eng+chi_sim",
            "error": null | "reason"
        }
    """
    payload: Dict[str, Any] = {
        "engine": "tesseract",
        "image_path": image_path,
        "full_text": "",
        "lines": [],
        "lang": DEFAULT_LANG,
        "error": None,
    }

    if not image_path or not os.path.isfile(image_path):
        payload["error"] = "image_not_found"
        return payload

    tesseract_cmd = _resolve_tesseract_cmd()
    if not tesseract_cmd:
        payload["error"] = "tesseract_not_installed"
        return payload

    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        image = _preprocess_image(image_path)
        psm_modes = [int(x) for x in os.getenv("OPENCLAW_OCR_PSM", "6,11,3").split(",") if x.strip()]
        line_groups = []
        for psm in psm_modes:
            try:
                line_groups.append(_ocr_lines_from_image(image, psm))
            except Exception as exc:
                print(f"[OCR] PSM {psm} failed: {exc}")
        lines = _merge_ocr_lines(line_groups)
        payload["lines"] = lines
        payload["full_text"] = "\n".join(line["text"] for line in lines).strip()
        if payload["full_text"]:
            print(f"[OCR] Merged {len(lines)} line(s) from PSM modes {psm_modes}")
        if not payload["full_text"]:
            payload["error"] = "no_text_detected"
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        print(f"[OCR] Failed on {image_path}: {exc}")
        return payload


def ocr_payload_to_json(ocr_payload: Dict[str, Any]) -> str:
    """Serialize OCR output for Copilot text prompts."""
    slim = {
        "engine": ocr_payload.get("engine"),
        "lang": ocr_payload.get("lang"),
        "full_text": ocr_payload.get("full_text") or "",
        "lines": [
            {"text": line.get("text"), "confidence": line.get("confidence")}
            for line in (ocr_payload.get("lines") or [])
        ],
        "error": ocr_payload.get("error"),
    }
    return json.dumps(slim, ensure_ascii=False, indent=2)


def has_usable_ocr_text(ocr_payload: Dict[str, Any]) -> bool:
    text = str(ocr_payload.get("full_text") or "").strip()
    return bool(text) and not ocr_payload.get("error")
