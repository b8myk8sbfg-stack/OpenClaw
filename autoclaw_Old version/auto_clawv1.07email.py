import os
import requests
import time
import csv
import re
import urllib3
import datetime
import random
import string
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from dotenv import load_dotenv
from O365 import Account

from obm_quotation_helper import create_obm_quotation_from_inquiry
from non_standard_inquiry_handler import handle_non_standard_items

VERSION = "v1.07-EMAIL-NON-STANDARD-PN-FIX"

urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv()

WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"
PENDING_CSV = "/Users/evon/OpenClaw/pending_inquiries.csv"

TEST_ROUTING_EMAIL = "stephen@robomatics.sg"
MANAGER_EMAIL = "stephen@robomatics.sg"

PENDING_FIELDS = [
    "ref",
    "name",
    "email",
    "brand",
    "items",
    "created_at",
    "supplier_status",
    "supplier_replied_at",
    "last_checked_at",
    "manager_alerted_at"
]

SIGNATURE = (
    "Thanks & Regards,<br><br><strong>Evon</strong><br>"
    "Automation Engineer, Trade Affairs Division<br>ROBOMATICS (JOHOR) SDN. BHD.<br>"
    "✉️ evon@robomatics.sg | 📱 +6 016 710 4483"
)

BRAND_ROUTING = {
    "OMRON": "stephen@robomatics.sg",
    "SMC": "stephen@robomatics.sg",
    "BURKERT": "stephen@robomatics.sg",
    "FESTO": "stephen@robomatics.sg",
    "KEYENCE": "stephen@robomatics.sg",
    "SIEMENS": "stephen@robomatics.sg",
    "SCHNEIDER": "stephen@robomatics.sg",
    "MITSUBISHI": "stephen@robomatics.sg",
    "PANASONIC": "stephen@robomatics.sg",
    "CKD": "stephen@robomatics.sg",
    "KOGANEI": "stephen@robomatics.sg",
    "AIRTAC": "stephen@robomatics.sg",
    "CAMOZZI": "stephen@robomatics.sg",
    "PISCO": "stephen@robomatics.sg",
    "PIAB": "stephen@robomatics.sg",
    "YUKEN": "stephen@robomatics.sg",
    "YASKAWA": "stephen@robomatics.sg",
    "THK": "stephen@robomatics.sg",
    "NSK": "stephen@robomatics.sg",
    "NTN": "stephen@robomatics.sg",
    "HIWIN": "stephen@robomatics.sg",
    "MISUMI": "stephen@robomatics.sg",
    "IFM": "stephen@robomatics.sg",
    "SICK": "stephen@robomatics.sg",
    "LEUZE": "stephen@robomatics.sg",
    "BAUMER": "stephen@robomatics.sg",
    "CONTRINEX": "stephen@robomatics.sg",
    "HONEYWELL": "stephen@robomatics.sg",
    "EMERSON": "stephen@robomatics.sg",
    "DANFOSS": "stephen@robomatics.sg",
    "EATON": "stephen@robomatics.sg",
    "ABB": "stephen@robomatics.sg",
    "FUJI": "stephen@robomatics.sg",
    "DELTA": "stephen@robomatics.sg",
    "MEANWELL": "stephen@robomatics.sg",
    "COSEL": "stephen@robomatics.sg",
    "OMC": "stephen@robomatics.sg",
    "OMEGA": "stephen@robomatics.sg",
    "WIKA": "stephen@robomatics.sg",
    "DWYER": "stephen@robomatics.sg",
    "ASCO": "stephen@robomatics.sg",
    "MAC": "stephen@robomatics.sg",
    "NUMATICS": "stephen@robomatics.sg",
    "AVENTICS": "stephen@robomatics.sg",
    "NORGREN": "stephen@robomatics.sg",
    "HERION": "stephen@robomatics.sg",
    "BUSCHJOST": "stephen@robomatics.sg",
    "GOYEN": "stephen@robomatics.sg",
    "GEMU": "stephen@robomatics.sg",
    "KITZ": "stephen@robomatics.sg",
    "SWAGELOK": "stephen@robomatics.sg",
    "PARKER": "stephen@robomatics.sg",
    "REXROTH": "stephen@robomatics.sg",
    "BOSCH": "stephen@robomatics.sg",
    "FOTEK": "stephen@robomatics.sg",
    "AUTONICS": "stephen@robomatics.sg",
    "TAKEX": "stephen@robomatics.sg",
    "OPTEX": "stephen@robomatics.sg",
    "PATLITE": "stephen@robomatics.sg",
    "IDEC": "stephen@robomatics.sg",
    "UNKNOWN": "stephen@robomatics.sg",
    "DEFAULT": "stephen@robomatics.sg"
}


def log_line():
    print("-" * 90)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def gen_unique_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))


def normalize_part(part):
    return re.sub(r'[^A-Z0-9]', '', str(part or "").upper())


