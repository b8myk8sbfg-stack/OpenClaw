import os
import re
import csv
import requests
import urllib3
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from dotenv import load_dotenv

urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv()

VERSION = "v1.09-SMC-PORTAL-NEW-STOCK"

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"

OBM_API_URL = os.getenv("OBM_API_URL", "").rstrip("/")
OBM_API_KEY = os.getenv("OBM_API_KEY")
OBM_API_SECRET = os.getenv("OBM_API_SECRET")
OBM_AUTH = HTTPBasicAuth(OBM_API_KEY, OBM_API_SECRET)

# Customer sell price = purchase cost / MARKUP_DIVISOR (0.72 → ~38.9% markup on cost).
MARKUP_DIVISOR = float(os.getenv("OPENCLAW_MARKUP_DIVISOR", "0.72"))

KNOWN_BRANDS = {
    "OMRON", "SMC", "BURKERT", "BÜRKERT", "LEGRIS", "PANASONIC", "PISCO",
    "THK", "LOCTITE", "KEYENCE", "FESTO", "SICK", "IFM", "PARKER", "ABB", "SIEMENS",
    "ALLEN BRADLEY", "NITTO KOHKI", "CKD", "KOGANEI", "AIRTAC", "YASKAWA"
}


def normalize_part(part):
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


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

    print("📦 [ENGINE] Loading Warehouse Database...")

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
                norm = normalize_part(val)
                if norm and norm not in exact_lookup:
                    exact_lookup[norm] = row_data

    WAREHOUSE_ROWS = rows
    EXACT_LOOKUP = exact_lookup
    WAREHOUSE_BRANDS = brands

    print(f"✅ [ENGINE] Loaded {len(rows)} warehouse rows.")
    print(f"✅ [ENGINE] Loaded {len(exact_lookup)} exact lookup keys.")

    return rows, exact_lookup


load_warehouse_map()


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


def resolve_warehouse_match(part_no, declared_brand="UNKNOWN", qty=1, source=""):
    """Resolve warehouse stock for a part, with stricter rules for visual extraction."""
    part_no = str(part_no or "").strip().upper()
    source = str(source or "").upper()

    exact = EXACT_LOOKUP.get(normalize_part(part_no))
    if exact:
        return exact

    if source == "COPILOT_VISUAL":
        partial = find_best_warehouse_match(part_no, declared_brand=declared_brand, qty=qty)
        if partial and warehouse_match_trusted(part_no, partial):
            return partial
        if partial:
            print(
                f"   ⚠️ [ENGINE] Rejected warehouse remap for visual part {part_no} "
                f"→ {partial.get('stock_name') or partial.get('api_id')} (different product family)"
            )
        return None

    return find_best_warehouse_match(part_no, declared_brand=declared_brand, qty=qty)


def infer_brand_from_part(part_no):
    part_norm = normalize_part(part_no)

    # Safe family inference for common automation brands.
    if part_norm.startswith("E3Z") or part_norm.startswith("E39") or part_norm.startswith("E2E") or part_norm.startswith("MY2") or part_norm.startswith("MY4") or part_norm.startswith("H3Y"):
        return "OMRON"

    return "UNKNOWN"


def extract_voltage_signature(text):
    """Return (AC/DC, voltage values) while treating DC24 and 24VDC equally."""
    value = str(text or "").upper().replace(" ", "")
    patterns = (
        ("DC", r"DC(\d+(?:/\d+)*)"),
        ("DC", r"(\d+(?:/\d+)*)VDC"),
        ("AC", r"AC(\d+(?:/\d+)*)"),
        ("AC", r"(\d+(?:/\d+)*)VAC"),
    )
    for current_type, pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return current_type, tuple(int(part) for part in match.group(1).split("/"))
    return None


