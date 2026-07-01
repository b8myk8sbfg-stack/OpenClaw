import os
import re
import csv
import requests
import urllib3
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from dotenv import load_dotenv
from non_standard_inquiry_handler import handle_non_standard_items

urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv()

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"

OBM_API_URL = os.getenv("OBM_API_URL", "").rstrip("/")
OBM_API_KEY = os.getenv("OBM_API_KEY")
OBM_API_SECRET = os.getenv("OBM_API_SECRET")

OBM_AUTH = HTTPBasicAuth(OBM_API_KEY, OBM_API_SECRET)


def normalize_part(part):
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def load_warehouse_map():
    print("📦 [ENGINE] Loading Warehouse Database...")

    stock_db = []
    exact_lookup = {}

    if not os.path.exists(WAREHOUSE_CSV):
        print(f"❌ [ENGINE] Warehouse CSV not found: {WAREHOUSE_CSV}")
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

            values = [api_id, stock_name, model_no]

            for val in values:
                norm = normalize_part(val)
                if norm and norm not in exact_lookup:
                    exact_lookup[norm] = api_id

            def add_to_db(val, api_id, is_partial_source=False):
                if not val or len(val.strip()) < 4:
                    return

                core = re.sub(
                    r"^(SMC|OMRON|BURKERT|BÜRKERT|LEGRIS|THK|LOCTITE)\s+",
                    "",
                    val.upper().strip()
                )

                if len(normalize_part(core)) < 4:
                    return

                bad_generic = {
                    "OMRON", "SMC", "BURKERT", "BÜRKERT", "PLC", "VALVE",
                    "SENSOR", "SWITCH", "UNIT", "MODEL", "UNKNOWN",
                    "THK", "LOCTITE", "GREASE", "LUBRICANT"
                }

                if normalize_part(core) in {normalize_part(x) for x in bad_generic}:
                    return

                pat = (
                    re.escape(core)
                    .replace(r"\ ", r"[\s\-]*")
                    .replace(r"\-", r"[\s\-]*")
                )

                boundary_pat = rf"(?:^|[^a-zA-Z0-9])({pat})(?![a-zA-Z0-9])"

                stock_db.append({
                    "api_pid": api_id,
                    "regex": boundary_pat,
                    "len": len(core),
                    "is_partial": is_partial_source,
                    "norm": normalize_part(api_id),
                    "source_value": val
                })

            add_to_db(api_id, api_id)
            add_to_db(stock_name, api_id)
            add_to_db(model_no, api_id)

    stock_db.sort(key=lambda x: x["len"], reverse=True)

    print(f"✅ [ENGINE] Loaded {len(stock_db)} warehouse patterns.")
    print(f"✅ [ENGINE] Loaded {len(exact_lookup)} exact lookup keys.")

    return stock_db, exact_lookup


STOCK_DB, EXACT_LOOKUP = load_warehouse_map()


def exact_find_pid(part_no):
    norm = normalize_part(part_no)

    if not norm:
        return None

    pid = EXACT_LOOKUP.get(norm)

    if pid:
        print(f"   ✅ [ENGINE] Exact lookup match: {part_no} → {pid}")
        return pid

    print(f"   ⚠️ [ENGINE] Exact lookup no match: {part_no}")
    return None


