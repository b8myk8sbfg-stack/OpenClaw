import os
import time
import csv
import json
import datetime
from dotenv import load_dotenv
from O365 import Account
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from supplier_whatsapp_config import uses_purchasing_whatsapp

load_dotenv()

SUPPLIER_PENDING_CSV = "/Users/evon/OpenClaw/whatsapp_supplier_pending.csv"
INTERNAL_PURCHASING_EMAIL = "purchasing@robomatics.sg"

# EMAIL = internal purchasing@robomatics.sg
# PURCHASING_WHATSAPP = separate Chrome, purchasing line → external supplier WhatsApp
SUPPLIER_CHANNEL_ROUTING = {
    "OMRON": "PURCHASING_WHATSAPP",
    "FESTO": "EMAIL",
    "PIAB": "EMAIL",
    "BURKERT": "EMAIL",
    "SMC": "EMAIL",
    "UNKNOWN": "EMAIL",
    "DEFAULT": "EMAIL",
}

SUPPLIER_EMAIL_ROUTING = {
    "OMRON": "purchasing@robomatics.sg",
    "BURKERT": "purchasing@robomatics.sg",
    "SMC": "purchasing@robomatics.sg",
    "FESTO": "siuw@jsautomation.com.my",
    "PIAB": "Ang.SengGuan@piabgroup.com",
    "UNKNOWN": "purchasing@robomatics.sg",
    "DEFAULT": "purchasing@robomatics.sg",
}

