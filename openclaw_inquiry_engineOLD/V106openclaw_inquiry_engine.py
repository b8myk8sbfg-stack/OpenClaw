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

VERSION = "v1.06-PARTIAL-STOCK-FAMILY-MATCH"

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


def infer_brand_from_part(part_no):
    part_norm = normalize_part(part_no)

    # Safe family inference for common automation brands.
    if part_norm.startswith("E3Z") or part_norm.startswith("E39") or part_norm.startswith("MY4") or part_norm.startswith("H3Y"):
        return "OMRON"

    return "UNKNOWN"


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

    # If customer did not give brand, try safe inference before partial matching.
    if declared_brand == "UNKNOWN":
        declared_brand = infer_brand_from_part(part_no)

    best = None

    for row in WAREHOUSE_ROWS:
        if not row.get("api_id") or not row.get("stock_name"):
            continue

        row_brand = str(row.get("brand") or "").upper()
        stock_text = f"{row['api_id']} {row['stock_name']} {row['model_no']} {row['alt_model']} {row['brand']} {row['raw']}".upper()

        if declared_brand != "UNKNOWN" and row_brand and declared_brand not in row_brand and declared_brand not in stock_text:
            continue

        if not stock_contains_part_family(stock_text, part_no):
            continue

        score = 0
        score += startswith_part_boundary(row.get("stock_name"), part_no)

        part_norm = normalize_part(part_no)
        if part_norm in normalize_part(stock_text):
            score += 1000 + len(part_norm)

        if row.get("stock_qty", 0) >= qty:
            score += 500
        elif row.get("stock_qty", 0) > 0:
            score += 100

        score += min(int(row.get("stock_qty") or 0), 20) * 10

        # Prefer PHOTOELECTRIC SENSOR for E3Z/E39 family.
        if part_norm.startswith("E3Z") and "PHOTOELECTRIC SENSOR" in stock_text:
            score += 200
        if part_norm.startswith("E39") and "RETROREFLECTOR" in stock_text:
            score += 200

        if score <= 0:
            continue

        if best is None or score > best["score"]:
            best = {**row, "score": score, "match_type": "PARTIAL_STOCK_FAMILY"}

    if best:
        print(
            f"   ✅ [ENGINE] Partial stock-family match: {part_no} → {best['api_id']} | "
            f"{best['stock_name']} | Stock Qty: {best.get('stock_qty')} | Score: {best.get('score')}"
        )
        return best

    print(f"   ⚠️ [ENGINE] No warehouse match: {part_no}")
    return None


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


def build_row_from_api(api_id, qty, customer_part=None):
    print(f"⚙️ [ENGINE] Checking OBM API for: {api_id}")

    obm = get_product(api_id)
    p_res = get_purchase_price(api_id)

    brand = str(obm.get("brand", "UNKNOWN") or "UNKNOWN").upper()
    pn = str(obm.get("product_name", "") or "").strip()
    model = str(obm.get("model", "") or "").strip()

    full_desc = f"{brand} {pn}".strip()
    if model and model.upper() not in pn.upper():
        full_desc += f" ({model})"

    try:
        cost = float(p_res.get("unit_price", {}).get("price") or 0)
    except Exception:
        cost = 0.0

    try:
        stock_avail = float(obm.get("stock_qty", 0) or 0)
    except Exception:
        stock_avail = 0.0

    print(f"   Brand: {brand}")
    print(f"   Product: {full_desc}")
    print(f"   Stock Available: {stock_avail}")
    print(f"   Cost: RM {cost}")
    print(f"   Customer Qty: {qty}")

    if stock_avail >= qty and cost > 0:
        sell_price = cost / 0.8
        print(f"   ✅ Stock available. Sell Price: RM {sell_price:,.2f}")
        return {
            "desc": full_desc,
            "qty": qty,
            "price": f"{sell_price:,.2f}",
            "lt": "Ex-Stock",
            "pid": api_id,
            "brand": brand,
            "source": "STOCK_AVAILABLE",
            "customer_part": customer_part or api_id,
            "needs_supplier": False,
        }

    print("   ⚠️ Stock/cost unavailable. Added to supplier RFQ queue.")
    return {
        "desc": full_desc if full_desc else api_id,
        "qty": qty,
        "price": "[TBC]",
        "lt": "[TBC]",
        "pid": api_id,
        "brand": brand,
        "source": "STOCK_OR_COST_UNAVAILABLE",
        "customer_part": customer_part or api_id,
        "needs_supplier": True,
    }


def process_structured_items(structured_items):
    formatted_rows = []
    tbc_by_brand = {}
    skipped = []

    for item in structured_items:
        part_no = item["part_no"]
        qty = item["qty"]
        declared_brand = item.get("brand") or "UNKNOWN"

        match = find_best_warehouse_match(part_no, declared_brand=declared_brand, qty=qty)

        if match:
            row = build_row_from_api(match["api_id"], qty, customer_part=part_no)
            formatted_rows.append(row)

            if row["needs_supplier"]:
                brand = row["brand"] or match.get("brand") or declared_brand or "UNKNOWN"
                tbc_by_brand.setdefault(brand, []).append({
                    "desc": row["desc"],
                    "qty": qty,
                    "pid": match["api_id"],
                    "brand": brand,
                })
        else:
            inferred_brand = declared_brand if declared_brand != "UNKNOWN" else infer_brand_from_part(part_no)
            desc = item.get("desc") or (f"{inferred_brand} {part_no}" if inferred_brand != "UNKNOWN" else part_no)

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


def build_plain_quotation_reply(rows):
    msg = "Hi, thank you for your inquiry.\n\nHere is the initial status:\n\n"

    total = 0.0
    has_total = False

    for row in rows:
        desc = row.get("desc", "")
        qty = int(row.get("qty", 1))
        price = row.get("price", "[TBC]")
        lt = row.get("lt", "[TBC]")

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

    msg += "Items marked [TBC] will be verified and updated shortly."
    return msg
