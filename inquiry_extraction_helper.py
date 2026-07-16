import os
import re
import csv
import json
from typing import Dict, List, Optional


VERSION = "v1.09-PART-BEFORE-BRAND-QTY-FIX"

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"

KNOWN_BRANDS_RE = (
    r"OMRON|SMC|BURKERT|BÜRKERT|PANASONIC|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|PISCO|ABB|SIEMENS"
    r"|KOGANEI|CKD|AIRTAC|LEGRIS|MITSUBISHI|CPC|YASKAWA|DELTA|FUJI|IDEC"
)

KNOWN_BRAND_PREFIXES = tuple(
    brand.replace("BÜRKERT", "BURKERT")
    for brand in (
        "OMRON", "SMC", "BURKERT", "BÜRKERT", "PANASONIC", "THK", "LOCTITE", "KEYENCE", "FESTO",
        "SICK", "IFM", "PARKER", "PISCO", "ABB", "SIEMENS", "KOGANEI", "CKD", "AIRTAC", "LEGRIS",
        "MITSUBISHI", "CPC", "YASKAWA", "DELTA", "FUJI", "IDEC", "SCHNEIDER", "CAMOZZI", "PIAB",
    )
)

INVALID_PART_WORDS = {
    "THANK", "THANKS", "THANKYOU", "PLEASE", "REGARDS", "HELLO", "QUOTE", "PRICE",
    "CYLINDER", "SENSOR", "VALVE", "RELAY", "ITEM", "NEW", "UNIT", "UNITS", "PCS",
    "PC", "PCE", "PAS", "YOU", "YOUR", "ENQUIRY", "INQUIRY", "KINDLY", "DEAR", "REGARD",
    "BEST", "MORNING", "AFTERNOON", "FOLLOWING", "ATTACHED", "BELOW", "ABOVE",
    "MODEL", "BRAND", "TYPE", "PART", "NUMBER", "QTY", "QUANTITY", "FORWARDED",
    "VOD", "GERMANY", "MADE", "BURKERT", "BURKERTS",
}


def is_plausible_part_no(part_no: str) -> bool:
    """Reject salutations and prose mistaken for catalog part numbers."""
    part_no = str(part_no or "").strip().upper()
    norm = normalize_part(part_no)
    if len(norm) < 4:
        return False
    if norm in INVALID_PART_WORDS:
        return False
    if not re.search(r"\d", norm):
        return False
    return True


# ==================================================
# NORMALIZATION
# ==================================================