# Legacy map — external supplier numbers now live in supplier_whatsapp_config.py
SUPPLIER_WHATSAPP_ROUTING = {
    "DEFAULT": "",
}


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def normalize_phone(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def ensure_supplier_pending_csv():
    fields = [
        "created_at",
        "ref",
        "brand",
        "customer_name",
        "customer_contact",
        "customer_phone",
        "supplier_channel",
        "supplier_to",
        "items_json",
        "status",
        "supplier_replied_at",
        "customer_updated_at"
    ]

    if not os.path.exists(SUPPLIER_PENDING_CSV):
        with open(SUPPLIER_PENDING_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
    else:
        with open(SUPPLIER_PENDING_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            existing_fields = reader.fieldnames or []
        if existing_fields != fields:
            with open(SUPPLIER_PENDING_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in fields})

    return fields


def log_supplier_pending(
    ref, brand, customer_name, customer_contact, supplier_channel, supplier_to, items
):
    fields = ensure_supplier_pending_csv()

    customer_phone = normalize_phone(customer_contact)

    row = {
        "created_at": now_iso(),
        "ref": ref,
        "brand": brand,
        "customer_name": customer_name or "",
        "customer_contact": customer_contact or "",
        "customer_phone": customer_phone,
        "supplier_channel": supplier_channel,
        "supplier_to": supplier_to,
        "items_json": json.dumps(items, ensure_ascii=False),
        "status": "PENDING",
        "supplier_replied_at": "",
        "customer_updated_at": ""
    }

    with open(SUPPLIER_PENDING_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow(row)

    print("🧾 [ROUTER] WhatsApp supplier pending log saved.")
    print(f"   Ref: {ref}")
    print(f"   Customer Contact: {customer_contact}")
    print(f"   Customer Phone: {customer_phone}")
    print(f"   Supplier: {supplier_to}")


def get_supplier_channel(brand):
    brand = str(brand or "UNKNOWN").upper().strip()
    if uses_purchasing_whatsapp(brand):
        return "PURCHASING_WHATSAPP"
    return SUPPLIER_CHANNEL_ROUTING.get(
        brand,
        SUPPLIER_CHANNEL_ROUTING["DEFAULT"]
    )


def get_supplier_email(brand):
    brand = str(brand or "UNKNOWN").upper().strip()
    return SUPPLIER_EMAIL_ROUTING.get(
        brand,
        SUPPLIER_EMAIL_ROUTING["DEFAULT"]
    )


def get_supplier_whatsapp(brand):
    brand = str(brand or "UNKNOWN").upper().strip()
    return SUPPLIER_WHATSAPP_ROUTING.get(
        brand,
        SUPPLIER_WHATSAPP_ROUTING["DEFAULT"]
    )


def is_external_supplier(brand):
    """True when RFQ goes to a real external supplier (not internal purchasing only)."""
    brand = str(brand or "UNKNOWN").upper().strip()
    if uses_purchasing_whatsapp(brand):
        return True
    email = get_supplier_email(brand).lower()
    return email != INTERNAL_PURCHASING_EMAIL.lower()


def build_supplier_rfq_text(
    brand,
    items,
    ref,
    customer_name=None,
    customer_contact=None,
    include_customer_info=True,
):
    brand = str(brand or "UNKNOWN").upper().strip()

    msg = f"""Hi,

Please quote the following items:

Brand: {brand}
Ref: {ref}
"""

    if include_customer_info:
        msg += f"Customer: {customer_name or '-'}\nCustomer Contact: {customer_contact or '-'}\n"

    msg += f"""
[REPLY FORMAT - PLEASE COPY & FILL]

Ref: {ref}

"""

    for idx, item in enumerate(items, start=1):
        desc = item.get("desc", "")
        qty = item.get("qty", "")

        msg += f"""{idx}) {desc}
Qty: {qty}
Price:
Lead Time:

"""

    if brand == "PIAB":
        example_item = "Piab vacuum filter (31.16.671)"
        example_price = "SGD78.44 net/unit"
    else:
        example_item = "[any item request to quote]"
        example_price = "RM100"

    msg += f"""[END]

⚠️ Please copy the format above and fill in Price & Lead Time for each item.

Example:

1) {example_item}
Qty: 1
Price: {example_price}
Lead Time: 2 weeks

Thanks.
"""

    return msg


def get_mailbox():
    acc = Account(
        (os.getenv("MICROSOFT_CLIENT_ID"), os.getenv("MICROSOFT_CLIENT_SECRET")),
        auth_flow_type="credentials",
        tenant_id=os.getenv("MICROSOFT_TENANT_ID")
    )

    if not acc.authenticate():
        print("❌ [ROUTER] Microsoft account authentication failed.")
        return None

    return acc.mailbox(resource="evon@robomatics.sg")


def send_supplier_email(brand, items, ref, customer_name=None, customer_contact=None):
    brand = str(brand or "UNKNOWN").upper().strip()
    supplier_email = get_supplier_email(brand)
    external = is_external_supplier(brand)

    mailbox = get_mailbox()

    if not mailbox:
        return False

    supplier_msg = build_supplier_rfq_text(
        brand, items, ref,
        customer_name=customer_name,
        customer_contact=customer_contact,
        include_customer_info=not external,
    )

    sm = mailbox.new_message()
    sm.to.add(supplier_email)
    sm.subject = f"[RFQ] [{brand}] Price Request - Ref: {ref}"
    sm.body = supplier_msg
    sm.body_type = "text"
    sm.send()

    print("✅ [ROUTER] Supplier RFQ sent by Email")
    print(f"   Brand: {brand}")
    print(f"   To: {supplier_email}")
    print(f"   Ref: {ref}")
    if external:
        print("   Customer details: withheld from external supplier")

    if external:
        internal_msg = build_supplier_rfq_text(
            brand, items, ref,
            customer_name=customer_name,
            customer_contact=customer_contact,
            include_customer_info=True,
        )
        sm_copy = mailbox.new_message()
        sm_copy.to.add(INTERNAL_PURCHASING_EMAIL)
        sm_copy.subject = f"[INTERNAL COPY] [RFQ] [{brand}] Ref: {ref}"
        sm_copy.body = internal_msg
        sm_copy.body_type = "text"
        sm_copy.send()

        print("✅ [ROUTER] Internal purchasing copy sent")
        print(f"   To: {INTERNAL_PURCHASING_EMAIL}")
        print("   Customer details: included for internal purchaser")

    return True


def send_purchasing_internal_rfq_copy(
    brand, items, ref, customer_name=None, customer_contact=None
):
    """Email purchasing@ with full customer details after external WhatsApp RFQ."""
    brand = str(brand or "UNKNOWN").upper().strip()
    mailbox = get_mailbox()
    if not mailbox:
        return False

    internal_msg = build_supplier_rfq_text(
        brand, items, ref,
        customer_name=customer_name,
        customer_contact=customer_contact,
        include_customer_info=True,
    )
    sm = mailbox.new_message()
    sm.to.add(INTERNAL_PURCHASING_EMAIL)
    sm.subject = f"[INTERNAL COPY] [RFQ] [{brand}] Ref: {ref}"
    sm.body = internal_msg
    sm.body_type = "text"
    sm.send()

    print("✅ [ROUTER] Internal purchasing copy sent")
    print(f"   To: {INTERNAL_PURCHASING_EMAIL}")
    print(f"   Ref: {ref}")
    return True


def open_whatsapp_chat(driver, phone):
    print(f"🌐 [ROUTER] Opening supplier WhatsApp chat: +{phone}")

    url = f"https://web.whatsapp.com/send?phone={phone}"
    driver.get(url)

    end = time.time() + 90

    while time.time() < end:
        try:
            box = driver.find_elements(
                By.XPATH,
                '//footer//div[@contenteditable="true"]'
            )

            if box:
                print("✅ [ROUTER] Supplier WhatsApp chat opened.")
                return True

        except Exception:
            pass

        time.sleep(3)

    print("❌ [ROUTER] Supplier WhatsApp chat did not open.")
    return False


_SELECT_GROUP_CHAT_JS = """
const hint = String(arguments[0] || '').trim().toLowerCase();
if (!hint) return null;
const side = document.querySelector('#side');
if (!side) return null;

function matchesHint(title, text) {
    const blob = ((title || '') + ' ' + (text || '')).toLowerCase();
    return blob.includes(hint) || hint.includes((title || '').toLowerCase());
}

const rows = side.querySelectorAll(
    '[data-testid="cell-frame-container"], [role="listitem"], [role="row"]'
);
for (const row of rows) {
    const titleEl = row.querySelector('span[title]');
    const title = titleEl ? (titleEl.getAttribute('title') || '').trim() : '';
    const text = (row.innerText || '').trim();
    if (!matchesHint(title, text)) continue;
    const target = row.matches('[data-testid="cell-frame-container"]')
        ? row
        : (row.querySelector('[data-testid="cell-frame-container"]') || row);
    target.scrollIntoView({ block: 'center' });
    target.click();
    return title || text.split('\\n')[0] || null;
}
return null;
"""


def open_whatsapp_group_chat(driver, group_name: str) -> bool:
    """Open a WhatsApp group by searching the chat list (purchasing line)."""
    group_name = str(group_name or "").strip()
    print(f"🌐 [ROUTER] Opening supplier WhatsApp group: {group_name}")

    driver.get("https://web.whatsapp.com")
    time.sleep(2)

    search_selectors = [
        '//div[@id="side"]//div[@contenteditable="true"]',
        '//div[@aria-label="Search input textbox"]',
        '//div[@title="Search input textbox"]',
    ]
    search_box = None
    for selector in search_selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)
            for el in elements:
                if el.is_displayed():
                    search_box = el
                    break
            if search_box:
                break
        except Exception:
            continue

    if not search_box:
        print("❌ [ROUTER] WhatsApp search box not found for group open.")
        return False

    search_box.click()
    time.sleep(0.4)
    for key_combo in (Keys.COMMAND, Keys.CONTROL):
        search_box.send_keys(key_combo, "a")
    search_box.send_keys(Keys.BACKSPACE)
    search_box.send_keys(group_name)
    time.sleep(2.5)

    try:
        opened = driver.execute_script(_SELECT_GROUP_CHAT_JS, group_name)
        if opened:
            time.sleep(2)
            if driver.find_elements(By.XPATH, '//footer//div[@contenteditable="true"]'):
                print(f"✅ [ROUTER] Supplier WhatsApp group opened: {opened}")
                return True
    except Exception as exc:
        print(f"⚠️ [ROUTER] Group search JS failed: {exc}")

    # Fallback: click span title in sidebar
    try:
        results = driver.find_elements(
            By.XPATH,
            f'//div[@id="side"]//span[contains(@title, "{group_name[:20]}")]',
        )
        for result in results:
            driver.execute_script("arguments[0].click();", result)
            time.sleep(2)
            if driver.find_elements(By.XPATH, '//footer//div[@contenteditable="true"]'):
                print(f"✅ [ROUTER] Supplier group opened via title match.")
                return True
    except Exception:
        pass

    print(f"❌ [ROUTER] Could not open WhatsApp group: {group_name}")
    return False


def find_message_box(driver):
    textbox_selectors = [
        '//footer//div[@contenteditable="true"][@role="textbox"]',
        '//footer//div[@contenteditable="true"]',
        '//div[@contenteditable="true"][@role="textbox"]',
        '(//div[@contenteditable="true"])[last()]',
    ]

    for selector in textbox_selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)

            for el in elements:
                if el.is_displayed():
                    return el

        except Exception:
            continue

    return None


def click_send_button(driver, timeout=15):
    print("🔎 [ROUTER] Looking for WhatsApp Send button...")

    end = time.time() + timeout

    send_selectors = [
        '//footer//button[@aria-label="Send"]',
        '//footer//button[@aria-label="Send message"]',
        '//button[@aria-label="Send"]',
        '//button[@aria-label="Send message"]',
        '//footer//*[@data-icon="send"]/ancestor::button',
        '//footer//*[@data-icon="send"]/ancestor::div[@role="button"]',
        '//*[@data-icon="send"]/ancestor::button',
        '//*[@data-icon="send"]/ancestor::div[@role="button"]',
        '//*[@data-icon="send"]',
    ]

    while time.time() < end:
        for selector in send_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)

                for el in elements:
                    if el.is_displayed():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            el
                        )
                        time.sleep(0.3)
                        driver.execute_script("arguments[0].click();", el)
                        print("✅ [ROUTER] WhatsApp message sent by clicking Send.")
                        return True

            except Exception:
                continue

        time.sleep(1)

    return False


