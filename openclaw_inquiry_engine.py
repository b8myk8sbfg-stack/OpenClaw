import os
import re
import csv
import requests
import urllib3
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from dotenv import load_dotenv

from burkert_price_list import lookup_burkert_quote, resolve_burkert_id, format_burkert_id_display
from smc_portal_lookup import lookup_smc_quote

urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv()

VERSION = "v1.12-SMC-PORTAL"

# Customer sell price = purchase cost / MARKUP_DIVISOR (0.72 → ~38.9% markup on cost, ~28% margin).
MARKUP_DIVISOR = float(os.getenv("OPENCLAW_MARKUP_DIVISOR", "0.72"))

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"

OBM_API_URL = os.getenv("OBM_API_URL", "").rstrip("/")
OBM_API_KEY = os.getenv("OBM_API_KEY")
OBM_API_SECRET = os.getenv("OBM_API_SECRET")
OBM_AUTH = HTTPBasicAuth(OBM_API_KEY, OBM_API_SECRET)

KNOWN_BRANDS = {
    "OMRON", "SMC", "BURKERT", "BÜRKERT", "LEGRIS", "PANASONIC", "PISCO",
    "THK", "LOCTITE", "KEYENCE", "FESTO", "SICK", "IFM", "PARKER", "ABB", "SIEMENS",
    "ALLEN BRADLEY", "NITTO KOHKI", "CKD", "KOGANEI", "AIRTAC", "YASKAWA"
}


def normalize_part(part):
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def catalog_part_base_key(value):
    """Strip a trailing voltage suffix so E5CC-RX2ASM-800 AC100-240 → E5CC-RX2ASM-800."""
    text = re.sub(r"\s+", " ", str(value or "").upper().strip())
    if not text:
        return ""
    match = re.match(r"^(.+?)\s+(?:AC|DC)(?:\d|/|\s)", text)
    if match:
        return normalize_part(match.group(1))
    return normalize_part(text)


