import os
import time
import csv
import json
import datetime
from dotenv import load_dotenv
from O365 import Account
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

load_dotenv()

SUPPLIER_PENDING_CSV = "/Users/evon/OpenClaw/whatsapp_supplier_pending.csv"

SUPPLIER_CHANNEL_ROUTING = {
    "OMRON": "EMAIL",
    "BURKERT": "EMAIL",
    "SMC": "EMAIL",
    "UNKNOWN": "EMAIL",
    "DEFAULT": "EMAIL",
}

SUPPLIER_EMAIL_ROUTING = {
    "OMRON": "purchasing@robomatics.sg",
    "BURKERT": "purchasing@robomatics.sg",
    "SMC": "purchasing@robomatics.sg",
    "UNKNOWN": "purchasing@robomatics.sg",
    "DEFAULT": "purchasing@robomatics.sg",
}

SUPPLIER_WHATSAPP_ROUTING = {
    "OMRON": "60167027683",
    "BURKERT": "60167027683",
    "DEFAULT": "60167027683",
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


def build_supplier_rfq_text(brand, items, ref, customer_name=None, customer_contact=None):
    brand = str(brand or "UNKNOWN").upper().strip()

    msg = f"""Hi,

Please quote the following items:

Brand: {brand}
Ref: {ref}
Customer: {customer_name or "-"}
Customer Contact: {customer_contact or "-"}

[REPLY FORMAT - PLEASE COPY & FILL]

"""

    for idx, item in enumerate(items, start=1):
        desc = item.get("desc", "")
        qty = item.get("qty", "")

        msg += f"""{idx}) {desc}
Qty: {qty}
Price:
Lead Time:

"""

    msg += """[END]

⚠️ Please copy the format above and fill in Price & Lead Time for each item.

Example:

1) SAMPLE ITEM
Qty: 1
Price: RM100
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

    mailbox = get_mailbox()

    if not mailbox:
        return False

    msg_text = build_supplier_rfq_text(
        brand, items, ref,
        customer_name=customer_name,
        customer_contact=customer_contact,
    )

    sm = mailbox.new_message()
    sm.to.add(supplier_email)
    sm.subject = f"[PURCHASING RFQ] [{brand}] WhatsApp Inquiry - Ref: {ref}"
    sm.body = msg_text
    sm.body_type = "text"
    sm.send()

    print("✅ [ROUTER] Supplier RFQ sent by Email")
    print(f"   Brand: {brand}")
    print(f"   To: {supplier_email}")
    print(f"   Ref: {ref}")

    return True


def open_whatsapp_chat(driver, phone):
    print(f"🌐 [ROUTER] Opening supplier WhatsApp chat: {phone}")

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


def send_whatsapp_in_current_driver(driver, phone, message):
    print("📲 [ROUTER] Sending WhatsApp using current browser session...")
    print(f"   To: {phone}")

    if not open_whatsapp_chat(driver, phone):
        print("❌ [ROUTER] Could not open WhatsApp supplier chat.")
        return False

    time.sleep(3)

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

    if channel == "WHATSAPP":
        phone = get_supplier_whatsapp(brand)
        msg = build_supplier_rfq_text(
            brand, items, ref,
            customer_name=customer_name,
            customer_contact=customer_contact,
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