def send_whatsapp_message(driver, destination, message):
    """
    Send a WhatsApp message to a phone number or group.
    destination: SupplierDestination, phone str, or group name str prefixed with 'group:'.
    """
    from supplier_whatsapp_config import SupplierDestination

    dest_label = ""
    opened = False

    if isinstance(destination, SupplierDestination):
        dest_label = destination.label
        if destination.is_group:
            opened = open_whatsapp_group_chat(driver, destination.value)
        else:
            opened = open_whatsapp_chat(driver, destination.value)
    else:
        raw = str(destination or "").strip()
        if raw.lower().startswith("group:"):
            name = raw.split(":", 1)[1].strip()
            dest_label = name
            opened = open_whatsapp_group_chat(driver, name)
        else:
            phone = normalize_phone(raw)
            dest_label = f"+{phone}"
            opened = open_whatsapp_chat(driver, phone)

    if not opened:
        print(f"❌ [ROUTER] Could not open WhatsApp destination: {dest_label}")
        return False

    return _type_and_send_whatsapp_message(driver, message)


def send_whatsapp_in_current_driver(driver, phone, message):
    print("📲 [ROUTER] Sending WhatsApp using current browser session...")
    print(f"   To: {phone}")
    return send_whatsapp_message(driver, phone, message)