def get_usable_store_qty(api_id, warehouse_row=None):
    """Return STORE-location quantity from OBM, with CSV fallback for legacy PIDs."""
    obm = get_product(api_id)
    store_qty = get_store_qty_from_product(obm)
    if store_qty > 0:
        return store_qty

    if str(obm.get("error") or "") == "101" and warehouse_row:
        return float(warehouse_row.get("stock_qty") or 0)

    return 0.0


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
        if not set(requested_voltage[1]).intersection(stock_voltage[1]):
            return None
    if stock_voltage:
        candidate_voltages.add(stock_voltage)

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


def find_best_warehouse_match(part_no, declared_brand="UNKNOWN", qty=1):
    part_no = str(part_no or "").strip().upper()
    declared_brand = str(declared_brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")

    norm = normalize_part(part_no)
    if not norm:
        return None

    exact = EXACT_LOOKUP.get(norm)
    if exact:
        print(f"   ✅ [ENGINE] Exact lookup match: {part_no} → {exact['api_id']}")
        return exact

    if declared_brand == "UNKNOWN":
        declared_brand = infer_brand_from_part(part_no)

    requested_voltage = extract_voltage_signature(part_no)
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
            f"{sorted(candidate_voltages)}. Refusing to guess."
        )
        return None

    best = _pick_best_warehouse_candidate(candidates, part_no, qty)
    if best:
        print(
            f"   ✅ [ENGINE] Partial stock-family match: {part_no} → {best['api_id']} | "
            f"{best['stock_name']} | CSV Qty: {best.get('stock_qty')} | Score: {best.get('score')}"
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
    using_csv_stock_fallback = product_lookup_invalid and bool(warehouse)

    try:
        total_stock_qty = float(obm.get("stock_qty", 0) or 0)
    except Exception:
        total_stock_qty = 0.0

    store_qty = get_store_qty_from_product(obm)
    if using_csv_stock_fallback:
        store_qty = float(warehouse.get("stock_qty") or 0)
        total_stock_qty = store_qty
        print(
            "   ⚠️ OBM GetProduct rejected this legacy PID; "
            f"using warehouse CSV quantity fallback: {store_qty}"
        )
    usable_store_qty = int(store_qty) if store_qty > 0 else 0
    requested_qty = int(qty)

    print(f"   Brand: {brand}")
    print(f"   Product: {full_desc}")
    stock_label = "Warehouse CSV fallback" if using_csv_stock_fallback else "OBM"
    print(f"   Total Stock Qty from {stock_label}: {total_stock_qty}")
    print(f"   Usable Warehouse Qty Used by Bot: {usable_store_qty}")
    print(f"   Cost: RM {cost}")
    print(f"   Customer Qty: {requested_qty}")

    rows = []
    supplier_item = None

    if usable_store_qty > 0:
        quoted_qty = min(requested_qty, usable_store_qty)
        balance_qty = max(requested_qty - quoted_qty, 0)
        sell_price = (cost / 0.8) if cost > 0 else None

        stock_source = (
            "WAREHOUSE_CSV_STOCK_FALLBACK"
            if using_csv_stock_fallback
            else "STORE_STOCK_AVAILABLE"
        )
        stock_lead_time = (
            "Ex-Stock (Warehouse CSV)"
            if using_csv_stock_fallback
            else "Ex-Stock (STORE)"
        )

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
            rows.append({
                "desc": full_desc,
                "qty": balance_qty,
                "price": "[TBC]",
                "lt": "[TBC]",
                "pid": api_id,
                "brand": brand,
                "source": "BALANCE_SUPPLIER_REQUIRED",
                "customer_part": customer_part or api_id,
                "needs_supplier": True,
            })

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

        return rows, supplier_item

    print("   ⚠️ No usable STORE stock. Full quantity added to supplier RFQ queue.")

    rows.append({
        "desc": full_desc if full_desc else api_id,
        "qty": requested_qty,
        "price": "[TBC]",
        "lt": "[TBC]",
        "pid": api_id,
        "brand": brand,
        "source": "NO_STORE_STOCK_OR_COST",
        "customer_part": customer_part or api_id,
        "needs_supplier": True,
    })

    supplier_item = {
        "desc": full_desc if full_desc else api_id,
        "qty": requested_qty,
        "pid": api_id,
        "brand": brand,
    }

    return rows, supplier_item


def _try_smc_portal_row(part_no, qty, desc=None, brand="", search_context=""):
    """Fill price and lead time from the SMC distributor web portal when available."""
    brand_u = str(brand or "").upper().replace("BÜRKERT", "BURKERT")
    if brand_u != "SMC":
        return None

    try:
        from smc_portal_lookup import lookup_smc_quote
    except ImportError:
        print("   ⚠️ [ENGINE] smc_portal_lookup not available — SMC portal search skipped.")
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

    for item in structured_items:
        part_no = item["part_no"]
        qty = item["qty"]
        declared_brand = item.get("brand") or "UNKNOWN"

        match = resolve_warehouse_match(
            part_no,
            declared_brand=declared_brand,
            qty=qty,
            source=item.get("source") or "",
        )

        if match:
            rows, supplier_item = build_rows_from_api(match["api_id"], qty, customer_part=part_no)
            formatted_rows.extend(rows)

            if supplier_item:
                brand = supplier_item.get("brand") or match.get("brand") or declared_brand or "UNKNOWN"
                tbc_by_brand.setdefault(brand, []).append(supplier_item)
        else:
            inferred_brand = declared_brand if declared_brand != "UNKNOWN" else infer_brand_from_part(part_no)
            desc = item.get("desc") or (f"{inferred_brand} {part_no}" if inferred_brand != "UNKNOWN" else part_no)
            search_context = item.get("search_context") or ""

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
                    print(f"   📡 [ENGINE] SMC portal partial — supplier RFQ for balance: {desc} | Qty: {qty}")
                else:
                    print(f"   ✅ [ENGINE] SMC portal quote: {smc_row.get('desc')} | Qty: {qty}")
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

    return formatted_rows, tbc_by_brand, skipped


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

        formatted_rows, tbc_by_brand, skipped = process_structured_items(structured_items)

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


def build_plain_quotation_reply(rows, ai_research=None):
    msg = "Hi, thank you for your inquiry.\n\n"

    if ai_research:
        msg += "Product information:\n"
        msg += str(ai_research).strip()
        msg += "\n\n"

    msg += "Here is the initial status:\n\n"

    total = 0.0
    has_total = False
    has_ex_stock = False
    has_tbc_balance = False

    for row in rows:
        desc = row.get("desc", "")
        customer_part = str(row.get("customer_part") or "").strip()
        if customer_part and customer_part.upper() not in desc.upper():
            desc = customer_part

        qty = int(row.get("qty", 1))
        price = row.get("price", "[TBC]")
        lt = row.get("lt", "[TBC]")

        if str(lt).startswith("Ex-Stock"):
            has_ex_stock = True
        if price == "[TBC]" and str(lt) == "[TBC]":
            has_tbc_balance = True

        msg += f"- {desc}\n"
        msg += f"  Qty: {qty}\n"
        msg += f"  Unit Price: RM {price}\n"
        msg += f"  Lead Time: {lt}\n"

        if price != "[TBC]":
            price_val = float(str(price).replace(",", ""))
            subtotal = price_val * qty
            total += subtotal
            has_total = True
            msg += f"  Subtotal: RM {subtotal:,.2f}\n"

        msg += "\n"

    if has_total:
        msg += f"Total available quoted amount: RM {total:,.2f}\n\n"

    if has_ex_stock and has_tbc_balance:
        msg += (
            "Available STORE quantity is quoted Ex-Stock above. "
            "Any remaining quantity is marked [TBC] and will be verified shortly.\n\n"
        )

    msg += "Items marked [TBC] will be verified and updated shortly."
    return msg
