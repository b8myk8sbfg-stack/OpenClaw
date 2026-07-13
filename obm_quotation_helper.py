import os
import re
import csv
import json
import datetime
import requests
import urllib3
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

urllib3.disable_warnings()
load_dotenv()

OBM_API_URL = os.getenv("OBM_API_URL", "").rstrip("/")
OBM_API_KEY = os.getenv("OBM_API_KEY")
OBM_API_SECRET = os.getenv("OBM_API_SECRET")

auth = HTTPBasicAuth(OBM_API_KEY, OBM_API_SECRET)

CUSTOMER_CSV = "/Users/evon/OpenClaw/Robomatics_Customer_Listing.csv"
WAREHOUSE_CSV = "/Users/evon/OpenClaw/Robomatics_Stock_List.csv"
QUOTATION_LOG_CSV = "/Users/evon/OpenClaw/quotation_creation_log.csv"

DEFAULT_ITEM_UNIT = "PCE"
DEFAULT_TAX_CODE = "SS0"
DEFAULT_TAX_RATE = 0
DEFAULT_CURRENCY = "RM"
DEFAULT_STATUS = "W"
DEFAULT_VALID_DAYS = 30

SKIP_MISSING_ITEMS = True
NEW_STOCK_PID = "NEW"
MANAGER_EMAIL = "stephen@robomatics.sg"

KNOWN_BRANDS_FOR_NEW_PID = {
    "OMRON", "SMC", "BURKERT", "BÜRKERT", "LEGRIS", "PANASONIC", "PISCO",
    "THK", "LOCTITE", "KEYENCE", "FESTO", "SICK", "IFM", "PARKER", "ABB", "SIEMENS",
    "ALLEN BRADLEY", "NITTO KOHKI", "CKD", "KOGANEI", "AIRTAC", "YASKAWA",
}


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.datetime.now().strftime("%d/%m/%Y")


def valid_date():
    return (
        datetime.datetime.now() + datetime.timedelta(days=DEFAULT_VALID_DAYS)
    ).strftime("%d/%m/%Y")