def clean_email_body(raw_body):
    body_clean = re.sub(r'<[^>]+>', ' ', raw_body or "")
    body_clean = re.sub(r'&[a-z0-9#]+;', ' ', body_clean, flags=re.I)
    body_clean = re.sub(r'\s+', ' ', body_clean)
    return body_clean.strip()


def is_inquiry_like(subject, body_clean):
    text = f"{subject or ''} {body_clean or ''}".upper()

    inquiry_keywords = [
        "ENQ",
        "RFQ",
        "QUOTE",
        "QUOTATION",
        "PLS QUOTE",
        "PLEASE QUOTE",
        "KINDLY QUOTE",
        "REQUEST FOR QUOTE",
        "REQUEST QUOTATION"
    ]

    return any(keyword in text for keyword in inquiry_keywords)


def get_routing_email(brand):
    brand = str(brand or "UNKNOWN").strip().upper()

    if not brand:
        brand = "UNKNOWN"

    email = BRAND_ROUTING.get(brand, BRAND_ROUTING["DEFAULT"])

    if brand not in BRAND_ROUTING:
        print(f"   ⚠️ Routing: Brand '{brand}' not found in BRAND_ROUTING. Using DEFAULT -> {email}")
    else:
        print(f"   📌 Routing: Brand '{brand}' -> {email}")

    return email


