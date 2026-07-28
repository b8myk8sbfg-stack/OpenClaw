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

VERSION = "v1.02-OCR-NOISE-FILTER"

DEFAULT_LANG = os.getenv("OPENCLAW_OCR_LANG", "eng+chi_sim")
MIN_LINE_CONFIDENCE = float(os.getenv("OPENCLAW_OCR_MIN_CONFIDENCE", "25"))
MIN_UPSCALE_WIDTH = int(os.getenv("OPENCLAW_OCR_MIN_WIDTH", "2000"))

_OCR_LINE_NOISE_PATTERNS = (
    re.compile(r"^\s*forwarded\s*$", re.I),
    re.compile(r"^\s*quote\s+me\b", re.I),
    re.compile(r"^\s*\d{1,2}\s*:\s*\d{2}\s*(?:am|pm)?\s*$", re.I),
    re.compile(r"^\s*(?:burkert|5urkert|purkert|bürkert)\s*$", re.I),
    re.compile(r"^\s*made\s*(?:in)?\s*germany\s*$", re.I),
    re.compile(r"^\s*(?:pas|pcs|pce|pc)\s*$", re.I),
)


def _is_whatsapp_ui_noise_line(text: str) -> bool:
    line = str(text or "").strip()
    if not line:
        return True
    if len(line) <= 2 and not re.search(r"\d{3,}", line):
        return True
    for pattern in _OCR_LINE_NOISE_PATTERNS:
        if pattern.search(line):
            return True
    if re.fullmatch(r"[\W_]+", line):
        return True
    return False