def extract_structured_rfq_items(body_text):
    rfq_items = []
    body_upper = str(body_text or "").upper()
    existing = set()

    def add_item(brand, part_no, qty, source, desc=None):
        part_no = str(part_no or "").strip().upper()
        brand = str(brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")
        qty = int(qty or 1)
        norm = normalize_part(part_no)

        if not norm or norm in existing:
            return

        if len(norm) < 4:
            return

        existing.add(norm)

        rfq_items.append({
            "brand": brand,
            "part_no": part_no,
            "desc": desc or f"{brand} {part_no}",
            "qty": qty,
            "norm": norm,
            "source": source
        })

    # Format:
    # Brand : THK
    # Item : GREASE
    # Model : AFB-LF+400G
    # Quantity : 2 PCS
    pattern_brand_item_model = re.compile(
        r"BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+"
        r"ITEM\s*:\s*([A-Z0-9\s\-\(\)\+\/]+?)\s+"
        r"MODEL\s*:\s*([A-Z0-9\-\+\._/ ]+?)\s+"
        r"QUANTITY\s*:\s*(\d+)",
        re.I | re.S
    )

    for brand, item_name, model, qty in pattern_brand_item_model.findall(body_upper):
        brand = brand.strip().upper()
        item_name = re.sub(r"\s+", " ", item_name.strip().upper())
        model = re.sub(r"\s+", " ", model.strip().upper())
        qty = int(qty)

        add_item(
            brand=brand,
            part_no=model,
            qty=qty,
            source="BRAND_ITEM_MODEL_QTY",
            desc=f"{brand} {model} ({item_name})"
        )

    # Format:
    # Brand : SMC
    # Part No. : MXQ8-20
    # Quantity : 2
    pattern_brand_part_qty = re.compile(
        r"BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+"
        r"PART\s*NO\.?\s*:\s*([A-Z0-9\-_/ ]+?)\s+"
        r"QUANTITY\s*:\s*(\d+)",
        re.I | re.S
    )

    for brand, part_no, qty in pattern_brand_part_qty.findall(body_upper):
        add_item(
            brand=brand,
            part_no=part_no,
            qty=qty,
            source="BRAND_PART_QTY"
        )

    # Format:
    # I'm looking for this model of PLC from OMRON
    # Model: CJ2M-CPU32
    # Qty: 1 Pc
    brand_context = "UNKNOWN"
    brand_context_match = re.search(
        r"\bFROM\s+(OMRON|SMC|BURKERT|BÜRKERT|THK|LOCTITE|KEYENCE|FESTO|SICK|IFM|PARKER|ABB|SIEMENS)\b",
        body_upper,
        re.I
    )

    if brand_context_match:
        brand_context = brand_context_match.group(1).upper().replace("BÜRKERT", "BURKERT")

    model_qty_pattern = re.compile(
        r"(?:MODEL|PART\s*NO\.?|PART|ID)\s*:\s*([A-Z0-9\-_/ \+\.]{3,50}?)\s+"
        r"(?:QTY|QUANTITY)\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?",
        re.I | re.S
    )

    for part_no, qty in model_qty_pattern.findall(body_upper):
        add_item(
            brand=brand_context,
            part_no=part_no,
            qty=qty,
            source="EXPLICIT_MODEL_QTY"
        )

    # Format:
    # Burkert ID : 199983
    # 5PCS
    brand_id_qty_pattern = re.compile(
        r"\b(OMRON|SMC|BURKERT|BÜRKERT|KEYENCE|FESTO|SICK|IFM|PARKER|ABB|SIEMENS|THK|LOCTITE)\s+"
        r"(?:ID|NO|PART|MODEL)?\s*[:#]?\s*([A-Z0-9\-_/ \+\.]+?)"
        r"(?:.*?)"
        r"(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)",
        re.I | re.S
    )

    for brand, part_no, qty in brand_id_qty_pattern.findall(body_upper):
        add_item(
            brand=brand,
            part_no=part_no,
            qty=qty,
            source="BRAND_ID_QTY"
        )

    # WhatsApp / simple format:
    # E3Z-T61 Qty:1
    # 3104 10 00 Qty:2
    line_qty_pattern = re.compile(
        r"^\s*([A-Z0-9][A-Z0-9\-_/ \+\.]{2,40}?)\s+QTY\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?\s*$",
        re.I | re.M
    )

    for part_no, qty in line_qty_pattern.findall(body_upper):
        add_item(
            brand="UNKNOWN",
            part_no=part_no,
            qty=qty,
            source="LINE_QTY_FORMAT"
        )

    return rfq_items


def get_product(api_id):
    try:
        return requests.get(
            f"{OBM_API_URL}/GetProduct",
            auth=OBM_AUTH,
            params={"pid": api_id},
            verify=False
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
            verify=False
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
            "needs_supplier": False
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
        "needs_supplier": True
    }


def process_structured_items(structured_items):
    formatted_rows = []
    tbc_by_brand = {}
    skipped = []

    for item in structured_items:
        part_no = item["part_no"]
        qty = item["qty"]
        declared_brand = item.get("brand") or "UNKNOWN"

        api_id = exact_find_pid(part_no)

        if api_id:
            row = build_row_from_api(api_id, qty, customer_part=part_no)
            formatted_rows.append(row)

            if row["needs_supplier"]:
                brand = row["brand"] or declared_brand or "UNKNOWN"

                tbc_by_brand.setdefault(brand, []).append({
                    "desc": row["desc"],
                    "qty": qty,
                    "pid": api_id,
                    "brand": brand
                })

        else:
            brand = declared_brand or "UNKNOWN"
            desc = item.get("desc") or (f"{brand} {part_no}" if brand != "UNKNOWN" else part_no)

            row = {
                "desc": desc,
                "qty": qty,
                "price": "[TBC]",
                "lt": "[TBC]",
                "pid": part_no,
                "brand": brand,
                "source": item["source"],
                "customer_part": part_no,
                "needs_supplier": False
            }

            formatted_rows.append(row)

            skipped.append({
                "brand": brand,
                "part_no": part_no,
                "qty": qty,
                "desc": desc,
                "reason": "Not found in exact warehouse lookup"
            })

            print(f"   🧩 [ENGINE] Non-standard item skipped to technical: {desc} | Qty: {qty}")

    return formatted_rows, tbc_by_brand, skipped


def process_inquiry_text(inquiry_text):
    print("")
    print("=" * 90)
    print("🧠 [ENGINE] START INQUIRY PROCESSING")
    print("=" * 90)

    body_clean = re.sub(r"<[^>]+>", " ", inquiry_text or "")
    body_clean = re.sub(r"&[a-z0-9#]+;", " ", body_clean, flags=re.I)
    body_clean = re.sub(r"\r", "\n", body_clean)

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
            "skipped": skipped
        }

    print("ℹ️ [ENGINE] No explicit structured item found. Falling back to broad stock matching.")

    body_upper = body_clean.upper()
    matches = []

    print(f"📊 [ENGINE] Database Pattern Count: {len(STOCK_DB)}")

    for item in STOCK_DB:
        for m in re.finditer(item["regex"], body_upper):
            matches.append({
                "start": m.start(1),
                "end": m.end(1),
                "matched_text": m.group(1),
                "item": item
            })

    matches.sort(key=lambda x: x["start"])

    final_list = []
    last_end = -1
    has_partial = False

    for m in matches:
        if m["start"] >= last_end:
            final_list.append(m)
            last_end = m["end"]

            if m["item"]["is_partial"]:
                has_partial = True

            print(
                f"   🔎 [ENGINE] Layer 1 Stock Match | "
                f"API PID: {m['item']['api_pid']} | "
                f"Matched Text: {m['matched_text']} | "
                f"{'Partial' if m['item']['is_partial'] else 'Exact/Normal'}"
            )

    formatted_rows = []
    tbc_by_brand = {}
    skipped = []

    for m in final_list:
        api_id = m["item"]["api_pid"]

        q_match = re.search(
            r"(?:QTY|QUANTITY|X|:)\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|NOS)?",
            body_upper[m["end"]: m["end"] + 80]
        )

        qty = int(q_match.group(1)) if q_match else 1

        row = build_row_from_api(api_id, qty, customer_part=m["matched_text"])
        formatted_rows.append(row)

        if row["needs_supplier"]:
            brand = row["brand"] or "UNKNOWN"

            tbc_by_brand.setdefault(brand, []).append({
                "desc": row["desc"],
                "qty": qty,
                "pid": api_id,
                "brand": brand
            })

    print("=" * 90)
    print("✅ [ENGINE] END INQUIRY PROCESSING")
    print("=" * 90)

    return {
        "formatted_rows": formatted_rows,
        "tbc_by_brand": tbc_by_brand,
        "has_partial": has_partial,
        "missing_layer2_items": [],
        "skipped": skipped
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