def _type_and_send_whatsapp_message(driver, message):
    time.sleep(2)

    box = find_message_box(driver)

    if not box:
        print("❌ [ROUTER] WhatsApp message box not found.")
        return False

    try:
        box.click()
        time.sleep(1)

        print("⌨️ [ROUTER] Typing WhatsApp supplier RFQ...")

        lines = message.split("\n")

        for idx, line in enumerate(lines):
            box.send_keys(line)

            if idx != len(lines) - 1:
                box.send_keys(Keys.SHIFT, Keys.ENTER)

        time.sleep(2)

        if click_send_button(driver, timeout=15):
            print("✅ [ROUTER] Supplier WhatsApp RFQ sent.")
            return True

        print("⚠️ [ROUTER] Send button not found. Trying ENTER method...")

        box = find_message_box(driver)

        if box:
            box.click()
            time.sleep(0.5)
            box.send_keys(Keys.ENTER)
            print("✅ [ROUTER] Supplier WhatsApp RFQ sent by ENTER.")
            return True

        print("❌ [ROUTER] Supplier WhatsApp send failed.")
        return False

    except Exception as e:
        print(f"❌ [ROUTER] Supplier WhatsApp send error: {e}")
        return False


def send_supplier_rfq(
    driver, brand, items, ref, customer_name=None, customer_contact=None
):
    brand = str(brand or "UNKNOWN").upper().strip()
    channel = get_supplier_channel(brand)

    print("")
    print("=" * 90)
    print("📡 [ROUTER] SUPPLIER RFQ ROUTING")
    print(f"   Brand: {brand}")
    print(f"   Channel: {channel}")
    print(f"   Ref: {ref}")
    print(f"   Customer: {customer_name}")
    print(f"   Customer Contact: {customer_contact}")
    print("=" * 90)

    if channel == "PURCHASING_WHATSAPP":
        from purchasing_whatsapp import send_purchasing_supplier_rfq

        result = send_purchasing_supplier_rfq(
            brand=brand,
            items=items,
            ref=ref,
            customer_name=customer_name,
            customer_contact=customer_contact,
        )
        if result.get("status") == "SENT":
            return result
        print("⚠️ [ROUTER] Purchasing WhatsApp failed. Falling back to Email.")

    elif channel == "WHATSAPP":
        phone = get_supplier_whatsapp(brand)
        msg = build_supplier_rfq_text(
            brand, items, ref,
            customer_name=customer_name,
            customer_contact=customer_contact,
            include_customer_info=not is_external_supplier(brand),
        )

        success = send_whatsapp_in_current_driver(driver, phone, msg)

        if success:
            log_supplier_pending(
                ref=ref,
                brand=brand,
                customer_name=customer_name,
                customer_contact=customer_contact,
                supplier_channel="WHATSAPP",
                supplier_to=phone,
                items=items
            )

            return {
                "channel": "WHATSAPP",
                "to": phone,
                "status": "SENT"
            }

        print("⚠️ [ROUTER] WhatsApp failed. Falling back to Email.")

    success = send_supplier_email(
        brand, items, ref,
        customer_name=customer_name,
        customer_contact=customer_contact,
    )

    if success:
        log_supplier_pending(
            ref=ref,
            brand=brand,
            customer_name=customer_name,
            customer_contact=customer_contact,
            supplier_channel="EMAIL",
            supplier_to=get_supplier_email(brand),
            items=items,
        )

    return {
        "channel": "EMAIL",
        "to": get_supplier_email(brand),
        "status": "SENT" if success else "FAILED"
    }