def clean_text(value):
    value = str(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&[a-z0-9#]+;", " ", value, flags=re.I)
    value = value.replace("\r", "\n")
    return value


WAREHOUSE_ROWS = []
EXACT_LOOKUP = {}
WAREHOUSE_BRANDS = set()


def parse_float(value, default=0.0):
    try:
        return float(str(value or "").replace(",", ""))
    except Exception:
        return default


def load_warehouse_map():
    global WAREHOUSE_ROWS, EXACT_LOOKUP, WAREHOUSE_BRANDS

    print("📦 [ENGINE] Loading Warehouse Database (part number → OBM PID lookup)...")

    rows = []
    exact_lookup = {}
    brands = set()

    if not os.path.exists(WAREHOUSE_CSV):
        print(f"❌ [ENGINE] Warehouse CSV not found: {WAREHOUSE_CSV}")
        WAREHOUSE_ROWS = []
        EXACT_LOOKUP = {}
        WAREHOUSE_BRANDS = set()
        return [], {}

    with open(WAREHOUSE_CSV, mode="r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)

        for row in reader:
            if len(row) < 5:
                continue

            api_id = row[1].strip()
            stock_name = row[2].strip()
            model_no = row[4].strip()
            alt_model = row[5].strip() if len(row) > 5 else ""
            brand = row[6].strip().upper() if len(row) > 6 else ""
            stock_qty = parse_float(row[10]) if len(row) > 10 else 0.0

            if brand:
                brands.add(brand.replace("BÜRKERT", "BURKERT"))

            row_data = {
                "api_id": api_id,
                "stock_name": stock_name,
                "model_no": model_no,
                "alt_model": alt_model,
                "brand": brand.replace("BÜRKERT", "BURKERT"),
                "stock_qty": stock_qty,
                "raw": ",".join(row),
            }
            rows.append(row_data)

            for val in [api_id, stock_name, model_no, alt_model]:
                for norm in {normalize_part(val), catalog_part_base_key(val)}:
                    if norm and len(norm) >= 5 and norm not in exact_lookup:
                        exact_lookup[norm] = row_data

    WAREHOUSE_ROWS = rows
    EXACT_LOOKUP = exact_lookup
    WAREHOUSE_BRANDS = brands

    print(f"✅ [ENGINE] Loaded {len(rows)} warehouse rows.")
    print(f"✅ [ENGINE] Loaded {len(exact_lookup)} exact lookup keys.")

    return rows, exact_lookup


_WAREHOUSE_LOADED = False


def _ensure_warehouse_loaded() -> None:
    global _WAREHOUSE_LOADED
    if not _WAREHOUSE_LOADED:
        load_warehouse_map()
        _WAREHOUSE_LOADED = True


def part_aliases(part_no):
    part_no = str(part_no or "").upper().strip()
    aliases = [part_no]

    if part_no.endswith("-C") and len(normalize_part(part_no)) >= 5:
        aliases.append(part_no[:-2])

    cleaned = []
    seen = set()
    for alias in aliases:
        alias = alias.strip().upper()
        if alias and alias not in seen:
            seen.add(alias)
            cleaned.append(alias)
    return cleaned


def startswith_part_boundary(stock_name, part_no):
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


def stock_contains_part_family(stock_text, part_no):
    stock_norm = normalize_part(stock_text)

    for alias in part_aliases(part_no):
        alias_norm = normalize_part(alias)
        if alias_norm and alias_norm in stock_norm:
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


def warehouse_match_trusted(customer_part, match) -> bool:
    """True when a warehouse row clearly corresponds to the customer part number."""
    customer_norm = normalize_part(customer_part)
    if not customer_norm or not match:
        return False

    stock_blob = normalize_part(
        " ".join(
            str(match.get(key) or "")
            for key in ("stock_name", "model_no", "alt_model", "api_id")
        )
    )
    if not stock_blob:
        return False

    if customer_norm in stock_blob or stock_blob in customer_norm:
        return True

    # Allow cable-length suffixes such as E2E-X5E1 -> E2E-X5E1 2M.
    if stock_blob.startswith(customer_norm) and len(customer_norm) >= 5:
        return True

    return False


def resolve_warehouse_match(part_no, declared_brand="UNKNOWN", qty=1, source="", search_context=""):
    """Resolve warehouse stock for a part, with voltage-aware matching and variant prompts."""
    _ensure_warehouse_loaded()
    part_no = str(part_no or "").strip().upper()
    source = str(source or "").upper()
    requested_voltage = resolve_requested_voltage(part_no, search_context)

    if not requested_voltage:
        variants = collect_voltage_variants(part_no, declared_brand=declared_brand, qty=qty)
        if len(variants) > 1:
            print(
                f"   ⚠️ [ENGINE] Multiple voltage variants for {part_no} — "
                f"customer selection required ({len(variants)} options)"
            )
            return None, variants

    exact = EXACT_LOOKUP.get(normalize_part(part_no))
    if exact and requested_voltage:
        stock_voltage = extract_voltage_signature(
            f"{exact.get('stock_name') or ''} {exact.get('model_no') or ''}"
        )
        if stock_voltage and voltage_values_overlap(requested_voltage[1], stock_voltage[1]):
            return exact, None
        exact = None
    elif exact and not requested_voltage:
        variants = collect_voltage_variants(part_no, declared_brand=declared_brand, qty=qty)
        if len(variants) <= 1:
            return exact, None
        return None, variants

    if source in ("COPILOT_VISUAL", "COPILOT_UNIFIED", "COPILOT_LABEL_OCR"):
        partial = find_best_warehouse_match(
            part_no,
            declared_brand=declared_brand,
            qty=qty,
            search_context=search_context,
        )
        if partial and warehouse_match_trusted(part_no, partial):
            return partial, None
        if partial:
            print(
                f"   ⚠️ [ENGINE] Rejected warehouse remap for visual part {part_no} "
                f"→ {partial.get('stock_name') or partial.get('api_id')} (different product family)"
            )
        return None, None

    partial = find_best_warehouse_match(
        part_no,
        declared_brand=declared_brand,
        qty=qty,
        search_context=search_context,
    )
    return partial, None


def infer_brand_from_part(part_no):
    part_norm = normalize_part(part_no)

    # Safe family inference for common automation brands.
    if part_norm.startswith("E3Z") or part_norm.startswith("E39") or part_norm.startswith("E2E") or part_norm.startswith("MY2") or part_norm.startswith("MY4") or part_norm.startswith("H3Y") or part_norm.startswith("H3J") or part_norm.startswith("H3CR") or part_norm.startswith("E5CC") or part_norm.startswith("E5CN"):
        return "OMRON"

    if part_norm.startswith("150C") or part_norm.startswith("150-C"):
        return "ALLEN-BRADLEY"

    return "UNKNOWN"


PART_REF_PATTERN = re.compile(
    r"\b("
    r"[A-Z]{1,4}\d?[A-Z]{0,3}-[A-Z0-9][A-Z0-9\-]*"
    r"(?:\s+(?:AC|DC)[\d\-/A-Z]+)?"
    r"|[A-Z]{2,6}\d{3,}[A-Z0-9#\-/]*"
    r")\b",
    re.IGNORECASE,
)

SUCCESSOR_FAMILY_SEARCH = {
    "H3JA": ("H3CR-A8", "H3CR", "H3Y"),
    "H3J": ("H3CR-A8", "H3CR", "H3Y"),
    "H3A": ("H3CR-A8", "H3CR"),
}


def extract_part_references_from_text(message_text: str) -> list:
    """Pull likely catalogue part numbers from free-text customer messages."""
    text = str(message_text or "").upper()
    refs = []
    seen = set()
    for match in PART_REF_PATTERN.finditer(text):
        ref = re.sub(r"\s+", " ", match.group(1)).strip().upper()
        key = normalize_part(ref)
        if len(key) < 4 or key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def search_warehouse_stock_rows(part_no, brand="UNKNOWN", limit=8):
    """Return warehouse rows whose stock name/model matches the part family."""
    _ensure_warehouse_loaded()
    part_no = str(part_no or "").upper().strip()
    part_norm = normalize_part(part_no)
    if not part_norm:
        return []

    family_match = re.match(r"^([A-Z]+\d?[A-Z]*)", part_norm)
    family = family_match.group(1) if family_match else part_norm[:4]
    brand_u = str(brand or "UNKNOWN").upper().replace("BÜRKERT", "BURKERT")

    hits = []
    for row in WAREHOUSE_ROWS:
        row_brand = str(row.get("brand") or "").upper().replace("BÜRKERT", "BURKERT")
        stock_text = (
            f"{row.get('stock_name') or ''} {row.get('model_no') or ''} "
            f"{row.get('alt_model') or ''} {row.get('api_id') or ''}"
        ).upper()
        stock_norm = normalize_part(stock_text)

        if brand_u != "UNKNOWN" and row_brand and brand_u not in row_brand:
            continue

        matched = (
            part_norm in stock_norm
            or stock_norm.startswith(part_norm)
            or part_norm.startswith(stock_norm[: max(len(part_norm), 5)])
            or (len(family) >= 3 and family in stock_norm)
        )
        if not matched:
            continue

        score = 0
        if part_norm in stock_norm:
            score += 2000
        if family and family in stock_norm:
            score += 500
        score += float(row.get("stock_qty") or 0)
        hits.append({**row, "_score": score})

    hits.sort(key=lambda item: item["_score"], reverse=True)
    return hits[:limit]


def parse_qty_from_caption(text: str, default: int = 1) -> int:
    """Parse requested quantity from a customer caption; default to 1 pc when absent."""
    text_u = str(text or "").upper()
    patterns = (
        r"\b(?:QTY|QUANTITY)\s*[:;]?\s*(\d{1,4})\b",
        r"\b(\d{1,4})\s*(?:PCS|PC|PCE|PIECES|PIECE|UNIT|UNITS|EA|EACH|KE|BUAH)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text_u)
        if match:
            qty = int(match.group(1))
            if qty > 0:
                return qty
    try:
        qty = int(default or 1)
    except (TypeError, ValueError):
        qty = 1
    return max(1, qty)


def build_warehouse_support_context(message_text: str, part_refs=None, max_lines: int = 4) -> tuple:
    """Build warehouse stock context for technical support, prioritising in-stock SKUs.

    Ex-Stock labels are based on live OBM API STORE quantity, not CSV stock_qty.
    """
    parts = part_refs or extract_part_references_from_text(message_text)
    text_u = str(message_text or "").upper()
    search_terms = list(parts)

    if any(
        word in text_u
        for word in ("EQUIVALENT", "REPLACEMENT", "SUBSTITUTE", "ALTERNATIVE", "SUCCESSOR", "REPLACE")
    ):
        for part in parts:
            part_norm = normalize_part(part)
            for prefix, successors in SUCCESSOR_FAMILY_SEARCH.items():
                if part_norm.startswith(prefix):
                    for successor in successors:
                        if successor not in search_terms:
                            search_terms.append(successor)

    seen_keys = set()
    lines = []
    for term in search_terms:
        brand = infer_brand_from_part(term)
        for row in search_warehouse_stock_rows(term, brand=brand, limit=6):
            key = str(row.get("api_id") or row.get("stock_name") or "").strip()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            stock_label = str(row.get("stock_name") or key).strip()
            brand_label = str(row.get("brand") or brand or "").strip()
            store_qty = get_usable_store_qty(row.get("api_id"), row)
            if store_qty > 0:
                lines.append(f"- {brand_label} {stock_label} — Ex-Stock")
            else:
                lines.append(f"- {brand_label} {stock_label} — available to source")
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break

    return parts, "\n".join(lines)


def extract_voltage_signature(text):
    """Return (AC/DC, voltage values) from text like AC100-240, AC-100-240, DC24."""
    value = re.sub(r"\s+", "", str(text or "").upper())

    range_match = re.search(
        r"(AC|DC)[\-/]?(\d{2,4})[\-/](\d{2,4})",
        value,
    )
    if range_match:
        current_type = range_match.group(1)
        return current_type, (
            int(range_match.group(2)),
            int(range_match.group(3)),
        )

    patterns = (
        ("DC", r"DC(\d+(?:/\d+)*)"),
        ("DC", r"(\d+(?:/\d+)*)VDC"),
        ("AC", r"AC(\d+(?:/\d+)*)"),
        ("AC", r"(\d+(?:/\d+)*)VAC"),
    )
    for current_type, pattern in patterns:
        match = re.search(pattern, value.replace("-", ""))
        if match:
            return current_type, tuple(int(part) for part in match.group(1).split("/"))
    return None


def voltage_values_overlap(req_vals, stock_vals):
    """True when requested and warehouse voltage ranges share a value or overlap."""
    if not req_vals or not stock_vals:
        return False
    if set(req_vals) & set(stock_vals):
        return True
    return min(req_vals) <= max(stock_vals) and min(stock_vals) <= max(req_vals)


def resolve_requested_voltage(part_no, search_context=""):
    """Read voltage from part number and/or customer transcript, caption, or Copilot specs."""
    chunks = [str(part_no or ""), str(search_context or "")]
    for chunk in chunks:
        signature = extract_voltage_signature(chunk)
        if signature:
            return signature
    return None


def _format_voltage_label(row):
    stock_text = " ".join(
        str(row.get(key) or "")
        for key in ("stock_name", "model_no", "alt_model")
    )
    signature = extract_voltage_signature(stock_text)
    if signature:
        current_type, values = signature
        if len(values) >= 2:
            return f"{current_type}{values[0]}-{values[1]}"
        return f"{current_type}{values[0]}"
    return str(row.get("stock_name") or row.get("model_no") or row.get("api_id") or "").strip()


def get_usable_store_qty(api_id, warehouse_row=None):
    """Return live STORE quantity from OBM API only.

    The warehouse CSV is a part-number → product ID (PID) lookup catalog.
    It must not be used as a source of stock quantity for customer replies.
    """
    api_id = str(api_id or "").strip()
    if not api_id:
        return 0.0

    obm = get_product(api_id)
    if str(obm.get("error") or "") == "101":
        print(f"   ⚠️ OBM API rejected PID {api_id!r} — no Ex-Stock (CSV is lookup-only)")
        return 0.0

    store_qty = get_store_qty_from_product(obm)
    if store_qty > 0:
        print(f"   📦 OBM STORE qty for {api_id!r}: {store_qty}")
    return store_qty


def _default_cable_variant_bonus(part_no, stock_name):
    """When the customer omits cable length, prefer common 2M pre-wired variants."""
    part_u = str(part_no or "").upper()
    stock_u = str(stock_name or "").upper()
    if re.search(r"\d\s*M\b", part_u):
        return 0
    if part_u.startswith("E2E") and re.search(r"\b2M\b", stock_u):
        return 2500
    return 0


def _score_warehouse_candidate(row, part_no, qty, declared_brand, requested_voltage, candidate_voltages):
    row_brand = str(row.get("brand") or "").upper()
    stock_text = (
        f"{row['api_id']} {row['stock_name']} {row['model_no']} "
        f"{row['alt_model']} {row['brand']} {row['raw']}"
    ).upper()

    if declared_brand != "UNKNOWN" and row_brand and declared_brand not in row_brand and declared_brand not in stock_text:
        return None

    if not stock_contains_part_family(stock_text, part_no):
        return None

    stock_voltage = extract_voltage_signature(stock_text)
    if requested_voltage:
        if not stock_voltage or stock_voltage[0] != requested_voltage[0]:
            return None
        if not voltage_values_overlap(requested_voltage[1], stock_voltage[1]):
            return None

    score = 0
    score += startswith_part_boundary(row.get("stock_name"), part_no)

    part_norm = normalize_part(part_no)
    if part_norm in normalize_part(stock_text):
        score += 1000 + len(part_norm)

    if requested_voltage and stock_voltage == requested_voltage:
        score += 5000

    score += _default_cable_variant_bonus(part_no, row.get("stock_name"))

    if part_norm.startswith("E3Z") and "PHOTOELECTRIC SENSOR" in stock_text:
        score += 200
    if part_norm.startswith("E39") and "RETROREFLECTOR" in stock_text:
        score += 200

    if score <= 0:
        return None

    if stock_voltage:
        candidate_voltages.add(stock_voltage)

    return {**row, "score": score, "match_type": "PARTIAL_STOCK_FAMILY"}


def _pick_best_warehouse_candidate(candidates, part_no, qty):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    ranked = []
    for row in candidates:
        usable = get_usable_store_qty(row.get("api_id"), row)
        ranked.append({
            "row": row,
            "usable": usable,
            "score": row.get("score") or 0,
            "can_quote_now": usable >= qty,
        })

    ranked.sort(
        key=lambda item: (
            1 if item["can_quote_now"] else 0,
            item["usable"],
            item["score"],
        ),
        reverse=True,
    )

    best = ranked[0]["row"]
    if len(ranked) > 1:
        print(
            f"   📦 [ENGINE] Chose in-stock variant for {part_no}: "
            f"{best.get('stock_name') or best.get('api_id')} "
            f"(usable STORE qty={ranked[0]['usable']:.0f})"
        )
        for alt in ranked[1:4]:
            alt_row = alt["row"]
            print(
                f"      alt: {alt_row.get('stock_name') or alt_row.get('api_id')} "
                f"| usable={alt['usable']:.0f} | score={alt['score']}"
            )
    return best


def collect_voltage_variants(part_no, declared_brand="UNKNOWN", qty=1):
    """List distinct warehouse SKUs for the same catalog part with different voltages."""
    _ensure_warehouse_loaded()
    part_no = str(part_no or "").strip().upper()
    declared_brand = str(declared_brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")
    if declared_brand == "UNKNOWN":
        declared_brand = infer_brand_from_part(part_no)

    candidate_voltages = set()
    candidates = []
    for row in WAREHOUSE_ROWS:
        if not row.get("api_id") or not row.get("stock_name"):
            continue
        scored = _score_warehouse_candidate(
            row, part_no, qty, declared_brand, None, candidate_voltages
        )
        if scored and startswith_part_boundary(row.get("stock_name"), part_no) >= 4000:
            candidates.append(scored)

    if not candidates:
        return []

    seen = set()
    variants = []
    for row in sorted(candidates, key=lambda item: item.get("score") or 0, reverse=True):
        stock_name = str(row.get("stock_name") or "").strip().upper()
        if not stock_name or stock_name in seen:
            continue
        seen.add(stock_name)
        variants.append({
            "api_id": row.get("api_id"),
            "stock_name": row.get("stock_name"),
            "model_no": row.get("model_no"),
            "voltage_label": _format_voltage_label(row),
        })
    return variants


def find_best_warehouse_match(part_no, declared_brand="UNKNOWN", qty=1, search_context=""):
    _ensure_warehouse_loaded()
    part_no = str(part_no or "").strip().upper()
    declared_brand = str(declared_brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")

    norm = normalize_part(part_no)
    if not norm:
        return None

    requested_voltage = resolve_requested_voltage(part_no, search_context)
    exact = EXACT_LOOKUP.get(norm)
    if exact:
        if requested_voltage:
            stock_voltage = extract_voltage_signature(
                f"{exact.get('stock_name') or ''} {exact.get('model_no') or ''}"
            )
            if stock_voltage and voltage_values_overlap(requested_voltage[1], stock_voltage[1]):
                print(f"   ✅ [ENGINE] Exact lookup match: {part_no} → {exact['api_id']}")
                return exact
        elif len(collect_voltage_variants(part_no, declared_brand=declared_brand, qty=qty)) <= 1:
            print(f"   ✅ [ENGINE] Exact lookup match: {part_no} → {exact['api_id']}")
            return exact

    if declared_brand == "UNKNOWN":
        declared_brand = infer_brand_from_part(part_no)

    candidate_voltages = set()
    candidates = []

    for row in WAREHOUSE_ROWS:
        if not row.get("api_id") or not row.get("stock_name"):
            continue
        scored = _score_warehouse_candidate(
            row, part_no, qty, declared_brand, requested_voltage, candidate_voltages
        )
        if scored:
            candidates.append(scored)

    if not candidates:
        print(f"   ⚠️ [ENGINE] No warehouse match: {part_no}")
        return None

    if not requested_voltage and len(candidate_voltages) > 1:
        print(
            f"   ⚠️ [ENGINE] Ambiguous voltage variants for {part_no}: "
            f"{sorted(candidate_voltages)}. Customer must choose."
        )
        return None

    best = _pick_best_warehouse_candidate(candidates, part_no, qty)
    if best:
        obm_store = get_usable_store_qty(best.get("api_id"), best)
        print(
            f"   ✅ [ENGINE] Partial stock-family match: {part_no} → {best['api_id']} | "
            f"{best['stock_name']} | OBM STORE qty: {obm_store} | Score: {best.get('score')}"
        )
    else:
        print(f"   ⚠️ [ENGINE] No warehouse match: {part_no}")
    return best


def extract_structured_rfq_items(body_text):
    rfq_items = []
    body_upper = clean_text(body_text).upper()
    existing = set()

    def add_item(brand, part_no, qty, source, desc=None):
        part_no = str(part_no or "").strip().upper()
        brand = str(brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")
        qty = int(qty or 1)
        norm = normalize_part(part_no)

        if not norm or norm in existing or len(norm) < 4:
            return

        existing.add(norm)
        rfq_items.append({
            "brand": brand,
            "part_no": part_no,
            "desc": desc or (f"{brand} {part_no}" if brand != "UNKNOWN" else part_no),
            "qty": qty,
            "norm": norm,
            "source": source,
        })

    # Brand : THK / Item / Model / Quantity format.
    pattern_brand_item_model = re.compile(
        r"BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+"
        r"ITEM\s*:\s*([A-Z0-9\s\-\(\)\+\/]+?)\s+"
        r"MODEL\s*:\s*([A-Z0-9\-\+\._/ ]+?)\s+"
        r"QUANTITY\s*:\s*(\d+)",
        re.I | re.S,
    )
    for brand, item_name, model, qty in pattern_brand_item_model.findall(body_upper):
        brand = brand.strip().upper()
        item_name = re.sub(r"\s+", " ", item_name.strip().upper())
        model = re.sub(r"\s+", " ", model.strip().upper())
        add_item(brand, model, qty, "BRAND_ITEM_MODEL_QTY", f"{brand} {model} ({item_name})")

    # Brand: Pisco Model: VK... Qty: 5
    pattern_brand_model_qty = re.compile(
        r"BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+"
        r"MODEL\s*:\s*([A-Z0-9\-\+\._/ ]+?)\s+"
        r"(?:QTY|QUANTITY)\s*:\s*(\d+)",
        re.I | re.S,
    )
    for brand, model, qty in pattern_brand_model_qty.findall(body_upper):
        add_item(brand, model, qty, "BRAND_MODEL_QTY")

    # Brand : SMC / Part No. / Quantity format.
    pattern_brand_part_qty = re.compile(
        r"BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+"
        r"PART\s*NO\.?\s*:\s*([A-Z0-9\-_/ ]+?)\s+"
        r"QUANTITY\s*:\s*(\d+)",
        re.I | re.S,
    )
    for brand, part_no, qty in pattern_brand_part_qty.findall(body_upper):
        add_item(brand, part_no, qty, "BRAND_PART_QTY")

    # Model: CJ2M-CPU32 / Qty: 1
    brand_context = "UNKNOWN"
    brand_context_match = re.search(
        r"\bFROM\s+(OMRON|SMC|BURKERT|BÜRKERT|THK|LOCTITE|PISCO|PANASONIC|KEYENCE|FESTO|SICK|IFM|PARKER|ABB|SIEMENS)\b",
        body_upper,
        re.I,
    )
    if brand_context_match:
        brand_context = brand_context_match.group(1).upper().replace("BÜRKERT", "BURKERT")

    model_qty_pattern = re.compile(
        r"(?:MODEL|PART\s*NO\.?|PART|ID)\s*:\s*([A-Z0-9\-_/ \+\.]{3,50}?)\s+"
        r"(?:QTY|QUANTITY)\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS|SET)?",
        re.I | re.S,
    )
    for part_no, qty in model_qty_pattern.findall(body_upper):
        add_item(brand_context, part_no, qty, "EXPLICIT_MODEL_QTY")

    # WhatsApp / simple format: E3Z-T61 Qty:1
    line_qty_pattern = re.compile(
        r"^\s*([A-Z0-9][A-Z0-9\-_/ \+\.]{2,40}?)\s+QTY\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS|SET)?\s*$",
        re.I | re.M,
    )
    for part_no, qty in line_qty_pattern.findall(body_upper):
        add_item("UNKNOWN", part_no, qty, "LINE_QTY_FORMAT")

    # Voice/WhatsApp spoken order: "2 pcs 178902" / "2 ke 178902"
    qty_before_part_pattern = re.compile(
        r"\b(\d{1,4})\s*(?:PCS|PC|PCE|PIECES|PIECE|UNIT|UNITS|EA|EACH|KE|BUAH)\b[\s,.\-]*"
        r"([A-Z0-9][A-Z0-9\-_/]{2,30})\b",
        re.I,
    )
    for qty, part_no in qty_before_part_pattern.findall(body_upper):
        add_item("UNKNOWN", part_no, qty, "QTY_BEFORE_PART")

    # Spoken order: "178902 2 pcs"
    part_before_qty_pattern = re.compile(
        r"\b([A-Z0-9][A-Z0-9\-_/]{2,30})\b[\s,.\-]*"
        r"(\d{1,4})\s*(?:PCS|PC|PCE|PIECES|PIECE|UNIT|UNITS|EA|EACH|KE|BUAH)\b",
        re.I,
    )
    for part_no, qty in part_before_qty_pattern.findall(body_upper):
        add_item("UNKNOWN", part_no, qty, "PART_BEFORE_QTY")


    # WhatsApp simple single-part inquiry with no Qty:
    # Example: G3NA-210B DC5-24
    # Default Qty = 1.
    # Conservative rule:
    # - only used when no structured items found
    # - must contain letters and numbers
    # - must not look like a normal sentence/signature
    if not rfq_items:
        simple_text = body_upper.strip()
        simple_text = re.sub(r"\s+", " ", simple_text)

        blocked_words = [
            "HI", "HELLO", "THANKS", "REGARDS", "QUOTE", "PRICE", "PLEASE",
            "GOOD DAY", "MORNING", "AFTERNOON", "EVENING"
        ]

        has_blocked = any(w in simple_text for w in blocked_words)
        looks_part = bool(re.fullmatch(r"(?=.*[A-Z])(?=.*\d)[A-Z0-9][A-Z0-9\-_/ ]{3,40}", simple_text))

        if looks_part and not has_blocked:
            part_no = simple_text.strip().upper()
            add_item("UNKNOWN", part_no, 1, "STANDALONE_PART_DEFAULT_QTY")
            print(f"🧩 [ENGINE] Standalone WhatsApp part detected, default Qty=1: {part_no}")

    return rfq_items


def get_product(api_id):
    try:
        return requests.get(
            f"{OBM_API_URL}/GetProduct",
            auth=OBM_AUTH,
            params={"pid": api_id},
            verify=False,
        ).json()
    except Exception as e:
        print(f"❌ [ENGINE] GetProduct failed for {api_id}: {e}")
        return {}


def get_purchase_price(api_id):
    try:
        return requests.get(
            f"{OBM_API_URL}/GetPurProductPrice",
            auth=OBM_AUTH,
            params={"pid": api_id},
            verify=False,
        ).json()
    except Exception as e:
        print(f"❌ [ENGINE] GetPurProductPrice failed for {api_id}: {e}")
        return {}


def _try_burkert_price_list_row(
    part_no,
    qty,
    desc=None,
    brand="",
    search_context="",
    burkert_id="",
    technical_specs=None,
):
    """Fill price and lead time from the Burkert offline price list when available."""
    brand_u = str(brand or "").upper().replace("BÜRKERT", "BURKERT")
    if brand_u != "BURKERT":
        return None

    resolved_id = resolve_burkert_id(
        burkert_id=burkert_id,
        technical_specs=technical_specs,
        search_context=search_context,
    )

    quote = lookup_burkert_quote(
        part_no,
        qty=qty,
        markup_divisor=MARKUP_DIVISOR,
        search_context=search_context,
        burkert_id=resolved_id,
        technical_specs=technical_specs,
    )
    if not quote:
        return None

    label = resolved_id or part_no
    moq_note = ""
    if quote.get("moq_applied"):
        moq_note = f" | MOQ applied ({quote.get('requested_qty')} → {quote.get('qty')})"
    elif int(quote.get("moq") or 0) > 1:
        moq_note = f" | MOQ {quote.get('moq')}"
    print(
        f"   ✅ [BURKERT] Price list match for {label}: "
        f"RM {quote.get('price')} | LT {quote.get('lt')}{moq_note}"
    )
    return {
        "desc": quote.get("desc") or desc or part_no,
        "qty": int(quote.get("qty") or qty),
        "requested_qty": int(quote.get("requested_qty") or qty),
        "moq": int(quote.get("moq") or 0),
        "moq_applied": bool(quote.get("moq_applied")),
        "price": quote.get("price", "[TBC]"),
        "lt": quote.get("lt", "[TBC]"),
        "pid": quote.get("burkert_id") or part_no,
        "burkert_id": quote.get("burkert_id_display") or format_burkert_id_display(resolved_id),
        "brand": "BURKERT",
        "source": quote.get("source", "BURKERT_PRICE_LIST"),
        "customer_part": part_no,
        "needs_supplier": quote.get("price") == "[TBC]" or quote.get("lt") == "[TBC]",
    }


def _try_smc_portal_row(
    part_no,
    qty,
    desc=None,
    brand="",
    search_context="",
):
    """Fill price and lead time from the SMC distributor web portal when available."""
    brand_u = str(brand or "").upper().replace("BÜRKERT", "BURKERT")
    if brand_u != "SMC":
        return None

    quote = lookup_smc_quote(
        part_no,
        qty=qty,
        markup_divisor=MARKUP_DIVISOR,
        search_context=search_context,
    )
    if not quote:
        return None

    return {
        "desc": quote.get("desc") or desc or part_no,
        "qty": int(quote.get("qty") or qty),
        "requested_qty": int(quote.get("requested_qty") or qty),
        "moq": int(quote.get("moq") or 0),
        "moq_applied": bool(quote.get("moq_applied")),
        "price": quote.get("price", "[TBC]"),
        "lt": quote.get("lt", "[TBC]"),
        "pid": quote.get("smc_part") or part_no,
        "smc_part": quote.get("smc_part") or part_no,
        "brand": "SMC",
        "source": quote.get("source", "SMC_PORTAL"),
        "customer_part": part_no,
        "needs_supplier": quote.get("price") == "[TBC]" or quote.get("lt") == "[TBC]",
    }


def _merge_smc_quote_into_row(row, part_no):
    """Update an existing quote row from the SMC portal when price/LT are still TBC."""
    if str(row.get("brand") or "").upper() != "SMC":
        return row

    if row.get("price") != "[TBC]" and row.get("lt") != "[TBC]":
        return row

    quote_row = _try_smc_portal_row(
        part_no,
        row.get("qty", 1),
        desc=row.get("desc"),
        brand="SMC",
        search_context=row.get("search_context") or "",
    )
    if not quote_row:
        return row

    merged = dict(row)
    if merged.get("price") == "[TBC]" and quote_row.get("price") != "[TBC]":
        merged["price"] = quote_row["price"]
    if merged.get("lt") == "[TBC]" and quote_row.get("lt") != "[TBC]":
        merged["lt"] = quote_row["lt"]
    if quote_row.get("desc"):
        merged["desc"] = quote_row["desc"]
    if quote_row.get("pid"):
        merged["pid"] = quote_row["pid"]
    if quote_row.get("smc_part"):
        merged["smc_part"] = quote_row["smc_part"]
    if quote_row.get("qty"):
        merged["qty"] = quote_row["qty"]
    if quote_row.get("requested_qty") is not None:
        merged["requested_qty"] = quote_row["requested_qty"]
    if quote_row.get("moq") is not None:
        merged["moq"] = quote_row["moq"]
    merged["moq_applied"] = quote_row.get("moq_applied", merged.get("moq_applied"))
    merged["source"] = quote_row.get("source", merged.get("source"))
    merged["needs_supplier"] = quote_row.get("needs_supplier", merged.get("needs_supplier"))
    return merged


def _merge_burkert_quote_into_row(row, part_no):
    """Update an existing quote row from the Burkert price list when price/LT are still TBC."""
    if str(row.get("brand") or "").upper().replace("BÜRKERT", "BURKERT") != "BURKERT":
        return row

    if row.get("price") != "[TBC]" and row.get("lt") != "[TBC]":
        return row

    quote_row = _try_burkert_price_list_row(
        part_no,
        row.get("qty", 1),
        desc=row.get("desc"),
        brand="BURKERT",
        search_context=row.get("search_context") or "",
        burkert_id=row.get("burkert_id") or "",
        technical_specs=row.get("technical_specs") or [],
    )
    if not quote_row:
        return row

    merged = dict(row)
    if merged.get("price") == "[TBC]" and quote_row.get("price") != "[TBC]":
        merged["price"] = quote_row["price"]
    if merged.get("lt") == "[TBC]" and quote_row.get("lt") != "[TBC]":
        merged["lt"] = quote_row["lt"]
    if quote_row.get("desc"):
        merged["desc"] = quote_row["desc"]
    if quote_row.get("pid"):
        merged["pid"] = quote_row["pid"]
    if quote_row.get("burkert_id"):
        merged["burkert_id"] = quote_row["burkert_id"]
    if quote_row.get("qty"):
        merged["qty"] = quote_row["qty"]
    if quote_row.get("requested_qty") is not None:
        merged["requested_qty"] = quote_row["requested_qty"]
    if quote_row.get("moq") is not None:
        merged["moq"] = quote_row["moq"]
    merged["moq_applied"] = quote_row.get("moq_applied", merged.get("moq_applied"))
    merged["source"] = quote_row.get("source", merged.get("source"))
    merged["needs_supplier"] = quote_row.get("needs_supplier", merged.get("needs_supplier"))
    return merged


def get_store_qty_from_product(obm):
    """
    Only STORE location is considered usable stock.
    Other locations such as LOANPROJECT are ignored because they may be booked/in use.
    """
    location_qty = obm.get("location_qty") or []

    for loc in location_qty:
        location = str(loc.get("location", "")).strip().upper()
        if location == "STORE":
            try:
                return float(loc.get("qty") or 0)
            except Exception:
                return 0.0

    # Strict rule requested: if STORE is not listed, usable qty is 0.
    return 0.0


def build_rows_from_api(api_id, qty, customer_part=None):
    _ensure_warehouse_loaded()
    print(f"⚙️ [ENGINE] Checking OBM API for: {api_id}")

    obm = get_product(api_id)
    p_res = get_purchase_price(api_id)

    # Some legacy OBM product IDs return stock/price metadata incompletely.
    # Preserve the already-verified warehouse identity instead of degrading a
    # correct match into an "UNKNOWN" description.
    warehouse = EXACT_LOOKUP.get(normalize_part(api_id), {})

    brand = str(obm.get("brand") or warehouse.get("brand") or "UNKNOWN").upper()
    pn = str(
        obm.get("product_name")
        or warehouse.get("stock_name")
        or warehouse.get("model_no")
        or api_id
    ).strip()
    model = str(obm.get("model") or warehouse.get("alt_model") or "").strip()

    full_desc = f"{brand} {pn}".strip()
    if model and model.upper() not in pn.upper():
        full_desc += f" ({model})"

    try:
        cost = float(p_res.get("unit_price", {}).get("price") or 0)
    except Exception:
        cost = 0.0

    product_lookup_invalid = str(obm.get("error") or "") == "101"
    if product_lookup_invalid:
        print(f"   ⚠️ OBM GetProduct error 101 for {api_id!r} — cannot confirm Ex-Stock from API")

    store_qty = get_store_qty_from_product(obm) if not product_lookup_invalid else 0.0
    usable_store_qty = int(store_qty) if store_qty > 0 else 0
    requested_qty = int(qty)

    try:
        total_stock_qty = float(obm.get("stock_qty", 0) or 0)
    except Exception:
        total_stock_qty = 0.0

    print(f"   Brand: {brand}")
    print(f"   Product: {full_desc}")
    print(f"   OBM total stock_qty field: {total_stock_qty}")
    print(f"   OBM STORE qty used for Ex-Stock: {usable_store_qty}")
    print(f"   Cost: RM {cost}")
    print(f"   Customer Qty: {requested_qty}")

    rows = []
    supplier_item = None

    if usable_store_qty > 0:
        quoted_qty = min(requested_qty, usable_store_qty)
        balance_qty = max(requested_qty - quoted_qty, 0)
        sell_price = (cost / MARKUP_DIVISOR) if cost > 0 else None

        stock_source = "STORE_STOCK_AVAILABLE"
        stock_lead_time = "Ex-Stock"

        rows.append({
            "desc": full_desc,
            "qty": quoted_qty,
            "price": f"{sell_price:,.2f}" if sell_price is not None else "[TBC]",
            "lt": stock_lead_time,
            "pid": api_id,
            "brand": brand,
            "source": stock_source,
            "customer_part": customer_part or api_id,
            "needs_supplier": False,
        })

        if sell_price is not None:
            print(f"   ✅ Warehouse stock available. Quoting Qty {quoted_qty} at RM {sell_price:,.2f}")
        else:
            print(
                f"   ✅ Warehouse stock available. Quoting Qty {quoted_qty} Ex-Stock "
                "(unit price TBC — no purchase cost on file)"
            )

        if balance_qty > 0:
            balance_row = {
                "desc": full_desc,
                "qty": balance_qty,
                "price": "[TBC]",
                "lt": "[TBC]",
                "pid": api_id,
                "brand": brand,
                "source": "BALANCE_SUPPLIER_REQUIRED",
                "customer_part": customer_part or api_id,
                "needs_supplier": True,
            }
            balance_row = _merge_burkert_quote_into_row(balance_row, customer_part or api_id)
            balance_row = _merge_smc_quote_into_row(balance_row, customer_part or api_id)
            rows.append(balance_row)

            if balance_row.get("needs_supplier", True):
                supplier_item = {
                    "desc": full_desc,
                    "qty": balance_qty,
                    "pid": api_id,
                    "brand": brand,
                }
                print(
                    f"   ⚠️ Requested qty {requested_qty} exceeds STORE stock {usable_store_qty}. "
                    f"Balance qty {balance_qty} quoted as [TBC]."
                )
            else:
                supplier_item = None
                print(
                    f"   ✅ Balance qty {balance_qty} quoted from Burkert price list "
                    f"at RM {balance_row.get('price')} | LT {balance_row.get('lt')}"
                )

        rows[0] = _merge_burkert_quote_into_row(rows[0], customer_part or api_id)
        rows[0] = _merge_smc_quote_into_row(rows[0], customer_part or api_id)
        return rows, supplier_item

    print("   ⚠️ No usable STORE stock. Full quantity added to supplier RFQ queue.")

    no_stock_row = {
        "desc": full_desc if full_desc else api_id,
        "qty": requested_qty,
        "price": "[TBC]",
        "lt": "[TBC]",
        "pid": api_id,
        "brand": brand,
        "source": "NO_STORE_STOCK_OR_COST",
        "customer_part": customer_part or api_id,
        "needs_supplier": True,
    }
    no_stock_row = _merge_burkert_quote_into_row(no_stock_row, customer_part or api_id)
    no_stock_row = _merge_smc_quote_into_row(no_stock_row, customer_part or api_id)
    rows.append(no_stock_row)

    supplier_item = None
    if no_stock_row.get("needs_supplier", True):
        supplier_item = {
            "desc": full_desc if full_desc else api_id,
            "qty": requested_qty,
            "pid": api_id,
            "brand": brand,
        }
    else:
        print(
            f"   ✅ Quoted from Burkert price list at RM {no_stock_row.get('price')} "
            f"| LT {no_stock_row.get('lt')}"
        )

    return rows, supplier_item


# Backward-compatible wrapper for any old caller.
def build_row_from_api(api_id, qty, customer_part=None):
    rows, supplier_item = build_rows_from_api(api_id, qty, customer_part)
    if len(rows) == 1:
        return rows[0]
    # If split, return the first quoted row for old code paths.
    return rows[0]


def process_structured_items(structured_items):
    formatted_rows = []
    tbc_by_brand = {}
    skipped = []
    voltage_selections = []

    for item in structured_items:
        part_no = item["part_no"]
        qty = item["qty"]
        declared_brand = item.get("brand") or "UNKNOWN"
        search_context = item.get("search_context") or ""

        match, variants = resolve_warehouse_match(
            part_no,
            declared_brand=declared_brand,
            qty=qty,
            source=item.get("source") or "",
            search_context=search_context,
        )

        if variants:
            voltage_selections.append({
                "part_no": part_no,
                "brand": declared_brand,
                "qty": qty,
                "variants": variants,
            })
            continue

        if match:
            rows, supplier_item = build_rows_from_api(match["api_id"], qty, customer_part=part_no)
            formatted_rows.extend(rows)

            if supplier_item:
                brand = supplier_item.get("brand") or match.get("brand") or declared_brand or "UNKNOWN"
                tbc_by_brand.setdefault(brand, []).append(supplier_item)
        else:
            inferred_brand = declared_brand if declared_brand != "UNKNOWN" else infer_brand_from_part(part_no)
            desc = item.get("desc") or (f"{inferred_brand} {part_no}" if inferred_brand != "UNKNOWN" else part_no)

            smc_row = _try_smc_portal_row(
                part_no,
                qty,
                desc=desc,
                brand=declared_brand if declared_brand != "UNKNOWN" else inferred_brand,
                search_context=search_context,
            )
            if smc_row:
                formatted_rows.append(smc_row)
                if smc_row.get("needs_supplier") and inferred_brand in (WAREHOUSE_BRANDS | KNOWN_BRANDS):
                    tbc_by_brand.setdefault(inferred_brand, []).append({
                        "desc": smc_row.get("desc") or desc,
                        "qty": qty,
                        "pid": part_no,
                        "brand": inferred_brand,
                    })
                continue

            burkert_row = _try_burkert_price_list_row(
                part_no,
                qty,
                desc=desc,
                brand=declared_brand if declared_brand != "UNKNOWN" else inferred_brand,
                search_context=search_context,
                burkert_id=item.get("burkert_id") or "",
                technical_specs=item.get("technical_specs") or [],
            )
            if burkert_row:
                formatted_rows.append(burkert_row)
                if burkert_row.get("needs_supplier") and inferred_brand in (WAREHOUSE_BRANDS | KNOWN_BRANDS):
                    tbc_by_brand.setdefault(inferred_brand, []).append({
                        "desc": burkert_row.get("desc") or desc,
                        "qty": qty,
                        "pid": part_no,
                        "brand": inferred_brand,
                    })
                continue

            formatted_rows.append({
                "desc": desc,
                "qty": qty,
                "price": "[TBC]",
                "lt": "[TBC]",
                "pid": part_no,
                "brand": inferred_brand,
                "source": item["source"],
                "customer_part": part_no,
                "needs_supplier": False,
            })

            if inferred_brand != "UNKNOWN" and inferred_brand in (WAREHOUSE_BRANDS | KNOWN_BRANDS):
                tbc_by_brand.setdefault(inferred_brand, []).append({
                    "desc": desc,
                    "qty": qty,
                    "pid": part_no,
                    "brand": inferred_brand,
                })
                print(f"   📡 [ENGINE] Known-brand unmatched item routed to supplier RFQ: {desc} | Qty: {qty}")
            else:
                skipped.append({
                    "brand": inferred_brand,
                    "part_no": part_no,
                    "qty": qty,
                    "desc": desc,
                    "reason": "Unknown brand / not found in warehouse",
                })
                print(f"   🧩 [ENGINE] Unknown-brand item skipped to technical: {desc} | Qty: {qty}")

    return formatted_rows, tbc_by_brand, skipped, voltage_selections


def process_inquiry_text(inquiry_text):
    print("")
    print("=" * 90)
    print("🧠 [ENGINE] START INQUIRY PROCESSING")
    print("=" * 90)

    body_clean = clean_text(inquiry_text)
    structured_items = extract_structured_rfq_items(body_clean)

    if structured_items:
        print(f"🧩 [ENGINE] Explicit structured extraction found: {len(structured_items)} item(s)")
        for item in structured_items:
            print(
                f"   - Part: {item['part_no']} | Qty: {item['qty']} | "
                f"Brand: {item['brand']} | Source: {item['source']}"
            )

        formatted_rows, tbc_by_brand, skipped, _voltage_selections = process_structured_items(structured_items)

        print("=" * 90)
        print("✅ [ENGINE] END INQUIRY PROCESSING")
        print("=" * 90)

        return {
            "formatted_rows": formatted_rows,
            "tbc_by_brand": tbc_by_brand,
            "has_partial": False,
            "missing_layer2_items": [],
            "skipped": skipped,
        }

    print("ℹ️ [ENGINE] No explicit structured item found.")
    print("=" * 90)
    print("✅ [ENGINE] END INQUIRY PROCESSING")
    print("=" * 90)

    return {
        "formatted_rows": [],
        "tbc_by_brand": {},
        "has_partial": False,
        "missing_layer2_items": [],
        "skipped": [],
    }


def infer_customer_greeting(customer_message: str = "") -> str:
    """Match customer's time-of-day greeting when present."""
    text = str(customer_message or "").upper()
    if re.search(r"\b(GOOD\s+)?MORNING\b", text):
        return "Good morning"
    if re.search(r"\b(GOOD\s+)?AFTERNOON\b", text):
        return "Good afternoon"
    if re.search(r"\b(GOOD\s+)?EVENING\b", text):
        return "Good evening"
    return "Hello"


def build_voltage_selection_reply(voltage_selections, customer_message=None):
    """Ask the customer to pick a voltage-specific warehouse SKU when the base part is ambiguous."""
    company = os.getenv("OPENCLAW_COMPANY_NAME", "Robomatics").strip() or "Robomatics"
    greeting = infer_customer_greeting(customer_message)
    msg = f"{greeting},\n\nThank you for your enquiry.\n\n"

    for selection in voltage_selections or []:
        part_no = str(selection.get("part_no") or "").strip()
        brand = str(selection.get("brand") or "").strip()
        label = f"{brand} {part_no}".strip() if brand and brand != "UNKNOWN" else part_no
        msg += (
            f"For {label}, we stock more than one voltage option. "
            f"Please confirm which exact model you need:\n\n"
        )
        for index, variant in enumerate(selection.get("variants") or [], start=1):
            stock_name = str(
                variant.get("stock_name")
                or variant.get("model_no")
                or variant.get("voltage_label")
                or ""
            ).strip()
            msg += f"{index}. {stock_name}\n"
        msg += "\n"

    msg += (
        "Please reply with the exact model/voltage from the list above "
        "(for example, copy option 1).\n\n"
        f"Best regards,\n{company}"
    )
    return msg


def _copilot_item_for_part(copilot_items: list, part_no: str) -> dict:
    target = normalize_part(part_no)
    if not target:
        return {}
    for item in copilot_items or []:
        if not isinstance(item, dict):
            continue
        if normalize_part(item.get("part_no")) == target:
            return item
    return {}


def _row_burkert_id(row: dict, copilot_items: list = None) -> str:
    """Customer-facing Burkert ID for a quote row."""
    brand = str(row.get("brand") or "").upper().replace("BÜRKERT", "BURKERT")
    if brand != "BURKERT":
        return ""

    display_id = str(row.get("burkert_id") or "").strip()
    if display_id:
        return format_burkert_id_display(display_id)

    raw_id = str(row.get("pid") or "").strip()
    if raw_id.isdigit():
        return format_burkert_id_display(raw_id)

    customer_part = str(row.get("customer_part") or "").strip()
    copilot_item = _copilot_item_for_part(copilot_items, customer_part or row.get("desc", ""))
    copilot_id = str(copilot_item.get("burkert_id") or "").strip()
    if copilot_id:
        return format_burkert_id_display(copilot_id)
    return ""


def _format_burkert_order_label(row: dict, copilot_items: list = None) -> str:
    """One-line Burkert order label: brand, model, and nameplate specs."""
    brand = str(row.get("brand") or "").upper().replace("BÜRKERT", "BURKERT")
    if brand != "BURKERT":
        return ""

    customer_part = str(row.get("customer_part") or "").strip()
    copilot_item = _copilot_item_for_part(copilot_items, customer_part or row.get("desc", ""))
    part_no = str(copilot_item.get("part_no") or customer_part or row.get("desc") or "").strip()
    if not part_no:
        return ""

    tokens = [f"{brand} {part_no}"]
    specs = copilot_item.get("technical_specs") or row.get("technical_specs") or []
    if isinstance(specs, str):
        specs = [specs]
    for spec in specs:
        text = str(spec).strip()
        if not text:
            continue
        if ":" in text:
            label, value = text.split(":", 1)
            if label.strip().upper() in {"MODEL", "TYPE"}:
                continue
            value = value.strip()
            if value:
                tokens.append(value.replace(" - ", "-").replace(" ", " ").upper())
        else:
            tokens.append(text)

    return " ".join(tokens)


def _format_burkert_order_label_from_item(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    brand = str(item.get("brand") or "BURKERT").strip().upper().replace("BÜRKERT", "BURKERT")
    part_no = str(item.get("part_no") or "").strip()
    if not part_no:
        return ""
    return _format_burkert_order_label(
        {"brand": brand, "customer_part": part_no, "technical_specs": item.get("technical_specs") or []},
        copilot_items=[item],
    )


def _display_product_name(row: dict, copilot_items: list = None) -> str:
    """Prefer a readable product label from extraction, then warehouse row."""
    customer_part = str(row.get("customer_part") or "").strip()
    copilot_item = _copilot_item_for_part(copilot_items, customer_part or row.get("desc", ""))
    description = str(copilot_item.get("description") or "").strip()
    if description:
        first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
        if first_line:
            return first_line
    desc = str(row.get("desc") or "").strip()
    brand = str(copilot_item.get("brand") or row.get("brand") or "").strip()
    part_no = customer_part or desc
    if brand and brand.upper() not in ("UNKNOWN", ""):
        if normalize_part(brand) not in normalize_part(part_no):
            return f"{brand} {part_no}".strip()
    return desc or part_no


def _format_technical_details_section(product_details: str = "") -> str:
    """Format extracted or researched product details for the quotation body."""
    details = str(product_details or "").strip()
    if not details:
        return ""
    return f"Product details:\n{details}\n\n"


def build_plain_quotation_reply(
    rows,
    ai_research=None,
    photo_confirmation=None,
    customer_message=None,
    copilot_items=None,
):
    company = os.getenv("OPENCLAW_COMPANY_NAME", "Robomatics").strip() or "Robomatics"
    greeting = infer_customer_greeting(customer_message)
    msg = f"{greeting},\n\nThank you for your enquiry.\n\n"

    if photo_confirmation and not copilot_items:
        msg += f"{str(photo_confirmation).strip()}\n\n"

    if len(rows) == 1:
        row = rows[0]
        product_name = _display_product_name(row, copilot_items=copilot_items)
        qty = int(row.get("qty", 1))
        unit = "pc" if qty == 1 else "pcs"
        msg += (
            f"Please find below our preliminary quotation for "
            f"{product_name} — quantity {qty} {unit}:\n"
        )
        order_label = _format_burkert_order_label(row, copilot_items=copilot_items)
        burkert_id = _row_burkert_id(row, copilot_items=copilot_items)
        if order_label:
            msg += f"{order_label}\n"
        if burkert_id:
            msg += f"ID: {burkert_id}\n"
        msg += "\n"
    else:
        msg += "Please find below our preliminary quotation:\n\n"

    total = 0.0
    has_total = False
    has_ex_stock = False
    has_tbc_balance = False

    for row in rows:
        desc = _display_product_name(row, copilot_items=copilot_items)
        qty = int(row.get("qty", 1))
        price = row.get("price", "[TBC]")
        lt = row.get("lt", "[TBC]")

        if str(lt).startswith("Ex-Stock"):
            has_ex_stock = True
        if price == "[TBC]" and str(lt) == "[TBC]":
            has_tbc_balance = True

        msg += f"• {desc}\n"
        burkert_id = _row_burkert_id(row, copilot_items=copilot_items)
        if burkert_id and len(rows) != 1:
            order_label = _format_burkert_order_label(row, copilot_items=copilot_items)
            if order_label:
                msg += f"  {order_label}\n"
            msg += f"  ID: {burkert_id}\n"
        requested_qty = int(row.get("requested_qty") or qty)
        moq = int(row.get("moq") or 0)
        if row.get("moq_applied") and requested_qty != qty:
            msg += (
                f"  Quantity: {qty} {'pc' if qty == 1 else 'pcs'} "
                f"(MOQ — you requested {requested_qty} {'pc' if requested_qty == 1 else 'pcs'})\n"
            )
        else:
            msg += f"  Quantity: {qty} {'pc' if qty == 1 else 'pcs'}\n"
        if moq > 1:
            msg += f"  MOQ: {moq} pcs\n"
        if price == "[TBC]":
            msg += "  Unit price: Pending verification\n"
        else:
            msg += f"  Unit price: RM {price}\n"
        if lt == "[TBC]":
            msg += "  Lead time: Pending verification\n"
        else:
            msg += f"  Lead time: {lt}\n"

        if price != "[TBC]":
            price_val = float(str(price).replace(",", ""))
            subtotal = price_val * qty
            total += subtotal
            has_total = True
            msg += f"  Subtotal: RM {subtotal:,.2f}\n"

        msg += "\n"

    if has_total:
        msg += f"Total quoted amount: RM {total:,.2f}\n\n"

    if has_ex_stock and has_tbc_balance:
        msg += (
            "Available store quantity is quoted Ex-Stock above. "
            "Any remaining quantity is pending verification and will be updated shortly.\n\n"
        )
    elif has_tbc_balance:
        msg += (
            "We are confirming stock availability and final pricing with our supplier "
            "and will update you shortly.\n\n"
        )

    technical_block = _format_technical_details_section(ai_research)
    if technical_block:
        msg += technical_block

    msg += f"Best regards,\n{company}"

    return msg