def normalize_part(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_qty_caption_text(text: str) -> str:
    """Normalize common OCR/typo quantity tokens before extraction."""
    value = str(text or "")
    value = re.sub(r"\b(\d+)\s*PAS\b", r"\1 PCS", value, flags=re.I)
    value = re.sub(r"\bPCE\b", "PCS", value, flags=re.I)
    value = re.sub(r"\bPIECES?\b", "PCS", value, flags=re.I)
    return value


def extract_qty_from_caption(text: str) -> Optional[int]:
    """Read a quantity hint from customer caption/text."""
    caption = normalize_qty_caption_text(text).upper()
    if not caption.strip():
        return None

    patterns = (
        r"\b(?:QUOTE(?:\s+ME)?|QTY|QUANTITY)\s*(?:FOR\s+)?(\d+)\b",
        r"\b(\d+)\s*(?:PCS?|PIECES?|UNITS?|NOS|SETS?|KE)\b",
        r"\b(\d+)\s*PAS\b",
    )
    for pattern in patterns:
        match = re.search(pattern, caption, flags=re.I)
        if match:
            qty = int(match.group(1))
            if qty > 0:
                return qty
    return None


def apply_caption_qty_to_items(items: List[dict], caption: str) -> List[dict]:
    """Override qty with caption hint when appropriate."""
    hint = extract_qty_from_caption(caption)
    if not hint or not items:
        return items

    if len(items) == 1:
        item = items[0]
        if item.get("quotation_meta"):
            item["qty"] = hint
            return items
        try:
            current = int(item.get("qty") or 1)
        except (TypeError, ValueError):
            current = 1
        if current == 1 and hint > 1:
            item["qty"] = hint
    return items


def looks_like_burkert_article_id(part_no: str) -> bool:
    """True when a token looks like a Burkert numeric article ID."""
    raw = str(part_no or "").strip().upper()
    if not raw:
        return False
    compact = re.sub(r"[^0-9A-Z]", "", raw)
    if re.fullmatch(r"0*\d{5,9}", compact):
        return True
    if re.fullmatch(r"0*\d{5,8}[S5]", compact):
        return True
    return False


def burkert_id_ocr_variants(value: str) -> List[str]:
    """Generate OCR correction variants for Burkert numeric article IDs."""
    raw = str(value or "").strip().upper()
    variants: List[str] = []
    seen = set()

    def add(candidate: str) -> None:
        candidate = str(candidate or "").strip().upper()
        if candidate and candidate not in seen:
            seen.add(candidate)
            variants.append(candidate)

    add(raw)
    compact = re.sub(r"[^0-9A-Z]", "", raw)
    add(compact)
    if re.search(r"[EIO]", compact) and re.search(r"\d", compact):
        corrected = (
            compact.replace("E", "2")
            .replace("I", "1")
            .replace("O", "0")
        )
        add(corrected)
        add(
            compact.replace("E", "4")
            .replace("I", "1")
            .replace("O", "0")
        )
    if re.search(r"\dS$", compact):
        add(compact[:-1] + "5")
        add(compact[:-1])
    if re.search(r"\d5$", compact) and len(compact) >= 6:
        add(compact[:-1] + "S")
    return variants


def normalize_burkert_part_from_ocr(part_no: str) -> str:
    """Fix common OCR misreads on numeric Burkert article IDs (e.g. e → 2, S → 5)."""
    raw = str(part_no or "").strip().upper()
    compact = re.sub(r"[^0-9A-Z]", "", raw)
    if re.fullmatch(r"0*[0-9EIO]{6,12}", compact):
        compact = (
            compact.replace("E", "2")
            .replace("I", "1")
            .replace("O", "0")
        )
        if raw.startswith("00") and len(compact) <= 8:
            return compact.zfill(8)
        return compact
    if re.fullmatch(r"0*\d{5,8}S", compact):
        return compact[:-1] + "5"
    return raw


def sanitize_whatsapp_outbound_text(text: str) -> str:
    """Strip non-BMP characters and normalize punctuation for ChromeDriver send_keys."""
    out: List[str] = []
    for ch in str(text or ""):
        code = ord(ch)
        if code > 0xFFFF:
            continue
        if ch in "\u2022\u2023\u25cf\u25aa":
            out.append("-")
        elif ch in "\u2013\u2014":
            out.append("-")
        elif ch in "\u2018\u2019\u2032":
            out.append("'")
        elif ch in "\u201c\u201d\u2033":
            out.append('"')
        elif ch == "\u00a0":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def scan_burkert_article_ids_from_ocr_text(text: str) -> List[str]:
    """Find likely Burkert numeric article IDs in noisy OCR output."""
    blob = str(text or "")
    if not blob.strip():
        return []

    candidates: List[str] = []
    seen = set()

    def add(value: str) -> None:
        value = str(value or "").strip()
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) < 6:
            return
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    try:
        from burkert_price_list import extract_burkert_id_from_text

        found = extract_burkert_id_from_text(blob)
        if found:
            add(found)
    except ImportError:
        pass

    patterns = (
        r"\b(00\d{6,7})\b",
        r"\b(0\d{7,8})\b",
        r"\b(\d{7,8})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, blob):
            add(match.group(1))

    compact = re.sub(r"[^0-9]", "", blob)
    for length in (8, 7, 6):
        for match in re.finditer(rf"(\d{{{length}}})", compact):
            add(match.group(1))

    return candidates


def is_ocr_junk_part_no(part_no: str) -> bool:
    """True when Copilot/OCR likely hallucinated a label fragment instead of a part ID."""
    raw = str(part_no or "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if not compact or len(compact) < 3:
        return True
    if compact in INVALID_PART_WORDS or raw in INVALID_PART_WORDS:
        return True
    if looks_like_burkert_article_id(raw):
        return False
    if is_plausible_part_no(raw):
        return False
    if re.fullmatch(r"[A-Z]{1,4}\d{0,2}", compact):
        return True
    if re.fullmatch(r"[A-Z]+\s*\d{1,3}", raw):
        return True
    if "VITA" in raw or raw.startswith("JP"):
        return True
    if len(compact) <= 5 and not re.search(r"\d{3,}", compact):
        return True
    if re.search(r"[|~*]", raw):
        return True
    return False


def is_copilot_prompt_example_hallucination(items: List[dict]) -> bool:
    """Detect when Copilot echoes the RFQ system-prompt example instead of real OCR."""
    if not items:
        return False
    parts = {str(item.get("part_no") or "").strip().upper() for item in items}
    example_pairs = (
        {"E2E-X5E1", "AS2201F-01-04SA"},
        {"AS2201F-01-04SA", "E2E-X5E1"},
    )
    if parts in example_pairs and len(parts) == 2:
        return True
    if len(items) == 2 and parts == {"E2E-X5E1", "AS2201F-01-04SA"}:
        return True
    return False


def filter_copilot_rfq_items(items: List[dict]) -> List[dict]:
    """Drop OCR-noise parts (PAS, VOD, caption tokens) before quoting."""
    if is_copilot_prompt_example_hallucination(items):
        print("[RFQ] Dropped Copilot prompt-example hallucination (E2E-X5E1 + AS2201F-01-04SA)")
        return []
    cleaned = []
    for item in items or []:
        part_no = str(item.get("part_no") or "").strip()
        if is_ocr_junk_part_no(part_no):
            print(f"[RFQ] Dropped junk extracted part {part_no!r}")
            continue
        cleaned.append(item)
    return cleaned


def reconcile_burkert_items_from_ocr(
    items: List[dict],
    image_path: str = "",
    caption: str = "",
) -> List[dict]:
    """Replace junk OCR/Copilot parts with numeric Burkert IDs read from the image."""
    image_path = str(image_path or "").strip()
    if not image_path or not os.path.isfile(image_path):
        return items

    try:
        from local_ocr import extract_text_from_image, has_usable_ocr_text
    except ImportError:
        return items

    payload = extract_text_from_image(image_path)
    if not has_usable_ocr_text(payload):
        return items

    ocr_text = payload.get("full_text") or ""
    ids = scan_burkert_article_ids_from_ocr_text(ocr_text)
    if not ids:
        return items

    preferred = ids[0]
    for candidate in ids:
        if candidate.startswith("00") and len(re.sub(r"[^0-9]", "", candidate)) >= 7:
            preferred = candidate
            break

    junk_items = items and all(is_ocr_junk_part_no(i.get("part_no", "")) for i in items)
    if not junk_items and len(items) == 1 and not looks_like_burkert_article_id(items[0].get("part_no", "")):
        junk_items = is_ocr_junk_part_no(items[0].get("part_no", ""))

    if not junk_items:
        return items

    qty = extract_qty_from_caption(caption) or 1
    if len(items) == 1:
        try:
            qty = int(items[0].get("qty") or qty)
        except (TypeError, ValueError):
            pass

    corrected = normalize_burkert_part_from_ocr(preferred)
    print(f"[RFQ] OCR Burkert ID recovery: {[i.get('part_no') for i in items]} → {corrected}")
    return [{"part_no": corrected, "qty": qty, "brand": "BURKERT"}]


def _canonical_brand(brand: str) -> str:
    return str(brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT") or "UNKNOWN"


def strip_leading_brand_from_part(brand: str, part_no: str) -> str:
    """Remove duplicated brand prefix from part number (SMC + SMC-AS2201F → AS2201F)."""
    brand_u = _canonical_brand(brand)
    part_u = str(part_no or "").strip().upper()
    if brand_u == "UNKNOWN" or not part_u:
        return part_u

    for sep in ("-", " ", "/"):
        prefix = f"{brand_u}{sep}"
        if part_u.startswith(prefix):
            stripped = part_u[len(prefix):].strip()
            if stripped and is_plausible_part_no(stripped):
                return stripped
    if part_u.startswith(brand_u) and len(part_u) > len(brand_u):
        # Rare glued form without separator — only when remainder looks like a part.
        remainder = part_u[len(brand_u):].lstrip("-/ ")
        if remainder and is_plausible_part_no(remainder):
            return remainder
    return part_u


def parse_brand_prefixed_part(token: str) -> tuple[str, str]:
    """
  Split customer tokens like SMC-AS2201F-01-04SA into (SMC, AS2201F-01-04SA).
  """
    token = str(token or "").strip().upper()
    token = re.sub(r"\s+", " ", token)
    if not token:
        return "UNKNOWN", ""

    for brand in sorted(set(KNOWN_BRAND_PREFIXES), key=len, reverse=True):
        for sep in ("-", " ", "/"):
            prefix = f"{brand}{sep}"
            if token.startswith(prefix):
                part = token[len(prefix):].strip().strip(":")
                if part and is_plausible_part_no(part):
                    return brand, part
    return "UNKNOWN", token


def normalize_inquiry_item(brand: str, part_no: str) -> tuple[str, str]:
    """
    Normalize brand + part from customer text.

    Handles BRAND-PARTNUMBER lines (e.g. SMC-AS2201F-01-04SA) and prevents
    duplicated descriptions like 'SMC SMC-AS2201F-01-04SA'.
    """
    brand_u = _canonical_brand(brand)
    part_u = str(part_no or "").strip().upper()
    part_u = re.sub(r"\s+", " ", part_u).strip(" :")

    parsed_brand, parsed_part = parse_brand_prefixed_part(part_u)
    if parsed_brand != "UNKNOWN":
        if brand_u in ("UNKNOWN", "", parsed_brand):
            brand_u = parsed_brand
        part_u = parsed_part
    elif brand_u != "UNKNOWN":
        part_u = strip_leading_brand_from_part(brand_u, part_u)

    return brand_u, part_u


def format_inquiry_description(brand: str, part_no: str) -> str:
    brand_u, part_u = normalize_inquiry_item(brand, part_no)
    if brand_u == "UNKNOWN":
        return f"UNKNOWN BRAND {part_u}"
    return f"{brand_u} {part_u}"


def clean_line(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def preprocess_body(text: str) -> str:
    """
    Fix flattened email body.

    Real email body may become:
    Hi ... Omron H3Y-4 ... Qty ; 2 Unit Omron MY4N ... Qty : 4 Unit Regards ...

    This converts brand boundaries back into item boundaries.
    """
    body = str(text or "")
    body = body.replace("\r", "\n")
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"&[a-z0-9#]+;", " ", body, flags=re.I)
    body = body.replace("–", " - ")
    body = body.replace("—", " - ")

    # Put newlines before known brands if they appear mid-sentence after qty/unit or general text.
    body = re.sub(
        r"\s+(OMRON|SMC|BURKERT|BÜRKERT|PANASONIC|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|PISCO|ABB|SIEMENS)\s+",
        r"\n\1 ",
        body,
        flags=re.I
    )

    # Strip email closings so "2pcs Thank you" is not parsed as qty + part THANK.
    body = re.sub(r"\bTHANK\s+YOU\b.*$", "", body, flags=re.I | re.S)
    body = re.sub(r"\bBEST\s+REGARDS\b.*$", "", body, flags=re.I | re.S)

    # Put newlines before common sign-off words to stop regex from eating signature.
    body = re.sub(
        r"\s+(REGARDS|THANKS|BEST REGARDS|THANK YOU)\s*,?",
        r"\n\1 ",
        body,
        flags=re.I
    )

    # Normalize quantity separators.
    body = re.sub(r"\bQTY\s*[;：]\s*", "Qty : ", body, flags=re.I)
    body = re.sub(r"\bQUANTITY\s*[;：]\s*", "Quantity : ", body, flags=re.I)

    # Keep useful newlines, clean horizontal spaces only.
    body = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines())
    body = re.sub(r"\n{2,}", "\n", body)

    return body.strip()


def normalize_voltage_text(text: str) -> str:
    text = str(text or "").upper()
    text = text.replace("V AC", "VAC")
    text = text.replace("VDC", "VDC")
    text = text.replace("V DC", "VDC")
    return text


def extract_voltage_family(text: str) -> Optional[str]:
    text = normalize_voltage_text(text)

    if re.search(
        r"\b(?:100|110|120)\s*VAC\b|"
        r"\bAC\s*(?:100|110|120)\b|"
        r"\bAC100[/-]120\b|"
        r"\bAC100[/-]110\b|"
        r"\bAC110[/-]120\b",
        text
    ):
        return "AC100_120"

    if re.search(
        r"\b(?:200|220|230|240)\s*VAC\b|"
        r"\bAC\s*(?:200|220|230|240)\b|"
        r"\bAC200[/-]220\b|"
        r"\bAC220[/-]240\b|"
        r"\bAC200[/-]240\b",
        text
    ):
        return "AC200_240"

    if re.search(r"\b24\s*VDC\b|\bDC\s*24\b|\bDC24\b", text):
        return "DC24"

    if re.search(r"\b12\s*VDC\b|\bDC\s*12\b|\bDC12\b", text):
        return "DC12"

    return None


def voltage_family_matches(request_family: Optional[str], stock_text: str) -> bool:
    if not request_family:
        return True

    stock_family = extract_voltage_family(stock_text)

    if stock_family and stock_family != request_family:
        return False

    return True


def extract_timer_seconds(text: str) -> Optional[str]:
    text = str(text or "").upper()
    m = re.search(r"\b(\d+)\s*(?:SEC|SECS|SECOND|SECONDS|S)\b", text)
    if m:
        return f"{m.group(1)}S"
    return None


def part_aliases(part_no: str) -> List[str]:
    part_no = clean_line(part_no).upper()
    aliases = [part_no]

    norm = normalize_part(part_no)

    # Example: H3Y-4-C requested, warehouse has H3Y-4.
    if part_no.endswith("-C") and len(norm) >= 5:
        aliases.append(part_no[:-2])

    cleaned = []
    seen = set()

    for a in aliases:
        a = clean_line(a).upper()
        if a and a not in seen:
            seen.add(a)
            cleaned.append(a)

    return cleaned


def stock_contains_part_family(stock_text: str, part_no: str) -> bool:
    stock_norm = normalize_part(stock_text)

    for alias in part_aliases(part_no):
        alias_norm = normalize_part(alias)

        if not alias_norm:
            continue

        if alias_norm in stock_norm:
            return True

    part_norm = normalize_part(part_no)

    family = re.match(r"([A-Z]+[0-9]+[A-Z]*)", part_norm)
    if family:
        fam = family.group(1)

        if len(fam) >= 4 and fam in stock_norm:
            if part_norm.startswith("MY4"):
                return "MY4" in stock_norm and ("GS" in stock_norm if "GS" in part_norm else True)
            return True

    return False


def is_dangerous_candidate(candidate: str, part_no: str) -> bool:
    cand_norm = normalize_part(candidate)

    if not cand_norm:
        return True

    dangerous = {
        "AC100120", "AC100110", "AC110120", "AC200240", "AC220240",
        "110VAC", "230VAC", "230V", "24VDC", "DC24",
        "TIMER", "RELAY", "SENSOR", "VALVE", "PLC", "UNIT", "10S", "5S", "2M"
    }

    if cand_norm in dangerous:
        return True

    return not stock_contains_part_family(candidate, part_no)


# ==================================================
# WAREHOUSE LOADING
# ==================================================

_WAREHOUSE_CACHE = None


def load_warehouse_rows() -> List[Dict[str, str]]:
    global _WAREHOUSE_CACHE

    if _WAREHOUSE_CACHE is not None:
        return _WAREHOUSE_CACHE

    rows = []

    if not os.path.exists(WAREHOUSE_CSV):
        print(f"❌ [EXTRACTOR] Warehouse CSV not found: {WAREHOUSE_CSV}")
        _WAREHOUSE_CACHE = []
        return []

    with open(WAREHOUSE_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)

        for row in reader:
            if len(row) < 5:
                continue

            rows.append({
                "stock_id": row[1].strip(),
                "stock_name": row[2].strip(),
                "model_no": row[4].strip(),
                "brand": row[6].strip() if len(row) > 6 else "",
                "stock_qty": float(row[10]) if len(row) > 10 and str(row[10]).strip() not in ["", None] else 0.0,
                "raw": ",".join(row)
            })

    print(f"📦 [EXTRACTOR] Warehouse rows loaded: {len(rows)}")
    _WAREHOUSE_CACHE = rows
    return rows


# ==================================================
# EXTRACTION
# ==================================================

def extract_brand_from_text(text: str) -> str:
    text = str(text or "").upper()
    brands = [
        "OMRON", "SMC", "BURKERT", "BÜRKERT", "PANASONIC", "THK", "LOCTITE",
        "KEYENCE", "FESTO", "SICK", "IFM", "PARKER", "PISCO", "ABB", "SIEMENS"
    ]

    for brand in brands:
        if re.search(rf"\b{re.escape(brand)}\b", text):
            return brand.replace("BÜRKERT", "BURKERT")

    return "UNKNOWN"


def guess_part_no(search_text: str) -> str:
    text = clean_line(search_text).upper()

    stop_words = {
        "BRAND", "MODEL", "ITEM", "TYPE", "TIMER", "RELAY", "SENSOR",
        "PRESSURE", "VALVE", "PLC", "UNIT", "PRICE", "QUOTE", "URGENTLY",
        "SUPPLIER", "QTY", "QUANTITY"
    }

    # Prefer explicit part-like token containing both letters and digits.
    part_like = re.findall(r"\b(?=[A-Z0-9\-]*[A-Z])(?=[A-Z0-9\-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+\b", text)
    for token in reversed(part_like):
        if token.upper() not in stop_words:
            return token.upper()

    compact = re.findall(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,}\b", text)
    for token in reversed(compact):
        if token.upper() not in stop_words:
            return token.upper()

    text = re.sub(r"\b(BRAND|MODEL|TYPE|TIMER|RELAY|SENSOR|PRESSURE|VALVE|PLC|UNIT|PRICE|QUOTE|ITEM|URGENTLY|SUPPLIER)\b", " ", text)
    text = re.sub(r"\b\d+\s*(?:SEC|SECS|SECOND|SECONDS)\b", " ", text)
    text = re.sub(r"\b\d+\s*VAC\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    tokens = re.findall(r"\b[A-Z0-9][A-Z0-9\-]{2,}\b", text)

    for token in reversed(tokens):
        if token.upper() not in stop_words:
            return token.upper()

    return text


def build_candidates(part_no: str, search_text: str) -> List[str]:
    part_no = clean_line(part_no).upper()
    search_text = clean_line(search_text).upper()

    candidates = []

    voltage_family = extract_voltage_family(search_text)
    timer_seconds = extract_timer_seconds(search_text)

    voltage_tokens = []
    if voltage_family == "AC100_120":
        voltage_tokens = ["AC100/110", "AC100-120", "AC110/120"]
    elif voltage_family == "AC200_240":
        voltage_tokens = ["AC200/220", "AC220/240", "AC200-240", "AC220-240"]
    elif voltage_family:
        voltage_tokens = [voltage_family.replace("_", "")]

    for alias in part_aliases(part_no):
        if voltage_tokens and timer_seconds:
            for v in voltage_tokens:
                candidates.append(f"{alias} {v} {timer_seconds}")

        if voltage_tokens:
            for v in voltage_tokens:
                candidates.append(f"{alias} {v}")

        candidates.append(alias)

    cleaned = []
    seen = set()

    for c in candidates:
        c = clean_line(c).upper()
        if not c:
            continue
        if is_dangerous_candidate(c, part_no):
            continue
        if c in seen:
            continue
        seen.add(c)
        cleaned.append(c)

    return cleaned


def build_extracted_item(brand: str, raw_text: str, part_no: str, search_text: str, qty: int, source: str) -> Dict:
    raw_text = clean_line(raw_text)
    part_no = clean_line(part_no).upper()
    search_text = clean_line(search_text).upper()
    brand = str(brand or "UNKNOWN").upper()
    brand, part_no = normalize_inquiry_item(brand, part_no)

    candidates = build_candidates(part_no, search_text)

    return {
        "brand": brand,
        "raw_text": raw_text,
        "part_no": part_no,
        "search_text": search_text,
        "qty": int(qty),
        "source": source,
        "candidates": candidates
    }


def extract_items_from_segment(segment: str, qty: int, source: str) -> List[Dict]:
    items = []

    raw_item = clean_line(segment)
    brand = extract_brand_from_text(raw_item)

    item_without_brand = re.sub(rf"\b{brand}\b", "", raw_item, flags=re.I).strip() if brand != "UNKNOWN" else raw_item

    slash_match = re.search(r"\b([A-Z0-9][A-Z0-9\-]+)\s*/\s*([A-Z0-9][A-Z0-9\-]+)\b", item_without_brand, re.I)

    if slash_match:
        left = slash_match.group(1).upper()
        right = slash_match.group(2).upper()
        trailing = item_without_brand[slash_match.end():].strip()

        for part in [left, right]:
            search_text = f"{part} {trailing}".strip()
            items.append(build_extracted_item(
                brand=brand,
                raw_text=raw_item,
                part_no=part,
                search_text=search_text,
                qty=qty,
                source=f"{source}_SLASH_SPLIT"
            ))
    else:
        part_no = guess_part_no(item_without_brand)
        items.append(build_extracted_item(
            brand=brand,
            raw_text=raw_item,
            part_no=part_no,
            search_text=item_without_brand,
            qty=qty,
            source=source
        ))

    return items


def extract_clean_items_from_text(text: str) -> List[Dict]:
    print("")
    print("=" * 90)
    print(f"🧠 [EXTRACTOR] START CLEAN EXTRACTION - {VERSION}")
    print("=" * 90)

    body = preprocess_body(text)

    print("🧹 [EXTRACTOR] Preprocessed Body:")
    print(body)

    extracted = []

    # Email format: MXY12-150 / Brand : SMC / Qty : 2pcs
    part_brand_qty_pattern = re.compile(
        rf"\b((?=[A-Z0-9\-_/]*\d)[A-Z0-9][A-Z0-9\-_/]{{2,40}})\b\s+"
        rf"BRAND\s*:\s*({KNOWN_BRANDS_RE})\s+"
        r"(?:QTY|QUANTITY)\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS|SET)?",
        re.I | re.S,
    )

    for part_no, brand, qty in part_brand_qty_pattern.findall(body):
        part_no = clean_line(part_no).upper()
        if not is_plausible_part_no(part_no):
            continue
        extracted.append(build_extracted_item(
            brand=brand.upper().replace("BÜRKERT", "BURKERT"),
            raw_text=f"{part_no} Brand: {brand}",
            part_no=part_no,
            search_text=part_no,
            qty=int(qty),
            source="PART_BRAND_QTY",
        ))

    # Explicit format:
    # Brand: Pisco Model: VKMH12S-S618S2E-B04 Qty: 5 pcs
    # Pisco Model: VKMH12S-S618S2E-B04 Qty: 5 pcs
    brand_model_qty_pattern = re.compile(
        r"(?:BRAND\s*:\s*)?"
        r"\b(OMRON|SMC|BURKERT|BÜRKERT|PANASONIC|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|PISCO|ABB|SIEMENS)\b\s+"
        r"(?:ITEM\s*:\s*[^\n]{1,80}?\s+)?"
        r"MODEL\s*:\s*([A-Z0-9\-_/+.]{3,80})\s+"
        r"(?:QTY|QUANTITY)\s*[;:\-]?\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS|SET)?",
        re.I | re.S
    )

    for brand, part_no, qty in brand_model_qty_pattern.findall(body):
        brand = brand.upper().replace("BÜRKERT", "BURKERT")
        part_no = clean_line(part_no).upper()
        extracted.append(build_extracted_item(
            brand=brand,
            raw_text=f"{brand} {part_no}",
            part_no=part_no,
            search_text=part_no,
            qty=int(qty),
            source="BRAND_MODEL_QTY"
        ))

    # Compact format:
    # PANASONIC SENSOR PRESSURE DP-102 - 3pcs
    compact_dash_qty_pattern = re.compile(
        r"(?:^|\n)\s*(.*?)\s*-\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS|SET)\b",
        re.I
    )

    for raw_item, qty in compact_dash_qty_pattern.findall(body):
        raw_item = clean_line(raw_item)
        if not raw_item:
            continue
        if not re.search(r"\b(OMRON|SMC|BURKERT|BÜRKERT|PANASONIC|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|PISCO|ABB|SIEMENS)\b", raw_item, re.I):
            continue
        extracted.extend(extract_items_from_segment(
            segment=raw_item,
            qty=int(qty),
            source="COMPACT_DASH_QTY"
        ))

    # Numbered or line-based with dash:
    # 1. Omron H3Y... - Qty : 2 Unit
    numbered_pattern = re.compile(
        r"(?:^|\n)\s*(?:\d+\s*[\.\)]\s*)?(.*?)\s*-\s*QTY\s*[;:\-]?\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?",
        re.I
    )

    for raw_item, qty in numbered_pattern.findall(body):
        raw_item = clean_line(raw_item)

        if not raw_item:
            continue

        if re.search(r"\b(HI|KINDLY|QUOTE|PRICE|FOLLOWING|REGARDS|THANKS)\b", raw_item, re.I) and not extract_brand_from_text(raw_item) != "UNKNOWN":
            continue

        extracted.extend(extract_items_from_segment(
            segment=raw_item,
            qty=int(qty),
            source="LINE_DASH_QTY"
        ))

    # Flattened style without useful newline:
    # Omron H3Y-4... Qty : 2 Unit
    # Omron My4n-GS... Qty : 4 Unit
    brand_qty_pattern = re.compile(
        r"\b(OMRON|SMC|BURKERT|BÜRKERT|PANASONIC|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|PISCO|ABB|SIEMENS)\b\s+"
        r"(.{3,120}?)\s+QTY\s*[;:\-]?\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?",
        re.I
    )

    for brand, item_text, qty in brand_qty_pattern.findall(body):
        segment = f"{brand} {item_text}"
        extracted.extend(extract_items_from_segment(
            segment=segment,
            qty=int(qty),
            source="BRAND_QTY_FLAT"
        ))

    # P/N / Model / ID format.
    pn_pattern = re.compile(
        r"(?:P/N|PN|PART\s*NO\.?|MODEL|ID)\s*[:#]\s*([A-Z0-9\-_/+. ]{3,60}?)\s+"
        r"(?:QTY|QUANTITY)\s*[:;]\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?",
        re.I | re.S
    )

    for part_no, qty in pn_pattern.findall(body):
        part_no = clean_line(part_no).upper()
        qty_int = int(qty)

        # If explicit Brand/Model already captured this model, do not duplicate.
        already = any(normalize_part(x.get("part_no")) == normalize_part(part_no) and int(x.get("qty", 0)) == qty_int for x in extracted)
        if already:
            continue

        extracted.append(build_extracted_item(
            brand=extract_brand_from_text(body),
            raw_text=part_no,
            part_no=part_no,
            search_text=part_no,
            qty=qty_int,
            source="PN_QTY"
        ))

    # De-duplicate strongly by normalized part + qty.
    unique = []
    seen = set()

    for item in extracted:
        if not is_plausible_part_no(item.get("part_no")):
            continue
        if normalize_part(item.get("part_no")) in {"MODEL", "BRAND", "ITEM", "TYPE"}:
            continue
        key = (normalize_part(item["part_no"]), item["qty"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    for item in unique:
        print("")
        print(f"🔎 [EXTRACTOR] Raw: {item['raw_text']}")
        print(f"   Brand: {item['brand']}")
        print(f"   Part: {item['part_no']}")
        print(f"   Qty: {item['qty']}")
        print(f"   Source: {item['source']}")
        print(f"   Candidates: {item['candidates']}")

        match = find_best_warehouse_match(item)

        if match:
            item.update({
                "matched": True,
                "matched_stock_id": match["stock_id"],
                "matched_stock_name": match["stock_name"],
                "matched_model_no": match["model_no"],
                "match_score": match["score"],
                "matched_candidate": match["candidate"],
                "confidence": match["confidence"],
            })

            print("   ✅ Warehouse Match:")
            print(f"      Stock ID: {match['stock_id']}")
            print(f"      Stock Name: {match['stock_name']}")
            print(f"      Model No: {match['model_no']}")
            print(f"      Score: {match['score']}")
            print(f"      Confidence: {match['confidence']}")
            print(f"      Candidate: {match['candidate']}")
        else:
            item.update({
                "matched": False,
                "matched_stock_id": "",
                "matched_stock_name": "",
                "matched_model_no": "",
                "match_score": 0,
                "matched_candidate": "",
                "confidence": "NONE",
            })

            print("   ⚠️ No safe warehouse match found.")

    print("=" * 90)
    print("✅ [EXTRACTOR] END CLEAN EXTRACTION")
    print("=" * 90)

    return unique


# ==================================================
# MATCHING
# ==================================================

def startswith_part_boundary(stock_name: str, part_no: str) -> int:
    """
    Higher score when customer short model is a clean stock family prefix.
    Example:
    E3Z-T61 -> E3Z-T61 2M       strongest
    E3Z-T61 -> E3Z-T61- D 2M    strong
    E3Z-T61 -> E3Z-T61A 2M      weaker variant
    """
    stock_name_u = str(stock_name or "").upper().strip()
    part_u = str(part_no or "").upper().strip()

    for alias in part_aliases(part_u):
        if stock_name_u == alias:
            return 5000

        if stock_name_u.startswith(alias + " "):
            return 4500

        if stock_name_u.startswith(alias + "-"):
            return 4000

        if stock_name_u.startswith(alias):
            return 2000

    return 0


def find_best_warehouse_match(item: Dict) -> Optional[Dict]:
    rows = load_warehouse_rows()

    part_no = item["part_no"]
    search_text = item["search_text"]
    brand = item.get("brand", "UNKNOWN")
    requested_voltage = extract_voltage_family(search_text)
    requested_timer = extract_timer_seconds(search_text)

    best = None

    for candidate in item["candidates"]:
        if is_dangerous_candidate(candidate, part_no):
            continue

        candidate_norm = normalize_part(candidate)

        if len(candidate_norm) < 4:
            continue

        for row in rows:
            if not row.get("stock_id") or not row.get("stock_name"):
                continue

            stock_text = f"{row['stock_id']} {row['stock_name']} {row['model_no']} {row['brand']} {row['raw']}".upper()
            stock_norm = normalize_part(stock_text)
            stock_name = str(row.get("stock_name", "")).upper()

            if brand != "UNKNOWN":
                stock_brand = str(row.get("brand", "")).upper()
                if stock_brand and brand not in stock_brand and brand not in stock_text:
                    continue

            if not stock_contains_part_family(stock_text, part_no):
                continue

            if not voltage_family_matches(requested_voltage, stock_text):
                continue

            score = 0

            # Partial stock-family priority. This is critical for E3Z-T61 -> E3Z-T61 2M.
            score += startswith_part_boundary(stock_name, part_no)

            if candidate_norm in stock_norm:
                score += 1000 + len(candidate_norm)

            for alias in part_aliases(part_no):
                alias_norm = normalize_part(alias)
                if alias_norm in stock_norm:
                    score += 800 + len(alias_norm)

            stock_voltage = extract_voltage_family(stock_text)
            if requested_voltage and stock_voltage == requested_voltage:
                score += 250
            elif requested_voltage and not stock_voltage:
                score += 25

            if requested_timer and requested_timer in stock_norm:
                score += 250

            if "RELAY" in search_text and "RELAY" in stock_text:
                score += 150
            if "TIMER" in search_text and "TIMER" in stock_text:
                score += 150
            if "SENSOR" in search_text and "SENSOR" in stock_text:
                score += 150
            if "PRESSURE" in search_text and "PRESSURE" in stock_text:
                score += 150

            # Prefer rows with available quantity when the match is otherwise close.
            try:
                stock_qty = float(row.get("stock_qty") or 0)
            except Exception:
                stock_qty = 0.0

            if stock_qty > 0:
                score += min(int(stock_qty), 20) * 10

            if "RELAY" in search_text and "TEMPERATURE CONTROLLER" in stock_text:
                score -= 800
            if "TIMER" in search_text and "TEMPERATURE CONTROLLER" in stock_text:
                score -= 400

            if score <= 0:
                continue

            confidence = "LOW"
            if score >= 3000:
                confidence = "HIGH"
            elif score >= 1200:
                confidence = "MEDIUM"

            if best is None or score > best["score"]:
                best = {
                    "stock_id": row["stock_id"],
                    "stock_name": row["stock_name"],
                    "model_no": row["model_no"],
                    "score": score,
                    "candidate": candidate,
                    "confidence": confidence,
                }

    if best and best["score"] >= 500:
        return best

    return None

# ==================================================
# TEST
# ==================================================

if __name__ == "__main__":
    test_text = """
Good days, Please quote, 1.Valve Brand: Pisco Model: VKMH12S-S618S2E-B04 Qty: 5 pcs Should you need further assistances, please feel free to contact us. Thanks B,regards Ms. AK TAN Mobile : +6016 – 772 7063 T T Solution (M) Sdn Bhd
"""

    result = extract_clean_items_from_text(test_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