def filter_whatsapp_ui_noise_from_ocr(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove WhatsApp chat chrome that leaks into thumb/bubble OCR."""
    kept = []
    for line in lines or []:
        text = str(line.get("text") or "").strip()
        if _is_whatsapp_ui_noise_line(text):
            continue
        kept.append(line)
    return kept


def ocr_enabled() -> bool:
    """Return True when local OCR routing is enabled."""
    mode = os.getenv("OPENCLAW_OCR_ENABLED", "1").strip().lower()
    return mode not in ("0", "false", "no", "off", "none")


def _resolve_tesseract_cmd() -> Optional[str]:
    override = os.getenv("TESSERACT_CMD", "").strip()
    if override and os.path.isfile(override):
        return override
    return shutil.which("tesseract")


def _preprocess_image(image_path: str, *, thumb_capture: bool = False, rotate_degrees: int = 0):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    image = ImageOps.exif_transpose(Image.open(image_path))
    if rotate_degrees:
        image = image.rotate(int(rotate_degrees) % 360, expand=True)
    if thumb_capture:
        width, height = image.size
        top = int(height * 0.14)
        side = int(width * 0.04)
        if height - top > 80 and width - (2 * side) > 80:
            image = image.crop((side, top, width - side, height))
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


def _fix_burkert_digit_confusions(compact: str) -> str:
    """Fix letter/digit swaps inside numeric-only Burkert article IDs."""
    text = str(compact or "").upper()
    if not text or not re.fullmatch(r"0*[0-9EIO]{6,12}", text):
        return text
    return (
        text.replace("E", "2")
        .replace("I", "1")
        .replace("O", "0")
    )


def _fix_burkert_ocr_token(token: str) -> str:
    compact = re.sub(r"[^0-9A-Z]", "", str(token or "").upper())
    compact = _fix_burkert_digit_confusions(compact)
    if re.fullmatch(r"0*\d{5,8}S", compact):
        return compact[:-1] + "5"
    return str(token or "")


def _fix_burkert_ocr_in_text(text: str) -> str:
    blob = str(text or "")

    def _replace_token(match: re.Match) -> str:
        return _fix_burkert_ocr_token(match.group(0))

    blob = re.sub(r"\b0?\d{3,8}[EIO]\d{3,8}\b", _replace_token, blob, flags=re.I)
    blob = re.sub(
        r"\b0?\d{5,8}S\b",
        _replace_token,
        blob,
        flags=re.I,
    )
    return blob


def _score_ocr_candidate(lines: List[Dict[str, Any]], full_text: str) -> float:
    text = str(full_text or "").upper()
    compact = re.sub(r"[^0-9A-Z]", "", text)
    score = 0.0

    if re.search(r"00\d{6,7}", compact):
        score += 1000.0
    elif re.search(r"0\d{6,8}", compact):
        score += 500.0
    if "6519" in compact or "6519" in text:
        score += 200.0
    if "BURKERT" in text or "BURK" in text:
        score += 100.0
    if "MADE IN GERMANY" in text:
        score += 50.0

    confs = [
        float(line.get("confidence"))
        for line in (lines or [])
        if line.get("confidence") is not None and float(line.get("confidence")) >= 0
    ]
    if confs:
        score += sum(confs) / len(confs)
    score += min(len(lines or []), 30) * 2.0
    return score


def _score_quotation_ocr_candidate(lines: List[Dict[str, Any]], full_text: str) -> float:
    text = str(full_text or "").upper()
    score = 0.0

    if re.search(r"\bQUOTATION\b", text):
        score += 300.0
    if re.search(r"\bOUR\s*REF\b", text):
        score += 200.0
    if re.search(r"\bUNIT\s*PRICE\b", text):
        score += 150.0
    if re.search(r"\bTOTAL\s*PRICE\b", text):
        score += 150.0
    if re.search(r"\b\d{2,}[A-Z]{1,4}\d+[A-Z0-9-]*\b", text):
        score += 400.0
    if "TRIMMER" in text or "RESISTOR" in text:
        score += 250.0
    if "MOQ" in text:
        score += 200.0
    if re.search(r"\b\d+\s*PCE\b", text):
        score += 100.0

    confs = [
        float(line.get("confidence"))
        for line in (lines or [])
        if line.get("confidence") is not None and float(line.get("confidence")) >= 0
    ]
    if confs:
        score += sum(confs) / len(confs)
    score += min(len(lines or []), 40) * 2.5
    return score


def _orientation_candidates(image_path: str) -> List[int]:
    try:
        from PIL import Image

        with Image.open(image_path) as probe:
            width, height = probe.size
        if height > width * 1.15:
            return [90, 270, 0, 180]
        if width > height * 1.15:
            return [0, 180, 90, 270]
    except Exception:
        pass
    return [0, 90, 180, 270]


def _ocr_image_variant(
    image_path: str,
    *,
    thumb_capture: bool,
    rotate_degrees: int,
    psm_modes: List[int],
    scoring_hint: str = "auto",
) -> Dict[str, Any]:
    import pytesseract

    image = _preprocess_image(
        image_path,
        thumb_capture=thumb_capture,
        rotate_degrees=rotate_degrees,
    )
    line_groups = []
    for psm in psm_modes:
        try:
            line_groups.append(_ocr_lines_from_image(image, psm))
        except Exception as exc:
            print(f"[OCR] PSM {psm} failed (rotate={rotate_degrees}): {exc}")
    lines = _merge_ocr_lines(line_groups)
    before = len(lines)
    lines = filter_whatsapp_ui_noise_from_ocr(lines)
    if before != len(lines):
        print(
            f"[OCR] Filtered WhatsApp UI noise: {before} → {len(lines)} line(s) "
            f"(rotate={rotate_degrees})"
        )
    for line in lines:
        line["text"] = _fix_burkert_ocr_in_text(line.get("text") or "")
    full_text = "\n".join(line["text"] for line in lines).strip()
    if scoring_hint == "quotation":
        score = _score_quotation_ocr_candidate(lines, full_text)
    elif scoring_hint == "burkert":
        score = _score_ocr_candidate(lines, full_text)
    else:
        score = max(
            _score_ocr_candidate(lines, full_text),
            _score_quotation_ocr_candidate(lines, full_text),
        )
    return {
        "lines": lines,
        "full_text": full_text,
        "score": score,
        "rotate_degrees": rotate_degrees,
    }


def extract_text_from_image(
    image_path: str,
    *,
    thumb_capture: bool = False,
    scoring_hint: str = "auto",
) -> Dict[str, Any]:
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

    try:
        from PIL import Image

        with Image.open(image_path) as probe:
            width, height = probe.size
            pixels = width * height
            if max(width, height) < 400 or pixels < 120_000:
                thumb_capture = True
    except Exception:
        pass

    tesseract_cmd = _resolve_tesseract_cmd()
    if not tesseract_cmd:
        payload["error"] = "tesseract_not_installed"
        return payload

    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        if thumb_capture:
            print("[OCR] Small capture detected — cropping chat chrome and upscaling for label OCR")
        psm_modes = [int(x) for x in os.getenv("OPENCLAW_OCR_PSM", "6,11,3").split(",") if x.strip()]

        best_variant = None
        all_line_groups: List[List[Dict[str, Any]]] = []
        for rotate_degrees in _orientation_candidates(image_path):
            variant = _ocr_image_variant(
                image_path,
                thumb_capture=thumb_capture,
                rotate_degrees=rotate_degrees,
                psm_modes=psm_modes,
                scoring_hint=scoring_hint,
            )
            all_line_groups.append(variant.get("lines") or [])
            if best_variant is None or variant["score"] > best_variant["score"]:
                best_variant = variant

        if best_variant and int(best_variant.get("rotate_degrees") or 0):
            print(
                f"[OCR] Best orientation: rotate {best_variant['rotate_degrees']}° "
                f"counter-clockwise (score={best_variant['score']:.1f})"
            )

        merged_lines = _merge_ocr_lines(all_line_groups)
        merged_full_text = "\n".join(
            str(line.get("text") or "").strip() for line in merged_lines if str(line.get("text") or "").strip()
        ).strip()

        lines = (best_variant or {}).get("lines") or []
        payload["lines"] = lines
        payload["full_text"] = (best_variant or {}).get("full_text") or ""
        payload["merged_full_text"] = merged_full_text or payload["full_text"]
        payload["rotate_degrees"] = int((best_variant or {}).get("rotate_degrees") or 0)
        if payload["full_text"]:
            print(
                f"[OCR] Merged {len(lines)} line(s) from best orientation; "
                f"{len(merged_lines)} line(s) across all rotations"
            )
        if not payload["full_text"]:
            payload["error"] = "no_text_detected"
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        print(f"[OCR] Failed on {image_path}: {exc}")
        return payload


def ocr_payload_to_json(ocr_payload: Dict[str, Any]) -> str:
    """Serialize OCR output for Copilot text prompts."""
    merged = str(ocr_payload.get("merged_full_text") or ocr_payload.get("full_text") or "")
    slim = {
        "engine": ocr_payload.get("engine"),
        "lang": ocr_payload.get("lang"),
        "full_text": ocr_payload.get("full_text") or "",
        "merged_full_text": merged,
        "rotate_degrees": ocr_payload.get("rotate_degrees"),
        "lines": [
            {"text": line.get("text"), "confidence": line.get("confidence")}
            for line in (ocr_payload.get("lines") or [])
        ],
        "error": ocr_payload.get("error"),
    }
    return json.dumps(slim, ensure_ascii=False, indent=2)


def ocr_text_for_extraction(ocr_payload: Dict[str, Any]) -> str:
    """Prefer merged OCR across all rotations (0/90/180/270° CCW) for part-ID scanning."""
    if not ocr_payload:
        return ""
    merged = str(ocr_payload.get("merged_full_text") or "").strip()
    if merged:
        return merged
    return str(ocr_payload.get("full_text") or "").strip()


def has_usable_ocr_text(ocr_payload: Dict[str, Any]) -> bool:
    text = str(ocr_payload.get("full_text") or "").strip()
    return bool(text) and not ocr_payload.get("error")
