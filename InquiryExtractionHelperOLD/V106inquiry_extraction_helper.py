import os
import re
import csv
import json
from typing import Dict, List, Optional


VERSION = "v1.04-INQUIRY-EXTRACTION-HELPER-FLAT-EMAIL-FIX"

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"


# ==================================================
# NORMALIZATION
# ==================================================

def normalize_part(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


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

    text = re.sub(r"\b(TIMER|RELAY|SENSOR|VALVE|PLC|UNIT|PRICE|QUOTE|ITEM)\b", " ", text)
    text = re.sub(r"\b\d+\s*(?:SEC|SECS|SECOND|SECONDS)\b", " ", text)
    text = re.sub(r"\b\d+\s*VAC\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    tokens = re.findall(r"\b[A-Z0-9][A-Z0-9\-]{2,}\b", text)

    if tokens:
        return tokens[0].upper()

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
        extracted.append(build_extracted_item(
            brand=extract_brand_from_text(body),
            raw_text=part_no,
            part_no=part_no,
            search_text=part_no,
            qty=int(qty),
            source="PN_QTY"
        ))

    # De-duplicate strongly by normalized part + qty.
    unique = []
    seen = set()

    for item in extracted:
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
            stock_text = f"{row['stock_id']} {row['stock_name']} {row['model_no']} {row['brand']} {row['raw']}".upper()
            stock_norm = normalize_part(stock_text)

            if brand != "UNKNOWN":
                stock_brand = str(row.get("brand", "")).upper()
                if stock_brand and brand not in stock_brand and brand not in stock_text:
                    continue

            if not stock_contains_part_family(stock_text, part_no):
                continue

            if not voltage_family_matches(requested_voltage, stock_text):
                continue

            score = 0

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

            if "RELAY" in search_text and "TEMPERATURE CONTROLLER" in stock_text:
                score -= 800
            if "TIMER" in search_text and "TEMPERATURE CONTROLLER" in stock_text:
                score -= 400

            if score <= 0:
                continue

            confidence = "LOW"
            if score >= 1800:
                confidence = "HIGH"
            elif score >= 900:
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
Hi, Kindly help quote the price as following item : Omron H3Y-4-C 110VAC 10sec timer – Qty ; 2 Unit Omron My4n-GS/My4N-GS-R 110Vac Relay – Qty : 4 Unit Regards, Annie Lee
"""

    result = extract_clean_items_from_text(test_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