def normalize(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def normalize_company(name):
    name = str(name or "").upper()
    name = re.sub(r"[^A-Z0-9 ]", " ", name)
    name = re.sub(r"\bSDN\b\s*\bBHD\b", "SDN BHD", name)
    name = re.sub(r"\bPTE\b\s*\bLTD\b", "PTE LTD", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def clean_company_candidate(value):
    value = str(value or "").strip()
    value = re.sub(r"^[,;:\-\s]+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def extract_company_from_text(email_body):
    text = str(email_body or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z0-9#]+;", " ", text, flags=re.I)
    text = text.replace("–", "-")
    text = re.sub(r"\s+", " ", text).strip()

    # Capture compact company names ending with SDN BHD / PTE LTD / BERHAD / LTD.
    # Limit to a few tokens before suffix so a flattened email does not become one huge company name.
    suffix = r"(?:SDN\.?\s*BHD\.?|PTE\.?\s*LTD\.?|BERHAD|LTD\.?)"
    pattern = re.compile(
        rf"((?:[A-Z0-9&()./\-]+\s+){{0,7}}{suffix})",
        re.I
    )

    candidates = []
    for m in pattern.finditer(text):
        cand = clean_company_candidate(m.group(1))
        norm = normalize_company(cand)
        if not norm:
            continue
        # Reject obviously bad captured fragments.
        if len(norm.split()) > 10:
            continue
        if any(bad in norm for bad in ["PLEASE QUOTE", "SHOULD YOU", "THANKS", "REGARDS", "MOBILE", "EMAIL"]):
            # Keep only trailing part after these words if possible.
            pass
        candidates.append(cand)

    if candidates:
        # Prefer candidate with company suffix and reasonable length, usually last company in signature.
        candidates = sorted(candidates, key=lambda x: (len(normalize_company(x).split()), len(x)))
        return candidates[0]

    return None


def extract_customer(email_body):
    print("🔍 [OBM] Extracting customer from inquiry body...")

    lines = [line.strip() for line in str(email_body or "").splitlines() if line.strip()]

    name = None
    company = None
    phone = None
    email = None

    email_match = re.search(r"[\w\.-]+@[\w\.-]+", str(email_body or ""))
    if email_match:
        email = email_match.group(0)

    for i, line in enumerate(lines):
        if re.search(r"(thanks|regards|best regards|thanks & regards)", line, re.I):
            for j in range(i + 1, min(i + 6, len(lines))):
                candidate = lines[j]

                if candidate.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
                    continue

                if len(candidate.split()) <= 5:
                    name = candidate
                    break
            break

    # First try line-based extraction. This works for normal signatures.
    for line in lines:
        if re.search(r"(SDN\.?\s*BHD\.?|PTE\.?\s*LTD\.?|BERHAD)", line, re.I):
            candidate = extract_company_from_text(line) or line
            company = candidate
            break

    # If email HTML/text was flattened into one long line, extract company from whole body.
    if not company or len(str(company).split()) > 10:
        company = extract_company_from_text(email_body)

    phone_match = re.search(r"(\+?\d[\d\s\-]{7,})", str(email_body or ""))
    if phone_match:
        phone = phone_match.group(1).strip()

    print(f"   Name: {name}")
    print(f"   Company: {company}")
    print(f"   Normalized Company: {normalize_company(company)}")
    print(f"   Email: {email}")
    print(f"   Phone: {phone}")

    return {
        "name": name,
        "company": company,
        "email": email,
        "phone": phone
    }


def get_customer_no(company):
    print("🔎 [OBM] Searching customer CSV...")

    company_norm = normalize_company(company)

    if not company_norm:
        print("   ❌ No company extracted from inquiry.")
        return None, None

    if not os.path.exists(CUSTOMER_CSV):
        print(f"   ❌ Customer CSV not found: {CUSTOMER_CSV}")
        return None, None

    with open(CUSTOMER_CSV, encoding="utf-8-sig") as f:
        reader = csv.reader(f)

        for row in reader:
            if len(row) <= 3:
                continue

            customer_no = row[2].strip()
            customer_name = row[3].strip()

            if not customer_no or not customer_name:
                continue

            csv_name = normalize_company(customer_name)

            if not csv_name:
                continue

            if company_norm == csv_name:
                print(f"   ✅ Exact match: {customer_name} → {customer_no}")
                return customer_no, customer_name

        f.seek(0)
        reader = csv.reader(f)

        for row in reader:
            if len(row) <= 3:
                continue

            customer_no = row[2].strip()
            customer_name = row[3].strip()

            if not customer_no or not customer_name:
                continue

            csv_name = normalize_company(customer_name)

            if not csv_name:
                continue

            # Safer partial matching: require meaningful overlap, not blank/very short values.
            if len(company_norm) >= 8 and len(csv_name) >= 8 and (company_norm in csv_name or csv_name in company_norm):
                print(f"   ✅ Partial match: {customer_name} → {customer_no}")
                return customer_no, customer_name

    print("   ❌ Customer not found in local CSV.")
    return None, None


def load_stock():
    print("📦 [OBM] Loading warehouse CSV for stock ID lookup...")

    data = []

    if not os.path.exists(WAREHOUSE_CSV):
        print(f"   ❌ Warehouse CSV not found: {WAREHOUSE_CSV}")
        return data

    with open(WAREHOUSE_CSV, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)

        for row in reader:
            if len(row) >= 5:
                data.append({
                    "pid": row[1].strip(),
                    "stock": row[2].strip(),
                    "model": row[4].strip(),
                    "n_pid": normalize(row[1]),
                    "n_stock": normalize(row[2]),
                    "n_model": normalize(row[4])
                })

    print(f"   Loaded: {len(data)} rows")
    return data


STOCK = load_stock()


def find_pid(part):
    key = normalize(part)

    if not key:
        return None

    for row in STOCK:
        if key == row["n_pid"] or key == row["n_stock"] or key == row["n_model"]:
            print(f"   ✅ Stock match: {part} → {row['pid']}")
            return row["pid"]

    print(f"   ⚠️ No stock match: {part}")
    return None


def extract_brand_from_item(item, customer_part=None):
    brand = str(item.get("brand") or "").strip().upper().replace("BÜRKERT", "BURKERT")
    if brand and brand != "UNKNOWN":
        return brand

    desc = str(item.get("desc") or item.get("description") or "").strip().upper()
    for known in KNOWN_BRANDS_FOR_NEW_PID:
        known_norm = known.replace("BÜRKERT", "BURKERT")
        if desc.startswith(f"{known_norm} "):
            return known_norm

    return ""


def should_use_new_stock_pid(item, customer_part=None):
    """Use OBM placeholder stock NEW for valid known-brand parts not in warehouse."""
    brand = extract_brand_from_item(item, customer_part)
    return brand in {b.replace("BÜRKERT", "BURKERT") for b in KNOWN_BRANDS_FOR_NEW_PID}


def new_stock_pid_available():
    return any(row.get("n_pid") == "NEW" or row.get("pid") == NEW_STOCK_PID for row in STOCK)


def resolve_stock_pid(part, item=None):
    pid = find_pid(part)
    if pid:
        return pid

    if item and should_use_new_stock_pid(item, part) and new_stock_pid_available():
        print(
            f"   ✅ Using {NEW_STOCK_PID} stock code for known-brand part not in warehouse: {part}"
        )
        return NEW_STOCK_PID

    return None


def parse_price(value):
    if value in [None, "", "[TBC]"]:
        return 0.0

    try:
        return float(str(value).replace(",", "").replace("RM", "").strip())
    except Exception:
        return 0.0


def clean_item_for_obm(item):
    desc = str(item.get("desc") or item.get("description") or "").strip()
    qty = int(item.get("qty") or 1)

    customer_part = (
        item.get("pid")
        or item.get("part_no")
        or item.get("part")
        or desc.split(" ")[-1]
    )

    price = parse_price(item.get("price"))

    brand = str(item.get("brand") or "").strip().upper().replace("BÜRKERT", "BURKERT")
    if not brand or brand == "UNKNOWN":
        brand = extract_brand_from_item(item, customer_part)

    return {
        "desc": desc,
        "qty": qty,
        "customer_part": str(customer_part).strip(),
        "price": price,
        "brand": brand,
        "raw": item
    }


def build_payload(cust_no, items):
    print("🧾 [OBM] Building CreateQuotation payload...")

    lines = []
    skipped_items = []

    for item in items:
        clean = clean_item_for_obm(item)

        desc = clean["desc"]
        qty = clean["qty"]
        customer_part = clean["customer_part"]
        price = clean["price"]

        pid = resolve_stock_pid(customer_part, item)

        if not pid:
            skipped_items.append({
                "desc": desc,
                "customer_part": customer_part,
                "qty": qty,
                "reason": "Stock ID not found in Robomatics_Stock_List.csv"
            })

            if SKIP_MISSING_ITEMS:
                print(f"   ❌ Skipped unresolved item: {desc} | Customer Part: {customer_part}")
                continue

            pid = customer_part

        amount = qty * price
        tax_amount = amount * (DEFAULT_TAX_RATE / 100)

        line = {
            "s_item_pid": pid,
            "s_item_desc": desc,
            "s_item_qty": qty,
            "s_item_unit": DEFAULT_ITEM_UNIT,
            "s_item_uprice": price,
            "s_item_disc": 0,
            "s_item_tax": DEFAULT_TAX_RATE,
            "s_item_taxcode": DEFAULT_TAX_CODE,
            "s_item_taxrate": DEFAULT_TAX_RATE,
            "s_item_taxamt": tax_amount,
            "s_item_total": amount
        }

        lines.append(line)

        print(
            f"   ✔ Quotation line: {desc} | Stock ID: {pid} | "
            f"Qty: {qty} | Unit: {DEFAULT_ITEM_UNIT} | Price: {price:.2f}"
        )

    payload = {
        "s_custno": cust_no,
        "s_date": today(),
        "valid_date": valid_date(),
        "currency_code": DEFAULT_CURRENCY,
        "status": DEFAULT_STATUS,
        "remarks": (
            "Created by AutoClaw automation. "
            "Known-brand parts not in warehouse use stock code NEW; "
            "other unresolved stock IDs were skipped."
        ),
        "items": lines
    }

    print("\n📦 [OBM] CreateQuotation payload:")
    print(json.dumps(payload, indent=2))

    return payload, skipped_items


def send_create_quotation(payload):
    print("🚀 [OBM] Sending CreateQuotation...")

    try:
        response = requests.post(
            f"{OBM_API_URL}/CreateQuotation",
            auth=auth,
            json=payload,
            verify=False
        )

        try:
            data = response.json()
        except Exception:
            print("❌ [OBM] Non-JSON response:")
            print(response.text)
            return {
                "error": "NON_JSON",
                "error_msg": response.text
            }

        print("\n📨 [OBM] CreateQuotation response:")
        print(json.dumps(data, indent=2))

        return data

    except Exception as e:
        print(f"❌ [OBM] CreateQuotation request error: {e}")
        return {
            "error": "REQUEST_ERROR",
            "error_msg": str(e)
        }


def ensure_log_csv():
    fields = [
        "timestamp",
        "source_subject",
        "customer_name",
        "customer_email",
        "extracted_contact_name",
        "extracted_company",
        "matched_customer_no",
        "matched_customer_name",
        "quote_no",
        "api_status",
        "api_error",
        "api_error_msg",
        "created_item_count",
        "skipped_item_count",
        "skipped_items_json"
    ]

    if not os.path.exists(QUOTATION_LOG_CSV):
        with open(QUOTATION_LOG_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

    return fields


def append_quotation_log(
    source_subject,
    customer_name,
    customer_email,
    extracted,
    matched_customer_no,
    matched_customer_name,
    quote_response,
    created_item_count,
    skipped_items
):
    fields = ensure_log_csv()

    row = {
        "timestamp": now_iso(),
        "source_subject": source_subject or "",
        "customer_name": customer_name or "",
        "customer_email": customer_email or "",
        "extracted_contact_name": extracted.get("name") or "",
        "extracted_company": extracted.get("company") or "",
        "matched_customer_no": matched_customer_no or "",
        "matched_customer_name": matched_customer_name or "",
        "quote_no": quote_response.get("quote_no", "") if quote_response else "",
        "api_status": quote_response.get("api_status", "") if quote_response else "",
        "api_error": quote_response.get("error", "") if quote_response else "",
        "api_error_msg": quote_response.get("error_msg", "") if quote_response else "",
        "created_item_count": created_item_count,
        "skipped_item_count": len(skipped_items),
        "skipped_items_json": json.dumps(skipped_items, ensure_ascii=False)
    }

    with open(QUOTATION_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow(row)

    print(f"🧾 [OBM] Quotation log saved: {QUOTATION_LOG_CSV}")


def notify_manager(mailbox, subject, body):
    if not mailbox:
        print("⚠️ [OBM] Mailbox not provided. Manager notification skipped.")
        return

    try:
        msg = mailbox.new_message()
        msg.to.add(MANAGER_EMAIL)
        msg.subject = subject
        msg.body = body
        msg.body_type = "html"
        msg.send()

        print(f"📧 [OBM] Manager notified: {MANAGER_EMAIL}")

    except Exception as e:
        print(f"❌ [OBM] Manager notification failed: {e}")


def create_obm_quotation_from_inquiry(
    email_body,
    items,
    customer_name=None,
    customer_email=None,
    source_subject=None,
    mailbox=None
):
    print("")
    print("=" * 90)
    print("🧾 [OBM] START AUTO QUOTATION FROM REAL INQUIRY")
    print("=" * 90)

    if not OBM_API_URL or not OBM_API_KEY or not OBM_API_SECRET:
        print("❌ [OBM] Missing API config in .env")
        return None

    extracted = extract_customer(email_body)

    cust_no, matched_customer_name = get_customer_no(extracted.get("company"))

    if not cust_no:
        skipped_items = [
            {
                "desc": item.get("desc", ""),
                "customer_part": item.get("pid") or item.get("part_no") or "",
                "qty": item.get("qty", ""),
                "reason": "Customer not found, quotation not created"
            }
            for item in items
        ]

        append_quotation_log(
            source_subject,
            customer_name,
            customer_email,
            extracted,
            "",
            "",
            {"error": "CUSTOMER_NOT_FOUND", "error_msg": "Customer not found in local CSV"},
            0,
            skipped_items
        )

        notify_manager(
            mailbox,
            "⚠️ AutoClaw OBM Quotation Failed - Customer Not Found",
            (
                "Hi Manager,<br><br>"
                "AutoClaw could not create an OBM quotation because customer was not found."
                "<br><br>"
                f"<strong>Subject:</strong> {source_subject}<br>"
                f"<strong>Customer Email:</strong> {customer_email}<br>"
                f"<strong>Extracted Company:</strong> {extracted.get('company')}<br>"
                f"<strong>Extracted Contact:</strong> {extracted.get('name')}<br><br>"
                "Please verify customer master / customer listing CSV."
            )
        )

        return None

    payload, skipped_items = build_payload(cust_no, items)

    if not payload["items"]:
        append_quotation_log(
            source_subject,
            customer_name,
            customer_email,
            extracted,
            cust_no,
            matched_customer_name,
            {"error": "NO_VALID_ITEMS", "error_msg": "No valid quotation items after PID filtering"},
            0,
            skipped_items
        )

        notify_manager(
            mailbox,
            "⚠️ AutoClaw OBM Quotation Failed - No Valid Items",
            (
                "Hi Manager,<br><br>"
                "AutoClaw could not create an OBM quotation because all items were unresolved."
                "<br><br>"
                f"<strong>Subject:</strong> {source_subject}<br>"
                f"<strong>Customer:</strong> {matched_customer_name} ({cust_no})<br>"
                f"<strong>Customer Email:</strong> {customer_email}<br><br>"
                f"<strong>Skipped Items:</strong><br><pre>{json.dumps(skipped_items, indent=2)}</pre>"
            )
        )

        return None

    quote_response = send_create_quotation(payload)

    append_quotation_log(
        source_subject,
        customer_name,
        customer_email,
        extracted,
        cust_no,
        matched_customer_name,
        quote_response,
        len(payload["items"]),
        skipped_items
    )

    quote_no = quote_response.get("quote_no") if quote_response else None
    api_error = str(quote_response.get("error")) if quote_response else ""

    if skipped_items or api_error != "0":
        notify_manager(
            mailbox,
            "⚠️ AutoClaw OBM Quotation Created / Needs Review",
            (
                "Hi Manager,<br><br>"
                "AutoClaw attempted to create an OBM quotation from a real inquiry."
                "<br><br>"
                f"<strong>Subject:</strong> {source_subject}<br>"
                f"<strong>Customer:</strong> {matched_customer_name} ({cust_no})<br>"
                f"<strong>Customer Email:</strong> {customer_email}<br>"
                f"<strong>Quote No:</strong> {quote_no or ''}<br>"
                f"<strong>API Error:</strong> {quote_response.get('error', '')}<br>"
                f"<strong>API Message:</strong> {quote_response.get('error_msg', '')}<br>"
                f"<strong>Created Item Count:</strong> {len(payload['items'])}<br>"
                f"<strong>Skipped Item Count:</strong> {len(skipped_items)}<br><br>"
                f"<strong>Skipped Items:</strong><br><pre>{json.dumps(skipped_items, indent=2)}</pre>"
                "<br>Please review before sending to customer."
            )
        )

    print("=" * 90)
    print("✅ [OBM] END AUTO QUOTATION FROM REAL INQUIRY")
    print("=" * 90)

    return quote_response