def ensure_pending_csv_columns():
    if not os.path.exists(PENDING_CSV):
        return

    with open(PENDING_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        existing_fields = reader.fieldnames or []

    updated_fields = list(existing_fields)

    for field in PENDING_FIELDS:
        if field not in updated_fields:
            updated_fields.append(field)

    if updated_fields == existing_fields:
        return

    with open(PENDING_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=updated_fields)
        writer.writeheader()
        writer.writerows(rows)

    print("🧾 Pending CSV columns updated.")


def update_pending_status(ref_code, status, replied_at=None):
    if not os.path.exists(PENDING_CSV):
        return

    ensure_pending_csv_columns()

    with open(PENDING_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or PENDING_FIELDS

    found = False

    for row in rows:
        if row.get("ref") == ref_code:
            old_status = row.get("supplier_status", "")
            row["supplier_status"] = status
            row["last_checked_at"] = now_iso()

            if replied_at:
                row["supplier_replied_at"] = replied_at

            found = True

            print(
                f"🧾 Pending CSV Status Updated | Ref: {ref_code} | "
                f"{old_status} -> {status} | Replied At: {row.get('supplier_replied_at', '')}"
            )

            break

    if not found:
        print(f"⚠️ Pending CSV Status Update Failed | Ref not found: {ref_code}")
        return

    with open(PENDING_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_structured_rfq_items(body_upper):
    """
    Layer 2 extractor for explicit customer inquiry formats.

    Supports examples:
    - Brand : SMC / Part No. : MXQ8-20 / Quantity : 2
    - PANASONIC SENSOR / P/N: MTNS000148AA / QTY: 5 PCS
    - Burkert ID : 199983 / 5PCS
    - Model : CJ2M-CPU32 / Qty : 1 PC
    - Part No. : ABC123 / Quantity : 2

    Important:
    - Brand is preserved when customer clearly provides it.
    - Unknown/not-found parts are routed later to technical, not supplier RFQ.
    """

    rfq_items = []
    existing_norms = set()

    BRAND_WORDS = (
        "OMRON|SMC|BURKERT|BÜRKERT|KEYENCE|FESTO|SICK|IFM|PARKER|ABB|SIEMENS|"
        "PANASONIC|THK|LOCTITE|MITSUBISHI|SCHNEIDER|CKD|AIRTAC|LEGRIS|PISCO|"
        "YASKAWA|DELTA|FUJI|IDEC|PATLITE|HONEYWELL|EMERSON|DANFOSS|EATON"
    )

    def add_item(brand, part_no, qty, source):
        brand = str(brand or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")
        part_no = str(part_no or "").strip().upper()
        part_no = re.sub(r"\s+", " ", part_no)
        qty = int(qty or 1)
        norm = normalize_part(part_no)

        if not norm or len(norm) < 4:
            return

        if norm in existing_norms:
            return

        existing_norms.add(norm)

        rfq_items.append({
            "brand": brand,
            "part_no": part_no,
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
        r'BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+'
        r'ITEM\s*:\s*([A-Z0-9\s\-\(\)\+\/]+?)\s+'
        r'MODEL\s*:\s*([A-Z0-9\-\+\._/ ]+?)\s+'
        r'(?:QTY|QUANTITY)\s*:\s*(\d+)',
        re.I | re.S
    )

    for brand, _item_name, model, qty in pattern_brand_item_model.findall(body_upper):
        add_item(brand, model, qty, "LAYER2_BRAND_ITEM_MODEL_QTY")

    # Format:
    # Brand : SMC
    # Part No. : MXQ8-20
    # Quantity : 2
    pattern_with_brand = re.compile(
        r'BRAND\s*:\s*([A-Z0-9\s\-/]+?)\s+'
        r'(?:PART\s*NO\.?|P/N|PN|P\.N\.?|MODEL|ID)\s*:\s*([A-Z0-9\-_/\+\. ]+?)\s+'
        r'(?:QTY|QUANTITY)\s*:\s*(\d+)',
        re.I | re.S
    )

    for brand, part_no, qty in pattern_with_brand.findall(body_upper):
        add_item(brand, part_no, qty, "LAYER2_WITH_BRAND")

    # Format:
    # PANASONIC SENSOR
    # P/N: MTNS000148AA
    # QTY: 5 PCS
    pattern_brand_pn_qty = re.compile(
        rf'\b({BRAND_WORDS})\b[^\n\r]{{0,80}}?\s+'
        r'(?:P/N|PN|P\.N\.?|PART\s*NO\.?|MODEL|ID)\s*:\s*([A-Z0-9\-_/\+\. ]+?)\s+'
        r'(?:QTY|QUANTITY)\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?',
        re.I | re.S
    )

    for brand, part_no, qty in pattern_brand_pn_qty.findall(body_upper):
        add_item(brand, part_no, qty, "LAYER2_BRAND_PN_QTY")

    # Format:
    # Burkert ID : 199983
    # 5PCS
    id_pattern = re.compile(
        rf'\b({BRAND_WORDS})\s+'
        r'(?:ID|NO|PART|MODEL)?\s*[:#]?\s*([A-Z0-9\-_/\+\. ]+?)'
        r'(?:.*?)'
        r'(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)',
        re.I | re.S
    )

    for brand, part_no, qty in id_pattern.findall(body_upper):
        # Avoid treating words like SENSOR / PLC as part no when P/N format already captured.
        part_no_clean = part_no.strip().upper()
        if normalize_part(part_no_clean) in {"SENSOR", "PLC", "VALVE", "GREASE", "LUBRICANT"}:
            continue
        add_item(brand, part_no_clean, qty, "LAYER2_BRAND_ID_FORMAT")

    # Format:
    # P/N: MTNS000148AA
    # QTY: 5 PCS
    pattern_without_brand = re.compile(
        r'(?:PART\s*NO\.?|P/N|PN|P\.N\.?|MODEL|ID)\s*:\s*([A-Z0-9\-_/\+\. ]+?)\s+'
        r'(?:QTY|QUANTITY)\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?',
        re.I | re.S
    )

    for part_no, qty in pattern_without_brand.findall(body_upper):
        add_item("UNKNOWN", part_no, qty, "LAYER2_NO_BRAND_DEFAULT")

    # Format:
    # E3Z-T61 Qty:1
    # 3104 10 00 Qty:2
    line_qty_pattern = re.compile(
        r'^\s*([A-Z0-9][A-Z0-9\-_/\+\. ]{2,40}?)\s+QTY\s*:\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|UNITS|NOS)?\s*$',
        re.I | re.M
    )

    for part_no, qty in line_qty_pattern.findall(body_upper):
        add_item("UNKNOWN", part_no, qty, "LAYER2_LINE_QTY_FORMAT")

    return rfq_items

def build_email_table(rows, include_lt=False, is_rfq=False):
    header = "<tr><th>Description</th><th>Qty</th>"

    if is_rfq:
        header += "<th>Unit Price (RM)</th><th>Lead Time</th></tr>"
    else:
        header += "<th>Unit Price (RM)</th>"

        if include_lt:
            header += "<th>Lead Time</th>"

        header += "<th>Sub-Total (RM)</th></tr>"

    table_rows = ""
    grand_total = 0.0

    for r in rows:
        qty = int(r['qty'])

        if is_rfq:
            row_html = f"<tr><td>{r['desc']}</td><td>{qty}</td><td></td><td></td></tr>"
        else:
            price_str = r['price']
            sub_total_str = "[TBC]"

            if price_str != "[TBC]":
                price_val = float(price_str.replace(',', ''))
                sub_total = qty * price_val
                grand_total += sub_total
                sub_total_str = f"{sub_total:,.2f}"
                price_display = f"{price_val:,.2f}"
            else:
                price_display = "[TBC]"

            row_html = f"<tr><td>{r['desc']}</td><td>{qty}</td><td>{price_display}</td>"

            if include_lt:
                row_html += f"<td>{r.get('lt', 'Stock')}</td>"

            row_html += f"<td>{sub_total_str}</td></tr>"

        table_rows += row_html

    footer = ""

    if not is_rfq:
        footer = (
            f"<tr><td colspan='{'4' if include_lt else '3'}' "
            f"style='text-align:right'><strong>TOTAL PRICE:</strong></td>"
            f"<td><strong>RM {grand_total:,.2f}</strong></td></tr>"
        )

    return (
        "<table border='1' cellpadding='5' "
        "style='border-collapse: collapse; min-width: 500px;'>"
        f"{header}{table_rows}{footer}</table>"
    )


def alert_manager_for_overdue_pending(mailbox):
    if not os.path.exists(PENDING_CSV):
        return

    ensure_pending_csv_columns()

    try:
        with open(PENDING_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames or PENDING_FIELDS

        for field in PENDING_FIELDS:
            if field not in fieldnames:
                fieldnames.append(field)

        now = datetime.datetime.now()
        overdue_rows = []
        updated = False

        print("🕒 Checking pending supplier RFQs older than 24 hours...")

        for row in rows:
            ref = row.get("ref", "")
            status = (row.get("supplier_status") or "").upper()
            created_at = row.get("created_at")
            manager_alerted_at = row.get("manager_alerted_at")

            if status != "PENDING":
                continue

            if manager_alerted_at:
                print(f"   ℹ️ Already alerted manager before | Ref: {ref}")
                continue

            try:
                created_dt = datetime.datetime.fromisoformat(created_at)
            except Exception:
                print(f"   ⚠️ Invalid created_at. Cannot check age | Ref: {ref} | created_at: {created_at}")
                continue

            age_hours = (now - created_dt).total_seconds() / 3600

            print(f"   ⏳ Pending Check | Ref: {ref} | Age: {age_hours:.1f} hours")

            if age_hours > 24:
                overdue_rows.append((row, age_hours))
                row["manager_alerted_at"] = now_iso()
                updated = True

        if not overdue_rows:
            print("✅ No overdue pending supplier RFQs found.")
            return

        html_rows = ""

        for row, age_hours in overdue_rows:
            html_rows += (
                "<tr>"
                f"<td>{row.get('ref', '')}</td>"
                f"<td>{row.get('brand', '')}</td>"
                f"<td>{row.get('name', '')}</td>"
                f"<td>{row.get('email', '')}</td>"
                f"<td>{row.get('items', '')}</td>"
                f"<td>{row.get('created_at', '')}</td>"
                f"<td>{age_hours:.1f} hours</td>"
                "</tr>"
            )

        body = (
            "Hi Manager,<br><br>"
            "The following supplier RFQ / manual verification items are still "
            "PENDING for more than 24 hours. Please follow up:"
            "<br><br>"
            "<table border='1' cellpadding='5' style='border-collapse: collapse;'>"
            "<tr>"
            "<th>Ref</th><th>Brand</th><th>Customer</th><th>Customer Email</th>"
            "<th>Items</th><th>Created At</th><th>Pending Time</th>"
            "</tr>"
            f"{html_rows}"
            "</table>"
            f"<br><br>{SIGNATURE}"
        )

        alert = mailbox.new_message()
        alert.to.add(MANAGER_EMAIL)
        alert.subject = "⚠️ Overdue Supplier RFQ Alert - Pending More Than 24 Hours"
        alert.body = body
        alert.body_type = "html"
        alert.send()

        print(f"🚨 Manager Alert Sent")
        print(f"   To: {MANAGER_EMAIL}")
        print(f"   Subject: ⚠️ Overdue Supplier RFQ Alert - Pending More Than 24 Hours")
        print(f"   Overdue Count: {len(overdue_rows)}")

        if updated:
            with open(PENDING_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            print("🧾 Pending CSV updated with manager_alerted_at.")

    except Exception as e:
        print(f"❌ Manager alert error: {e}")


def process_supplier_replies(mailbox):
    if not os.path.exists(PENDING_CSV):
        return

    ensure_pending_csv_columns()

    try:
        print("📬 Checking unread emails for supplier / human replies...")

        messages = list(mailbox.get_messages(limit=10, query='isRead eq false'))

        for msg in messages:
            content = msg.body if msg.body else ""

            ref_match = re.search(r'REQ-\d{8}-[A-Z0-9\-]+', f"{msg.subject} {content}")
            marker_match = re.search(r'\[TABLE_START\](.*?)\[TABLE_END\]', content, re.S)

            if not ref_match:
                continue

            ref_code = ref_match.group(0)

            print("")
            log_line()
            print(f"📬 Supplier Reply Parser Detected Possible Reply")
            print(f"   Ref: {ref_code}")
            print(f"   From: {msg.sender.address}")
            print(f"   Subject: {msg.subject}")

            if not marker_match:
                print(f"   ⚠️ Ref found but [TABLE_START]/[TABLE_END] not found. Skipping.")
                log_line()
                continue

            print(f"   ✅ Reply table marker found.")
            print(f"   Status: Processing supplier / human reply table...")

            update_pending_status(ref_code, "REPLIED", replied_at=now_iso())

            c_name, c_email, raw_items = "Customer", None, None

            with open(PENDING_CSV, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)

                for row in reader:
                    if row.get("ref") == ref_code:
                        c_name = row.get("name") or "Customer"
                        c_email = row.get("email")
                        raw_items = row.get("items")
                        print(f"   Customer Name: {c_name}")
                        print(f"   Customer Email: {c_email}")
                        print(f"   Original Items: {raw_items}")
                        break

            if not c_email or not raw_items:
                print(f"   ❌ Missing customer email or raw items. Cannot send final quotation.")
                log_line()
                continue

            table_area = marker_match.group(1)
            rows_content = re.split(r'<(?:/tr|tr)>|\n', table_area, flags=re.I)
            original_items = [x.split('|') for x in raw_items.split('; ')]

            formatted_rows = []

            for desc, target_qty in original_items:
                found_item = False
                part_id = re.sub(r'\(|\)', '', desc).strip().split(' ')[-1]

                print(f"   🔎 Matching supplier reply row for item: {desc} | Qty: {target_qty}")

                for row_content in rows_content:
                    if not row_content.strip():
                        continue

                    if part_id.upper() not in row_content.upper():
                        continue

                    clean_row = re.sub(r'<[^>]+>', ' ', row_content)
                    clean_row = re.sub(r'\s+', ' ', clean_row).strip()

                    print(f"      Candidate Row: {clean_row}")

                    num_matches = list(re.finditer(r'\b\d[\d\.,]*\b', clean_row))
                    nums = [m.group() for m in num_matches]

                    if len(nums) >= 2:
                        try:
                            qty_idx = next(i for i, v in enumerate(nums) if v == str(target_qty))
                            cost_match = num_matches[qty_idx + 1]
                            cost_val = cost_match.group()

                            sell_price = float(cost_val.replace(',', '')) / 0.8

                            lt_m = re.search(
                                r'(\d+[\-\s]?\d*\s*(?:weeks?|days?|months?)|stock|immediate|ex[\-\s]stock)',
                                clean_row[cost_match.end():],
                                re.I
                            )

                            lt = lt_m.group(0).strip() if lt_m else "Stock"

                            formatted_rows.append({
                                'desc': desc,
                                'qty': target_qty,
                                'price': f"{sell_price:,.2f}",
                                'lt': lt,
                                'pid': part_id
                            })

                            print(f"      ✅ Parsed Cost: RM {cost_val}")
                            print(f"      ✅ Sell Price: RM {sell_price:,.2f}")
                            print(f"      ✅ Lead Time: {lt}")

                            found_item = True
                            break

                        except Exception as e:
                            print(f"      ⚠️ Could not parse row: {e}")
                            continue

                if not found_item:
                    formatted_rows.append({
                        'desc': desc,
                        'qty': target_qty,
                        'price': "[TBC]",
                        'lt': "[TBC]",
                        'pid': part_id
                    })

                    print(f"      ⚠️ No matching supplier row found. Marked as TBC.")

            if formatted_rows:
                update_msg = mailbox.new_message()
                update_msg.to.add(c_email)
                update_msg.subject = f"Update: Quotation for Inquiry {ref_code}"
                update_msg.body = (
                    f"Hi {c_name},<br><br>"
                    f"Please find the updated price and lead time for your inquiry:<br><br>"
                    f"{build_email_table(formatted_rows, True)}<br><br>"
                    f"{SIGNATURE}"
                )
                update_msg.body_type = 'html'
                update_msg.send()

                update_pending_status(ref_code, "CUSTOMER_UPDATED", replied_at=now_iso())

                msg.mark_as_read()

                print(f"📧 Final Quotation Sent")
                print(f"   To: {c_email}")
                print(f"   Customer: {c_name}")
                print(f"   Ref: {ref_code}")
                print(f"   Subject: Update: Quotation for Inquiry {ref_code}")
                print(f"   Items Sent: {len(formatted_rows)}")
                log_line()

    except Exception as e:
        print(f"❌ Supplier Reply Parser Error: {e}")


def load_warehouse_map():
    print("📦 Loading Warehouse Database...")

    stock_db = []

    if not os.path.exists(WAREHOUSE_CSV):
        print(f"❌ Warehouse CSV not found: {WAREHOUSE_CSV}")
        return []

    try:
        with open(WAREHOUSE_CSV, mode='r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader)

            for row in reader:
                if len(row) < 5:
                    continue

                api_id = row[1].strip()
                stock_name = row[2].strip()
                model_no = row[4].strip()

                def add_to_db(val, api_id, is_partial_source=False):
                    if not val or len(val) < 3:
                        return

                    core = re.sub(
                        r'^(SMC|OMRON|BURKERT|LEGRIS)\s+',
                        '',
                        val.upper().strip()
                    )

                    pat = (
                        re.escape(core)
                        .replace(r'\ ', r'[\s\-]*')
                        .replace(r'\-', r'[\s\-]*')
                    )

                    boundary_pat = rf"(?:^|[^a-zA-Z0-9])({pat})(?![a-zA-Z0-9])"

                    stock_db.append({
                        "api_pid": api_id,
                        "regex": boundary_pat,
                        "len": len(core),
                        "is_partial": is_partial_source,
                        "norm": normalize_part(api_id)
                    })

                    base = re.sub(
                        r'[\s\-]+(?:2M|5M|10M|M8|M12|M18|M30)$',
                        '',
                        core,
                        flags=re.I
                    )

                    if base != core and len(base) >= 3:
                        base_pat = (
                            re.escape(base)
                            .replace(r'\ ', r'[\s\-]*')
                            .replace(r'\-', r'[\s\-]*')
                        )

                        boundary_base = rf"(?:^|[^a-zA-Z0-9])({base_pat})(?![a-zA-Z0-9])"

                        stock_db.append({
                            "api_pid": api_id,
                            "regex": boundary_base,
                            "len": len(base),
                            "is_partial": True,
                            "norm": normalize_part(api_id)
                        })

                add_to_db(api_id, api_id)
                add_to_db(stock_name, api_id)
                add_to_db(model_no, api_id)

        stock_db.sort(key=lambda x: x['len'], reverse=True)

        print(f"✅ Loaded {len(stock_db)} warehouse patterns.")

    except Exception as e:
        print(f"Load Error: {e}")

    return stock_db


STOCK_DB = load_warehouse_map()


def process_latest_inquiry():
    acc = Account(
        (os.getenv('MICROSOFT_CLIENT_ID'), os.getenv('MICROSOFT_CLIENT_SECRET')),
        auth_flow_type='credentials',
        tenant_id=os.getenv('MICROSOFT_TENANT_ID')
    )

    if not acc.authenticate():
        print("❌ Microsoft account authentication failed.")
        return

    mailbox = acc.mailbox(resource='evon@robomatics.sg')

    process_supplier_replies(mailbox)
    alert_manager_for_overdue_pending(mailbox)

    try:
        print("📥 Checking unread customer inquiry emails...")

        messages = list(mailbox.get_messages(limit=3, query='isRead eq false'))

        for msg in messages:
            sender_email = msg.sender.address.lower()
            raw_body = msg.body if msg.body else ""
            body_clean = clean_email_body(raw_body)
            body_upper = body_clean.upper()

            internal_sender = "robomatics.sg" in sender_email
            reply_email = "RE:" in msg.subject.upper()
            inquiry_like = is_inquiry_like(msg.subject, body_clean)

            if internal_sender and not reply_email and not inquiry_like:
                print("ℹ️ Internal non-inquiry email skipped and marked as read.")
                print(f"   From: {msg.sender.address}")
                print(f"   Subject: {msg.subject}")
                msg.mark_as_read()
                continue

            if internal_sender and inquiry_like:
                print("📩 Internal inquiry-like email detected. Processing instead of skipping.")
                print(f"   From: {msg.sender.address}")
                print(f"   Subject: {msg.subject}")

            print("")
            log_line()
            print(f"📩 New Inquiry Detected")
            print(f"   From: {msg.sender.address}")
            print(f"   Sender Name: {msg.sender.name}")
            print(f"   Subject: {msg.subject}")

            c_name = msg.sender.name if msg.sender.name else "Customer"
            c_email = msg.sender.address

            print(f"--- DEBUG RAW BODY START ---")
            print(body_clean)
            print(f"--- DEBUG RAW BODY END ---")
            print(f"📊 Database Pattern Count: {len(STOCK_DB)}")

            matches = []

            for item in STOCK_DB:
                for m in re.finditer(item['regex'], body_upper):
                    matches.append({
                        "start": m.start(1),
                        "end": m.end(1),
                        "matched_text": m.group(1),
                        "item": item
                    })

            matches.sort(key=lambda x: x['start'])

            final_list = []
            last_end = -1
            has_partial = False

            for m in matches:
                if m['start'] >= last_end:
                    final_list.append(m)
                    last_end = m['end']

                    if m['item']['is_partial']:
                        has_partial = True

                    print(
                        f"   🔎 Layer 1 Stock Match Found | "
                        f"API PID: {m['item']['api_pid']} | "
                        f"Matched Text: {m['matched_text']} | "
                        f"{'Partial' if m['item']['is_partial'] else 'Exact/Normal'}"
                    )

            quote_items = []
            detected_parts_norm = set()

            for m in final_list:
                detected_parts_norm.add(normalize_part(m['item']['api_pid']))
                detected_parts_norm.add(normalize_part(m['matched_text']))

                q_match = re.search(
                    r'(?:QTY|QUANTITY|X|:)\s*(\d+)\s*(?:PCS|PC|PCE|UNIT|NOS)?',
                    body_upper[m['end']: m['end'] + 80]
                )

                qty = int(q_match.group(1)) if q_match else 1

                quote_items.append({
                    "data": m['item'],
                    "qty": qty,
                    "matched_text": m.get("matched_text", "")
                })

                print(f"      Qty Detected: {qty}")

            structured_items = extract_structured_rfq_items(body_upper)
            missing_layer2_items = []

            if structured_items:
                print(f"🧩 Layer 2 Structured RFQ Extraction Found: {len(structured_items)} item(s)")

            if structured_items:
                structured_norms = {x["norm"] for x in structured_items}
                filtered_quote_items = []

                for qi in quote_items:
                    qi_api_norm = normalize_part(qi.get("data", {}).get("api_pid", ""))
                    qi_match_norm = normalize_part(qi.get("matched_text", ""))

                    if qi_api_norm in structured_norms or qi_match_norm in structured_norms:
                        filtered_quote_items.append(qi)
                    else:
                        print(
                            f"   ⚠️ Ignoring broad Layer 1 match because explicit P/N/Model was detected | "
                            f"API PID: {qi.get('data', {}).get('api_pid', '')} | "
                            f"Matched Text: {qi.get('matched_text', '')}"
                        )

                quote_items = filtered_quote_items

            for rfq in structured_items:
                if rfq["norm"] not in detected_parts_norm:
                    print(
                        f"   🆕 Layer 2 Missing Part Detected | "
                        f"Brand: {rfq['brand']} | "
                        f"Part No: {rfq['part_no']} | "
                        f"Qty: {rfq['qty']} | "
                        f"Source: {rfq['source']}"
                    )

                    if rfq["brand"] == "UNKNOWN":
                        print("      ⚠️ Customer did not specify brand. Routing will use DEFAULT.")

                    missing_layer2_items.append(rfq)

            if not quote_items and not missing_layer2_items:
                print("ℹ️ No parts detected in this email. No reply sent.")
                log_line()
                continue

            formatted_initial_rows = []
            tbc_by_brand = {}
            non_standard_items = []

            auth = HTTPBasicAuth(
                os.getenv('OBM_API_KEY'),
                os.getenv('OBM_API_SECRET')
            )

            base = os.getenv('OBM_API_URL').rstrip('/')

            for itm in quote_items:
                api_id = itm['data']['api_pid']

                try:
                    print(f"⚙️ Checking OBM API for: {api_id}")

                    obm = requests.get(
                        f"{base}/GetProduct",
                        auth=auth,
                        params={"pid": api_id},
                        verify=False
                    ).json()

                    p_res = requests.get(
                        f"{base}/GetPurProductPrice",
                        auth=auth,
                        params={"pid": api_id},
                        verify=False
                    ).json()

                    brand = obm.get('brand', 'UNKNOWN').upper()
                    pn = obm.get('product_name', '').strip()
                    model = obm.get('model', '').strip()

                    full_desc = f"{brand} {pn}"

                    if model and model.upper() not in pn.upper():
                        full_desc += f" ({model})"

                    cost = float(p_res.get('unit_price', {}).get('price') or 0)
                    stock_avail = float(obm.get('stock_qty', 0))

                    print(f"   API Product: {full_desc}")
                    print(f"   Brand: {brand}")
                    print(f"   Stock Available: {stock_avail}")
                    print(f"   Cost: RM {cost}")
                    print(f"   Customer Qty: {itm['qty']}")

                    if stock_avail >= itm['qty'] and cost > 0:
                        sell_price = cost / 0.8

                        formatted_initial_rows.append({
                            'desc': full_desc,
                            'qty': itm['qty'],
                            'price': f"{sell_price:,.2f}",
                            'lt': "Ex-Stock",
                            'pid': api_id
                        })

                        print(f"   ✅ Stock available. Customer initial price: RM {sell_price:,.2f}")

                    else:
                        formatted_initial_rows.append({
                            'desc': full_desc,
                            'qty': itm['qty'],
                            'price': "[TBC]",
                            'lt': "[TBC]",
                            'pid': api_id
                        })

                        if brand not in tbc_by_brand:
                            tbc_by_brand[brand] = []

                        tbc_by_brand[brand].append({
                            "desc": full_desc,
                            "qty": itm['qty']
                        })

                        print(f"   ⚠️ Stock/cost unavailable. Added to manual verification queue.")

                except Exception as e:
                    print(f"   ❌ API Failure for {api_id}: {e}")

            for missing in missing_layer2_items:
                brand = missing.get("brand") or "UNKNOWN"
                part_no = missing["part_no"]

                if brand == "UNKNOWN":
                    desc = f"UNKNOWN BRAND {part_no}"
                else:
                    desc = f"{brand} {part_no}"

                formatted_initial_rows.append({
                    'desc': desc,
                    'qty': missing['qty'],
                    'price': "[TBC]",
                    'lt': "[TBC]",
                    'pid': part_no
                })

                non_standard_items.append({
                    "brand": brand,
                    "part_no": part_no,
                    "qty": missing['qty'],
                    "desc": desc,
                    "reason": "Not found in warehouse / OBM"
                })

                print(f"   🧩 Non-standard item routed to technical verification.")
                print(f"      Brand: {brand}")
                print(f"      Description: {desc}")
                print(f"      Qty: {missing['qty']}")

            if formatted_initial_rows:
                remark = ""

                if has_partial:
                    remark += (
                        "<br><span style='color:red;'>"
                        "⚠️ Remark: Some items were partially matched. "
                        "Please re-confirm specifications before purchase."
                        "</span>"
                    )

                if missing_layer2_items:
                    remark += (
                        "<br><span style='color:red;'>"
                        "⚠️ Remark: Some items were not found in system and have been sent "
                        "for manual verification."
                        "</span>"
                    )

                cr = msg.reply()

                cr.body = (
                    f"Hi {c_name},<br><br>"
                    f"Thank you for your inquiry. Here is the initial status of your items:"
                    f"<br><br>{build_email_table(formatted_initial_rows, True)}"
                    f"{remark}<br><br>{SIGNATURE}"
                )

                cr.body_type = 'html'
                cr.send()

                msg.mark_as_read()

                print(f"✅ Initial Reply Sent")
                print(f"   To: {c_email}")
                print(f"   Customer: {c_name}")
                print(f"   Original Subject: {msg.subject}")
                print(f"   Items in Initial Reply: {len(formatted_initial_rows)}")

                if non_standard_items:
                    try:
                        print("")
                        print("🧩 Routing non-standard items to technical team...")

                        handle_non_standard_items(
                            customer_name=c_name,
                            customer_contact=c_email,
                            channel="EMAIL",
                            items=non_standard_items,
                            source_message=body_clean
                        )

                        print("✅ Non-standard items routed successfully.")

                    except Exception as e:
                        print(f"❌ Non-standard routing failed: {e}")

                try:
                    quote_response = create_obm_quotation_from_inquiry(
                        email_body=body_clean,
                        items=formatted_initial_rows,
                        customer_name=c_name,
                        customer_email=c_email,
                        source_subject=msg.subject,
                        mailbox=mailbox
                    )

                    if quote_response:
                        print("🧾 OBM quotation attempt completed.")
                        print(f"   Quote No: {quote_response.get('quote_no', '')}")
                        print(f"   API Status: {quote_response.get('api_status', '')}")
                        print(f"   Error: {quote_response.get('error', '')}")
                        print(f"   Message: {quote_response.get('error_msg', '')}")

                except Exception as e:
                    print(f"❌ OBM quotation integration error: {e}")

                for brd, items in tbc_by_brand.items():
                    brd = str(brd or "UNKNOWN").strip().upper()

                    ref_brand = brd if brd else "UNKNOWN"
                    ref = f"REQ-{datetime.datetime.now().strftime('%Y%m%d')}-{ref_brand}-{gen_unique_id()}"

                    item_log = "; ".join([
                        f"{i['desc']}|{i['qty']}" for i in items
                    ])

                    file_exists = os.path.exists(PENDING_CSV)

                    with open(PENDING_CSV, 'a', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=PENDING_FIELDS)

                        if not file_exists:
                            writer.writeheader()

                        writer.writerow({
                            "ref": ref,
                            "name": c_name,
                            "email": c_email,
                            "brand": brd,
                            "items": item_log,
                            "created_at": now_iso(),
                            "supplier_status": "PENDING",
                            "supplier_replied_at": "",
                            "last_checked_at": now_iso(),
                            "manager_alerted_at": ""
                        })

                    supplier_email = get_routing_email(brd)

                    sm = mailbox.new_message()
                    sm.to.add(supplier_email)

                    sm.subject = f"[{brd}] Inquiry / Manual Verification - Ref: {ref}"

                    rfq_table = build_email_table(items, is_rfq=True)

                    sm.body = (
                        f"Hi,<br><br>"
                        f"Please quote / verify Price and Lead Time for the following items:"
                        f"<br><br>"
                        f"[TABLE_START]{rfq_table}[TABLE_END]"
                        f"<br><br>"
                        f"Ref: {ref}<br><br>"
                        f"{SIGNATURE}"
                    )

                    sm.body_type = 'html'
                    sm.send()

                    print(f"📧 RFQ / Manual Verification Sent")
                    print(f"   To: {supplier_email}")
                    print(f"   Brand: {brd}")
                    print(f"   Ref: {ref}")
                    print(f"   Subject: [{brd}] Inquiry / Manual Verification - Ref: {ref}")
                    print(f"   Customer: {c_name}")
                    print(f"   Customer Email: {c_email}")
                    print(f"   Items:")
                    for i in items:
                        print(f"      - {i['desc']} | Qty: {i['qty']}")

                log_line()

    except Exception as e:
        print(f"❌ Core Error: {e}")


if __name__ == "__main__":
    print(f"🚀 AutoClaw {VERSION} Active.")
    print(f"📁 Warehouse CSV: {WAREHOUSE_CSV}")
    print(f"📁 Pending CSV: {PENDING_CSV}")
    print(f"👨‍💼 Manager Email: {MANAGER_EMAIL}")
    print(f"🧪 Testing Routing Email: {TEST_ROUTING_EMAIL}")
    log_line()

    while True:
        try:
            process_latest_inquiry()

        except requests.exceptions.ConnectionError:
            print("🌐 Network issues detected. Retrying in 60 seconds...")

        except Exception as e:
            print(f"⚠️ Unexpected error: {e}")

        print("⏳ Sleeping 30 seconds before next check...")
        log_line()
        time.sleep(30)