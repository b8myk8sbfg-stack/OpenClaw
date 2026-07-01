import os
import re
import time
import csv
import datetime
import random
import string
import socket

import sys

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options

from openclaw_inquiry_engine import (
    build_plain_quotation_reply,
    process_inquiry_text,
    process_structured_items,
)
from channel_router import send_supplier_rfq
from non_standard_inquiry_handler import handle_non_standard_items
from image_inquiry_analyzer import analyze_inquiry_image
from openclaw_main import extract_rfq_with_copilot
from whatsapp_message_classifier import (
    INTENT_TYPES,
    build_classification_monitor_message,
    classify_whatsapp_message,
    detect_bubble_media,
    log_classification,
)
from whatsapp_attachment_processor import enrich_message_from_attachments
from message_learning_store import apply_feedback_command

VERSION = "v3.10-VOICE-DOC-FEEDBACK"

CHROME_BINARY_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Users/evon/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

OPENCLAW_CHROME_PROFILE = "/Users/evon/OpenClaw/chrome_whatsapp_profile"
WHATSAPP_INQUIRY_LOG = "/Users/evon/OpenClaw/whatsapp_inquiries.csv"
SUPPLIER_PENDING_CSV = "/Users/evon/OpenClaw/whatsapp_supplier_pending.csv"
MARK_UNREAD_FLAG = "/Users/evon/OpenClaw/whatsapp_mark_unread.flag"
IMAGE_CAPTURE_DIR = "/Users/evon/OpenClaw/logs/wa_image_capture"
CUSTOMER_REPLY_MODE_FILE = "/Users/evon/OpenClaw/openclaw_whatsapp_reply_mode.txt"

MAX_UNREAD_CHATS_PER_RUN = 10
CHECK_INTERVAL_SECONDS = 60
MARKUP_DIVISOR = 0.8
MONITOR_WHATSAPP_PHONE = os.getenv("OPENCLAW_MONITOR_WHATSAPP_PHONE", "+60167222208")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def gen_unique_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))


def normalize_phone(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def get_customer_reply_mode():
    env_mode = os.getenv("OPENCLAW_WHATSAPP_REPLY_MODE")

    if env_mode:
        return env_mode.strip().lower()

    try:
        with open(CUSTOMER_REPLY_MODE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().lower() or "monitor"
    except FileNotFoundError:
        return "monitor"


def parse_money(value):
    raw = str(value or "").upper()
    raw = raw.replace("RM", "")
    raw = raw.replace(",", "")
    raw = raw.strip()

    match = re.search(r"\d+(?:\.\d+)?", raw)

    if not match:
        return None

    return float(match.group(0))


def format_money(value):
    return f"{float(value):,.2f}"


def find_chrome_binary():
    for path in CHROME_BINARY_PATHS:
        if os.path.exists(path):
            print(f"✅ Chrome binary found: {path}")
            return path

    raise FileNotFoundError("Google Chrome binary not found.")


def get_existing_chrome_debugger_address():
    """Return an active profile's DevTools address, ignoring stale port files."""
    port_file = os.path.join(OPENCLAW_CHROME_PROFILE, "DevToolsActivePort")
    try:
        with open(port_file, "r", encoding="utf-8") as f:
            port = int(f.readline().strip())
        with socket.create_connection(("127.0.0.1", port), timeout=0.75):
            return f"127.0.0.1:{port}"
    except (ValueError, OSError):
        try:
            os.remove(port_file)
            print("🧹 Removed stale WhatsApp Chrome DevTools port file.")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠️ Could not remove stale DevTools port file: {exc}")
        return None
    except FileNotFoundError:
        return None


def init_driver():
    chrome_binary = find_chrome_binary()

    os.makedirs(OPENCLAW_CHROME_PROFILE, exist_ok=True)

    options = Options()
    options.binary_location = chrome_binary
    debugger_address = get_existing_chrome_debugger_address()
    if debugger_address:
        print(f"♻️ Attaching to existing WhatsApp Chrome session: {debugger_address}")
        options.add_experimental_option("debuggerAddress", debugger_address)
    else:
        options.add_argument(f"--user-data-dir={OPENCLAW_CHROME_PROFILE}")
        options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    return webdriver.Chrome(options=options)


def ensure_log():
    fields = [
        "timestamp",
        "contact_name",
        "message",
        "detected_items",
        "supplier_brands",
        "status"
    ]

    if not os.path.exists(WHATSAPP_INQUIRY_LOG):
        with open(WHATSAPP_INQUIRY_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

    return fields


def append_log(contact_name, message, items, supplier_brands, status):
    fields = ensure_log()

    with open(WHATSAPP_INQUIRY_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({
            "timestamp": now_iso(),
            "contact_name": contact_name or "",
            "message": message or "",
            "detected_items": str(items),
            "supplier_brands": str(supplier_brands),
            "status": status
        })


def wait_for_whatsapp_ready(driver, timeout=90):
    print("🟢 Waiting for WhatsApp Web session...")

    end = time.time() + timeout

    while time.time() < end:
        selectors = [
            '//div[@id="side"]',
            '//div[@aria-label="Chat list"]',
            '//div[@role="grid"]',
            '//header',
        ]

        for selector in selectors:
            try:
                found = driver.find_elements(By.XPATH, selector)
                if found:
                    print("✅ WhatsApp Web ready.")
                    return True
            except Exception:
                pass

        print("   ⏳ Waiting for WhatsApp login/session...")
        time.sleep(3)

    print("❌ WhatsApp Web not ready.")
    return False


def find_unread_chat_rows(driver):
    print("🔎 Scanning visible chat list for unread chats...")

    unread_rows = []

    candidate_rows = driver.find_elements(
        By.XPATH,
        '//div[@role="listitem"] | //div[contains(@aria-label, "Chat list")]//div[@role="row"]'
    )

    print(f"📋 Visible chat row candidates: {len(candidate_rows)}")

    for row in candidate_rows:
        try:
            text = row.text.strip()

            if not text:
                continue

            unread_markers = row.find_elements(
                By.XPATH,
                './/*[contains(@aria-label, "unread") or contains(@aria-label, "Unread")]'
            )

            has_unread = bool(unread_markers)

            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if not has_unread and lines:
                last_line = lines[-1]
                if re.fullmatch(r"\d{1,2}", last_line):
                    has_unread = True

            if has_unread:
                unread_rows.append(row)
                preview = text.replace("\n", " | ")
                print(f"   ✅ Unread chat candidate: {preview[:160]}")

        except Exception:
            continue

    print(f"📬 Unread chats detected: {len(unread_rows)}")
    return unread_rows[:MAX_UNREAD_CHATS_PER_RUN]


def contact_hint_variants(contact_hint):
    raw = str(contact_hint or "").strip()
    digits = normalize_phone(raw)
    variants = [raw]

    if raw.startswith("+"):
        variants.append(raw[1:])

    if digits:
        variants.append(digits)
        if len(digits) > 9:
            variants.append(digits[-9:])
            variants.append(digits[-10:])

    if " " in raw:
        variants.append(raw.replace(" ", ""))
        variants.append(raw.replace(" ", "-"))

    deduped = []
    seen = set()

    for variant in variants:
        key = variant.lower()
        if variant and key not in seen:
            seen.add(key)
            deduped.append(variant)

    return deduped


def find_chat_list_row(driver, contact_hint):
    hints = contact_hint_variants(contact_hint)

    rows = driver.find_elements(
        By.XPATH,
        '//div[@role="listitem"] | //div[contains(@aria-label, "Chat list")]//div[@role="row"]'
    )

    for row in rows:
        try:
            row_text = (row.text or "").lower()

            for hint in hints:
                if hint.lower() in row_text:
                    return row

            for title_el in row.find_elements(By.XPATH, './/span[@title]'):
                title = (title_el.get_attribute("title") or "").lower()

                for hint in hints:
                    if hint.lower() in title:
                        return row

        except Exception:
            continue

    return None


def click_mark_unread_menu_item(driver):
    menu_selectors = [
        '//div[@role="button" and contains(., "Mark as unread")]',
        '//span[contains(text(), "Mark as unread")]',
        '//*[@aria-label="Mark as unread"]',
        '//li[contains(., "Mark as unread")]',
    ]

    for selector in menu_selectors:
        try:
            items = driver.find_elements(By.XPATH, selector)

            for item in items:
                if item.is_displayed():
                    driver.execute_script("arguments[0].click();", item)
                    return True

        except Exception:
            continue

    return False


def open_chat_via_search(driver, contact_hint):
    hints = contact_hint_variants(contact_hint)
    search_selectors = [
        '//div[@contenteditable="true"][@data-tab="3"]',
        '//div[@contenteditable="true"][@title="Search input textbox"]',
        '//div[@role="textbox"][@contenteditable="true"]',
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
        print("❌ WhatsApp search box not found.")
        return False

    search_box.click()
    time.sleep(0.5)

    for key_combo in [Keys.COMMAND, Keys.CONTROL]:
        search_box.send_keys(key_combo, "a")
        search_box.send_keys(Keys.BACKSPACE)

    search_box.send_keys(hints[0])
    time.sleep(2)

    title_selectors = [
        f'//span[@title="{hint}"]' for hint in hints
    ] + [
        '//span[@title]'
    ]

    for selector in title_selectors:
        try:
            results = driver.find_elements(By.XPATH, selector)

            for result in results:
                title = (result.get_attribute("title") or "").lower()

                if any(hint.lower() in title for hint in hints):
                    driver.execute_script("arguments[0].click();", result)
                    time.sleep(2)
                    print(f"✅ Opened chat via search: {result.get_attribute('title')}")
                    return True

        except Exception:
            continue

    print(f"❌ Could not find chat via search for: {contact_hint}")
    return False


def mark_current_chat_unread_via_header(driver):
    header_menu_selectors = [
        '//header//span[@data-icon="menu"]',
        '//header//*[@data-icon="menu"]',
        '//header//button[@aria-label="Menu"]',
        '//header//*[@aria-label="Menu"]',
    ]

    for selector in header_menu_selectors:
        try:
            buttons = driver.find_elements(By.XPATH, selector)

            for button in buttons:
                if button.is_displayed():
                    driver.execute_script("arguments[0].click();", button)
                    time.sleep(1)

                    if click_mark_unread_menu_item(driver):
                        print("✅ Chat marked as unread via header menu.")
                        return True

        except Exception:
            continue

    return False


def mark_chat_as_unread(driver, contact_hint="+60 16-722 2208"):
    print(f"📌 Marking WhatsApp chat as unread: {contact_hint}")

    driver.get("https://web.whatsapp.com")

    if not wait_for_whatsapp_ready(driver, timeout=60):
        return False

    time.sleep(2)

    row = find_chat_list_row(driver, contact_hint)

    if row:
        try:
            ActionChains(driver).move_to_element(row).perform()
            time.sleep(1)

            row_menu_selectors = [
                './/*[@data-icon="down"]',
                './/*[@aria-label="Open the chat context menu"]',
                './/button[contains(@aria-label, "Menu")]',
            ]

            for selector in row_menu_selectors:
                buttons = row.find_elements(By.XPATH, selector)

                for button in buttons:
                    try:
                        if button.is_displayed():
                            driver.execute_script("arguments[0].click();", button)
                            time.sleep(1)

                            if click_mark_unread_menu_item(driver):
                                print("✅ Chat marked as unread via chat list menu.")
                                return True

                    except Exception:
                        continue

            ActionChains(driver).context_click(row).perform()
            time.sleep(1)

            if click_mark_unread_menu_item(driver):
                print("✅ Chat marked as unread via right-click.")
                return True

        except Exception as e:
            print(f"⚠️ Chat row menu failed: {e}")

    if open_chat_via_search(driver, contact_hint):
        if mark_current_chat_unread_via_header(driver):
            return True

        try:
            header = driver.find_elements(By.XPATH, '//header')[-1]
            ActionChains(driver).context_click(header).perform()
            time.sleep(1)

            if click_mark_unread_menu_item(driver):
                print("✅ Chat marked as unread via header right-click.")
                return True

        except Exception as e:
            print(f"⚠️ Header right-click failed: {e}")

    print(f"❌ Failed to mark chat as unread: {contact_hint}")
    return False


def get_contact_name_from_open_chat(driver):
    # Prefer the conversation header inside #main. WhatsApp Web has other
    # headers (sidebar/business panels), and taking the last global header can
    # incorrectly label the customer as "WhatsApp Business".
    try:
        headers = driver.find_elements(By.CSS_SELECTOR, '#main header')
        if headers:
            header_text = headers[0].text.strip()
            lines = [x.strip() for x in header_text.splitlines() if x.strip()]
            if lines:
                return lines[0]
    except Exception:
        pass

    try:
        headers = driver.find_elements(By.XPATH, '//header')
        if headers:
            header_text = headers[-1].text.strip()
            lines = [x.strip() for x in header_text.splitlines() if x.strip()]
            if lines:
                return lines[0]
    except Exception:
        pass

    return "WhatsApp Customer"


def get_contact_phone_from_open_chat(driver, bubble=None):
    """Best-effort extraction of the sender JID/phone from WhatsApp's DOM."""
    candidates = []

    if bubble is not None:
        try:
            candidates.extend(bubble.find_elements(By.XPATH, './/ancestor-or-self::*[@data-id]'))
        except Exception:
            pass

    selectors = [
        ('css', '#main header [title]'),
        ('css', '#main [data-id]'),
        ('xpath', '//*[@aria-selected="true"]//*[@data-id]'),
    ]
    for kind, selector in selectors:
        try:
            by = By.CSS_SELECTOR if kind == 'css' else By.XPATH
            candidates.extend(driver.find_elements(by, selector))
        except Exception:
            continue

    values = []
    for element in candidates:
        try:
            values.extend([
                element.get_attribute("data-id") or "",
                element.get_attribute("title") or "",
                element.get_attribute("aria-label") or "",
                element.text or "",
            ])
        except Exception:
            continue

    for value in values:
        jid_match = re.search(r"(?:^|_)(\d{8,15})@(?:c\.us|s\.whatsapp\.net)", value)
        if jid_match:
            return f"+{jid_match.group(1)}"
        phone_match = re.search(r"\+(\d[\d\s\-()]{7,20}\d)", value)
        if phone_match:
            digits = normalize_phone(phone_match.group(0))
            if 8 <= len(digits) <= 15:
                return f"+{digits}"

    return ""


def open_unread_chat(row):
    try:
        row.click()
        time.sleep(5)
        return True
    except Exception as e:
        print(f"❌ Could not open unread chat: {e}")
        return False


def wait_for_chat_messages(driver, timeout=12):
    end = time.time() + timeout

    while time.time() < end:
        selectors = [
            'div[data-pre-plain-text]',
            'div[data-testid="msg-container"]',
            'div.message-in',
        ]

        for selector in selectors:
            try:
                if driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            except Exception:
                continue

        time.sleep(1)

    return False


def is_outgoing_pre_plain(pre_plain_text):
    ppt = str(pre_plain_text or "").strip()
    return bool(re.search(r"\]\s*You:\s*$", ppt, re.I))


def clean_bubble_text(text):
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    cleaned_lines = []

    for line in lines:
        if re.fullmatch(r"\d{1,2}:\d{2}\s*(AM|PM)?", line, re.I):
            continue

        if re.match(r"\[\d{1,2}:\d{2}\s*(?:AM|PM)?,\s*\d{1,2}/\d{1,2}/\d{4}\]", line, re.I):
            continue

        if line in ["✓", "✓✓", "✓ ✓"]:
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def is_whatsapp_system_promotion(text):
    """Identify WhatsApp/Meta promotional cards that mimic chat messages."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    promotion_markers = (
        "get the verified badge and the benefits",
        "apply to be meta verified",
        "additional benefits: new customers feel confident",
        "boosted trust in your brand",
    )
    return any(marker in normalized for marker in promotion_markers)


def pick_latest_message_block(messages):
    """
    Return ONLY the latest incoming WhatsApp message.

    Previous versions joined all trailing messages that contained "Qty:",
    which caused stale inquiry contamination:
      old E3Z / BURKERT message + new G3NA message + new ABC test message
    were merged into one inquiry.

    WhatsApp already keeps each message in its own data-pre-plain-text bubble,
    so we must only process the last non-empty incoming bubble.
    """
    if not messages:
        return ""

    cleaned = []

    for msg in messages:
        msg = clean_bubble_text(msg)
        if msg:
            cleaned.append(msg)

    if not cleaned:
        return ""

    return cleaned[-1].strip()


def extract_text_from_copyable_div(div):
    text_parts = []

    span_selectors = [
        'span[data-testid="selectable-text"]',
        'span.copyable-text',
        'span.selectable-text',
        'span[dir="ltr"]',
    ]

    for selector in span_selectors:
        try:
            spans = div.find_elements(By.CSS_SELECTOR, selector)

            for span in spans:
                txt = span.text.strip()
                if txt and txt not in text_parts:
                    text_parts.append(txt)

        except Exception:
            continue

    if text_parts:
        return clean_bubble_text("\n".join(text_parts))

    return clean_bubble_text(div.text)


def get_latest_incoming_message_from_pre_plain(driver):
    incoming_messages = []

    try:
        copyable_divs = driver.find_elements(By.CSS_SELECTOR, 'div[data-pre-plain-text]')
    except Exception:
        copyable_divs = []

    for div in copyable_divs:
        try:
            pre_plain = div.get_attribute("data-pre-plain-text") or ""

            if is_outgoing_pre_plain(pre_plain):
                continue

            text = extract_text_from_copyable_div(div)

            if text and not is_whatsapp_system_promotion(text):
                incoming_messages.append(text)

        except Exception:
            continue

    return pick_latest_message_block(incoming_messages)


def get_latest_incoming_message_from_legacy_selectors(driver):
    bubble_selectors = [
        '(//div[contains(@class, "message-in")])[last()]',
        '(//div[contains(@data-testid, "msg-container") and contains(@class, "message-in")])[last()]',
        '(//div[@data-testid="msg-container" and not(contains(@class, "message-out"))])[last()]',
    ]

    for bubble_selector in bubble_selectors:
        try:
            bubbles = driver.find_elements(By.XPATH, bubble_selector)

            if not bubbles:
                continue

            message = clean_bubble_text(bubbles[-1].text)

            if message and not is_whatsapp_system_promotion(message):
                return message

        except Exception:
            continue

    incoming_texts = []

    selectors = [
        '//div[contains(@class, "message-in")]//span[contains(@class, "selectable-text")]',
        '//div[contains(@class, "message-in")]//span[@data-testid="selectable-text"]',
        '//div[contains(@class, "message-in")]//span[@dir="ltr"]',
        '//div[contains(@class, "message-in")]//span[contains(@class, "copyable-text")]',
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)

            for el in elements:
                txt = el.text.strip()
                if (
                    txt
                    and not is_whatsapp_system_promotion(txt)
                    and txt not in incoming_texts
                ):
                    incoming_texts.append(txt)

        except Exception:
            continue

    return pick_latest_message_block(incoming_texts)


def get_latest_incoming_message(driver):
    if not wait_for_chat_messages(driver, timeout=10):
        print("⚠️ Chat message panel not ready yet.")

    try:
        driver.execute_script(
            "const panel = document.querySelector('[data-testid=\"conversation-panel-messages\"]');"
            "if (panel) { panel.scrollTop = panel.scrollHeight; }"
        )
        time.sleep(1)
    except Exception:
        pass

    message = get_latest_incoming_message_from_pre_plain(driver)

    if message:
        return message

    message = get_latest_incoming_message_from_legacy_selectors(driver)

    if message:
        return message

    print("⚠️ Could not scrape incoming message text from WhatsApp Web DOM.")
    return ""


def get_latest_incoming_bubble(driver):
    if not wait_for_chat_messages(driver, timeout=10):
        return None

    try:
        driver.execute_script(
            "const panel = document.querySelector('[data-testid=\"conversation-panel-messages\"]');"
            "if (panel) { panel.scrollTop = panel.scrollHeight; }"
        )
        time.sleep(1)
    except Exception:
        pass

    try:
        copyable_divs = driver.find_elements(By.CSS_SELECTOR, 'div[data-pre-plain-text]')
    except Exception:
        return None

    for div in reversed(copyable_divs):
        try:
            pre_plain = div.get_attribute("data-pre-plain-text") or ""
            if is_outgoing_pre_plain(pre_plain):
                continue
            text = extract_text_from_copyable_div(div)
            if is_whatsapp_system_promotion(text):
                print("ℹ️ Ignoring WhatsApp/Meta promotional message bubble.")
                continue
            return div
        except Exception:
            continue

    return None


def find_media_image_in_bubble(bubble):
    if bubble is None:
        return None

    selectors = [
        'img[src]',
        'video[src]',
        '[data-testid="image-thumb"] img',
        '[data-testid="image-thumb"]',
    ]

    best = None
    best_area = 0

    for selector in selectors:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            try:
                src = (element.get_attribute("src") or "").lower()
                if any(token in src for token in ("emoji", "avatar", "gif", "sticker")):
                    continue

                size = element.size or {}
                width = int(size.get("width") or 0)
                height = int(size.get("height") or 0)
                area = width * height

                if area >= 80 and area > best_area:
                    best = element
                    best_area = area

            except Exception:
                continue

    return best


def bubble_has_media_image(bubble):
    return find_media_image_in_bubble(bubble) is not None


def capture_bubble_image(driver, bubble, contact_name):
    media = find_media_image_in_bubble(bubble)

    if media is None:
        return None

    os.makedirs(IMAGE_CAPTURE_DIR, exist_ok=True)
    safe_contact = re.sub(r"[^A-Za-z0-9._-]+", "_", str(contact_name or "contact"))[:60]
    image_path = os.path.join(
        IMAGE_CAPTURE_DIR,
        f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_contact}.png"
    )

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            media
        )
        time.sleep(1)
        media.screenshot(image_path)
        print(f"🖼️ Saved incoming WhatsApp image: {image_path}")
        return image_path
    except Exception as e:
        print(f"❌ Failed to capture WhatsApp image: {e}")
        return None


def analyze_whatsapp_image(image_path, caption_text=""):
    try:
        return analyze_inquiry_image(image_path, caption_text=caption_text)
    except Exception as e:
        print(f"❌ Image inquiry analysis failed: {e}")
        return {
            "items": [],
            "inquiry_text": "",
            "notes": str(e),
            "source": "image",
            "image_path": image_path,
        }


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
                        print("✅ WhatsApp message sent by clicking Send.")
                        return True

            except Exception:
                continue

        time.sleep(1)

    return False


def send_reply_in_current_chat(driver, message):
    print("📲 Sending WhatsApp reply using current chat...")

    try:
        box = find_message_box(driver)

        if not box:
            print("❌ Message box not found.")
            return False

        box.click()
        time.sleep(1)

        lines = message.split("\n")

        for idx, line in enumerate(lines):
            box.send_keys(line)

            if idx != len(lines) - 1:
                box.send_keys(Keys.SHIFT, Keys.ENTER)

        time.sleep(2)

        if click_send_button(driver, timeout=15):
            return True

        print("⚠️ Send button not found. Trying ENTER method...")

        box = find_message_box(driver)

        if box:
            box.click()
            time.sleep(0.5)
            box.send_keys(Keys.ENTER)
            print("✅ WhatsApp message sent by ENTER.")
            return True

        return False

    except Exception as e:
        print(f"❌ Failed to send reply: {e}")
        return False


def open_whatsapp_chat_by_phone(driver, phone):
    phone = normalize_phone(phone)

    if not phone:
        print("❌ Customer phone missing. Cannot open WhatsApp chat.")
        return False

    print(f"🌐 Opening customer WhatsApp chat: {phone}")

    driver.get(f"https://web.whatsapp.com/send?phone={phone}")

    end = time.time() + 90

    while time.time() < end:
        try:
            box = driver.find_elements(By.XPATH, '//footer//div[@contenteditable="true"]')
            if box:
                print("✅ Customer WhatsApp chat opened.")
                return True
        except Exception:
            pass

        time.sleep(3)

    print("❌ Customer WhatsApp chat did not open.")
    return False


def customer_replies_go_to_monitor():
    return get_customer_reply_mode() in ("monitor", "debug", "test")


def build_monitor_reply(context, customer_name, customer_contact, original_message, reply_message,
                        classification_summary=None):
    classification_block = ""
    if classification_summary:
        classification_block = (
            "Classification:\n"
            f"{classification_summary}\n\n"
        )

    return (
        "[OpenClaw Monitor Mode]\n"
        f"Context: {context or 'Customer reply'}\n"
        f"Customer: {customer_name or '-'}\n"
        f"Customer Contact: {customer_contact or customer_name or '-'}\n\n"
        f"{classification_block}"
        "Original Message:\n"
        f"{original_message or '(empty)'}\n\n"
        "Generated Reply:\n"
        f"{reply_message or '(empty)'}"
    )


def send_customer_reply(driver, reply_message, customer_name=None, customer_contact=None,
                        original_message=None, context="CUSTOMER_REPLY",
                        customer_chat_is_open=True, classification_summary=None):
    if customer_replies_go_to_monitor():
        print(f"🧪 Monitor mode active. Redirecting customer reply to {MONITOR_WHATSAPP_PHONE}.")

        if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE):
            print("❌ Monitor WhatsApp chat did not open.")
            return False

        monitor_message = build_monitor_reply(
            context=context,
            customer_name=customer_name,
            customer_contact=customer_contact,
            original_message=original_message,
            reply_message=reply_message,
            classification_summary=classification_summary,
        )
        return send_reply_in_current_chat(driver, monitor_message)

    if not customer_chat_is_open:
        if not open_whatsapp_chat_by_phone(driver, customer_contact):
            return False

    return send_reply_in_current_chat(driver, reply_message)


def send_classification_alert(driver, contact_name, customer_contact, message_text, classification):
    """Always notify monitor during development so every message is classified and visible."""
    alert = build_classification_monitor_message(
        contact_name, customer_contact, message_text, classification
    )
    print("")
    print("=" * 90)
    print("🏷️ MESSAGE CLASSIFICATION")
    print(classification.summary())
    print("=" * 90)

    if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE):
        print("❌ Could not open monitor chat for classification alert.")
        return False

    return send_reply_in_current_chat(driver, alert)


def process_monitor_feedback(driver):
    """Check monitor chat for teaching commands like: correct: purchase_order"""
    if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE):
        return False

    feedback_text = get_latest_incoming_message(driver)
    if not feedback_text:
        return False

    result = apply_feedback_command(feedback_text, INTENT_TYPES)
    if not result:
        return False

    ack = (
        "[OpenClaw Learning Updated]\n"
        f"Intent: {result['intent']}\n"
        f"Match text: {result['match_text'][:200] or '(from last message)'}\n"
        f"Previous intent: {result.get('previous_intent') or '-'}\n"
        f"Customer context: {result.get('contact_name') or '-'}\n\n"
        "Saved to corrections + confirmed training examples."
    )
    print("")
    print("=" * 90)
    print("🎓 WHATSAPP FEEDBACK LEARNED")
    print(ack)
    print("=" * 90)
    send_reply_in_current_chat(driver, ack)
    return True


def load_supplier_pending():
    if not os.path.exists(SUPPLIER_PENDING_CSV):
        return [], []

    with open(SUPPLIER_PENDING_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames or []


def save_supplier_pending(rows, fieldnames):
    if not fieldnames:
        return

    with open(SUPPLIER_PENDING_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_supplier_sections(message):
    ref_matches = list(re.finditer(r"WA-\d{8}-[A-Z0-9]+-[A-Z0-9]+", message, re.I))

    sections = []

    for idx, match in enumerate(ref_matches):
        ref = match.group(0).upper()
        start = match.start()
        end = ref_matches[idx + 1].start() if idx + 1 < len(ref_matches) else len(message)
        section = message[start:end].strip()

        sections.append({
            "ref": ref,
            "section": section
        })

    return sections


def parse_supplier_reply_items(section):
    items = []

    block_pattern = re.compile(
        r"(\d+)\)\s*(.*?)\n"
        r"\s*Qty\s*:\s*(\d+)\s*\n"
        r"\s*Price\s*:\s*([^\n]*)\n"
        r"\s*Lead\s*Time\s*:\s*([^\n]*)",
        re.I | re.S
    )

    for idx, desc, qty, supplier_price_raw, lead_time in block_pattern.findall(section):
        desc = re.sub(r"\s+", " ", desc).strip()
        qty = int(qty)
        supplier_price_raw = supplier_price_raw.strip()
        lead_time = lead_time.strip()

        if not supplier_price_raw or not lead_time:
            continue

        if "SAMPLE ITEM" in desc.upper():
            continue

        supplier_cost = parse_money(supplier_price_raw)

        if supplier_cost is None:
            continue

        customer_unit_price = supplier_cost / MARKUP_DIVISOR
        customer_subtotal = customer_unit_price * qty

        items.append({
            "idx": int(idx),
            "desc": desc,
            "qty": qty,
            "supplier_cost_raw": supplier_price_raw,
            "supplier_cost": supplier_cost,
            "customer_unit_price": customer_unit_price,
            "customer_subtotal": customer_subtotal,
            "lead_time": lead_time
        })

    return items


def build_customer_update_from_supplier(ref, brand, parsed_items):
    msg = f"Hi, we have received supplier update for your inquiry.\n\nRef: {ref}\nBrand: {brand}\n\n"

    total = 0.0

    for item in parsed_items:
        total += item["customer_subtotal"]

        msg += f"{item['idx']}) {item['desc']}\n"
        msg += f"Qty: {item['qty']}\n"
        msg += f"Unit Price: RM {format_money(item['customer_unit_price'])}\n"
        msg += f"Lead Time: {item['lead_time']}\n"
        msg += f"Subtotal: RM {format_money(item['customer_subtotal'])}\n\n"

    msg += f"Total: RM {format_money(total)}\n\n"
    msg += "Thank you."

    return msg


def process_supplier_reply(driver, contact_name, latest_message):
    print("📬 Detected WhatsApp supplier reply / RFQ reference message.")

    sections = extract_supplier_sections(latest_message)

    if not sections:
        print("⚠️ No WA ref section found.")
        return True

    rows, fieldnames = load_supplier_pending()

    if not rows:
        print("⚠️ Supplier pending CSV is empty or missing.")
        append_log(contact_name, latest_message, [], [], "SUPPLIER_REF_BUT_PENDING_CSV_EMPTY")
        return True

    pending_by_ref = {row.get("ref", "").upper(): row for row in rows}

    for sec in sections:
        ref = sec["ref"]
        section = sec["section"]

        print("")
        print("-" * 90)
        print(f"🔎 Processing supplier ref section: {ref}")

        pending = pending_by_ref.get(ref)

        if not pending:
            print("⚠️ Ref not found in pending CSV.")
            print("   IMPORTANT: This will NOT be treated as a new customer inquiry.")
            append_log(contact_name, section, [], [], f"SUPPLIER_REF_NOT_FOUND_{ref}")
            continue

        parsed_items = parse_supplier_reply_items(section)

        if not parsed_items:
            print("⚠️ Ref found, but Price / Lead Time not filled.")
            print("   Treating as supplier RFQ copy or incomplete reply only.")
            append_log(contact_name, section, [], [pending.get("brand")], f"SUPPLIER_REF_NO_PRICE_LT_{ref}")
            continue

        customer_phone = pending.get("customer_phone")
        brand = pending.get("brand")

        print("✅ Supplier filled reply parsed with markup:")
        for item in parsed_items:
            print(
                f"   {item['idx']}) {item['desc']} | Qty: {item['qty']} | "
                f"Supplier Cost: RM {format_money(item['supplier_cost'])} | "
                f"Customer Price: RM {format_money(item['customer_unit_price'])} | "
                f"LT: {item['lead_time']}"
            )

        customer_msg = build_customer_update_from_supplier(ref, brand, parsed_items)
        sent = send_customer_reply(
            driver,
            customer_msg,
            customer_name=pending.get("customer_name") or pending.get("customer_contact"),
            customer_contact=customer_phone,
            original_message=section,
            context=f"SUPPLIER_UPDATE_{ref}",
            customer_chat_is_open=False
        )

        for row in rows:
            if row.get("ref", "").upper() == ref:
                row["status"] = "CUSTOMER_UPDATED" if sent else "SUPPLIER_REPLIED_CUSTOMER_SEND_FAILED"
                row["supplier_replied_at"] = now_iso()
                row["customer_updated_at"] = now_iso() if sent else ""
                break

        save_supplier_pending(rows, fieldnames)

        append_log(
            contact_name,
            section,
            parsed_items,
            [brand],
            f"SUPPLIER_REPLY_CUSTOMER_UPDATED_{ref}" if sent else f"SUPPLIER_REPLY_CUSTOMER_SEND_FAILED_{ref}"
        )

        time.sleep(3)

    return True


def process_customer_inquiry(
    driver, contact_name, latest_message, image_analysis=None, customer_contact=None,
    classification=None, document_items=None,
):
    customer_contact = customer_contact or contact_name
    classification_summary = classification.summary() if classification else None
    document_items = document_items or []
    image_path = image_analysis.get("image_path") if image_analysis else None
    copilot_items = extract_rfq_with_copilot(latest_message, image_path=image_path)

    if copilot_items:
        print(f"🤖 Copilot is primary: processing {len(copilot_items)} visually extracted item(s).")
        structured_items = []
        existing_norms = set()
        for item in copilot_items:
            part_no = str(item.get("part_no") or "").strip().upper()
            part_norm = re.sub(r"[^A-Z0-9]", "", part_no)
            if not part_norm or part_norm in existing_norms:
                continue
            structured_items.append({
                "brand": str(item.get("brand") or "UNKNOWN").strip().upper(),
                "part_no": part_no,
                "desc": part_no,
                "qty": int(item["qty"]),
                "norm": part_norm,
                "source": "COPILOT_VISUAL" if image_path else "COPILOT_TEXT",
            })
            existing_norms.add(part_norm)
            print(f"   👁️ Copilot identified | Part: {part_no} | Qty: {item['qty']}")

        formatted_rows, tbc_by_brand, skipped = process_structured_items(structured_items)
        result = {
            "formatted_rows": formatted_rows,
            "tbc_by_brand": tbc_by_brand,
            "has_partial": False,
            "missing_layer2_items": [],
            "skipped": skipped,
        }
    elif document_items:
        print(f"📄 Document extraction primary: processing {len(document_items)} item(s).")
        structured_items = []
        existing_norms = set()
        for item in document_items:
            part_no = str(item.get("part_no") or "").strip().upper()
            part_norm = re.sub(r"[^A-Z0-9]", "", part_no)
            if not part_norm or part_norm in existing_norms:
                continue
            structured_items.append({
                "brand": str(item.get("brand") or "UNKNOWN").strip().upper(),
                "part_no": part_no,
                "desc": part_no,
                "qty": int(item.get("qty") or 1),
                "norm": part_norm,
                "source": "DOCUMENT_EXTRACT",
            })
            existing_norms.add(part_norm)
        formatted_rows, tbc_by_brand, skipped = process_structured_items(structured_items)
        result = {
            "formatted_rows": formatted_rows,
            "tbc_by_brand": tbc_by_brand,
            "has_partial": False,
            "missing_layer2_items": [],
            "skipped": skipped,
        }
    else:
        print("⚠️ Copilot found no usable item. Falling back to regex extraction.")
        result = process_inquiry_text(latest_message)

    formatted_rows = result["formatted_rows"]
    tbc_by_brand = result["tbc_by_brand"]
    skipped = result.get("skipped", [])

    log_message = latest_message
    if image_analysis:
        log_message = (
            f"[IMAGE ANALYSIS]\n"
            f"File: {image_analysis.get('image_path', '')}\n"
            f"Notes: {image_analysis.get('notes', '')}\n"
            f"Extracted:\n{latest_message}"
        )

    image_prefix = "IMAGE_" if image_analysis else ""

    if skipped:
        try:
            handle_non_standard_items(
                customer_name=contact_name,
                customer_contact=customer_contact,
                channel="WHATSAPP",
                items=skipped,
                source_message=latest_message
            )
        except Exception as e:
            print(f"❌ Non-standard handler error: {e}")

    if not formatted_rows:
        if image_analysis:
            reply = (
                "Hi, I analyzed your photo but could not match the parts in our system.\n\n"
                "Please send a clearer label/nameplate photo, or type:\n"
                "E3Z-T61 Qty:1\n"
                "178902 Qty:2"
            )
        else:
            reply = (
                "Hi, I received your WhatsApp message, but I could not detect item details.\n\n"
                "Please send in this format:\n"
                "E3Z-T61 Qty:1\n"
                "178902 Qty:2"
            )

        sent = send_customer_reply(
            driver,
            reply,
            customer_name=contact_name,
            customer_contact=customer_contact,
            original_message=latest_message,
            context=f"{image_prefix}NO_ITEMS",
            customer_chat_is_open=True,
            classification_summary=classification_summary,
        )
        append_log(
            contact_name,
            log_message,
            [],
            [],
            f"{image_prefix}NO_ITEMS_REPLIED" if sent else f"{image_prefix}NO_ITEMS_REPLY_FAILED"
        )
        return

    print("✅ OpenClaw engine formatted rows:")
    for row in formatted_rows:
        print(
            f"   - {row.get('desc')} | Qty: {row.get('qty')} | "
            f"Price: {row.get('price')} | LT: {row.get('lt')} | Brand: {row.get('brand')}"
        )

    customer_reply = build_plain_quotation_reply(formatted_rows)
    sent = send_customer_reply(
        driver,
        customer_reply,
        customer_name=contact_name,
        customer_contact=customer_contact,
        original_message=latest_message,
        context=f"{image_prefix}QUOTATION_REPLY",
        customer_chat_is_open=True,
        classification_summary=classification_summary,
    )

    if tbc_by_brand:
        print("📡 Supplier RFQ required by brand:")

    for brand, items in tbc_by_brand.items():
        ref = f"WA-{datetime.datetime.now().strftime('%Y%m%d')}-{brand}-{gen_unique_id()}"

        print(f"   Brand: {brand} | Items: {len(items)} | Ref: {ref}")

        send_supplier_rfq(
            driver=driver,
            brand=brand,
            items=items,
            ref=ref,
            customer_name=contact_name,
            customer_contact=customer_contact,
        )

        time.sleep(3)

    append_log(
        contact_name,
        log_message,
        formatted_rows,
        list(tbc_by_brand.keys()),
        f"{image_prefix}CUSTOMER_REPLIED_SUPPLIER_ROUTED" if sent else f"{image_prefix}CUSTOMER_REPLY_FAILED_SUPPLIER_ROUTED"
    )


def process_classified_non_inquiry(
    driver,
    contact_name,
    latest_message,
    classification,
    customer_contact=None,
    image_analysis=None,
    document_items=None,
):
    """Handle non-RFQ intents with appropriate acknowledgement while learning in background."""
    customer_contact = customer_contact or contact_name
    handler = classification.handler
    reply = classification.suggested_reply or (
        "Hi, thank you for your message.\n\nOur team will review and respond shortly."
    )
    context = f"INTENT_{classification.intent.upper()}"

    if handler == "skip":
        print("🚫 Junk/ad message skipped — logged only.")
        append_log(contact_name, latest_message, [], [], f"JUNK_SKIPPED_{classification.intent.upper()}")
        log_classification(
            contact_name, customer_contact, latest_message, classification,
            status="JUNK_SKIPPED",
        )
        return

    if handler == "rfq_inquiry":
        process_customer_inquiry(
            driver,
            contact_name,
            latest_message,
            image_analysis=image_analysis,
            customer_contact=customer_contact,
            classification=classification,
        )
        return

    if handler == "purchase_order" and (image_analysis or document_items):
        print("📄 PO with attachment — attempting part extraction.")
        process_customer_inquiry(
            driver,
            contact_name,
            latest_message,
            image_analysis=image_analysis,
            customer_contact=customer_contact,
            classification=classification,
            document_items=document_items,
        )
        return

    sent = send_customer_reply(
        driver,
        reply,
        customer_name=contact_name,
        customer_contact=customer_contact,
        original_message=latest_message,
        context=context,
        customer_chat_is_open=True,
        classification_summary=classification.summary(),
    )

    append_log(
        contact_name,
        latest_message,
        [],
        [],
        f"{context}_REPLIED" if sent else f"{context}_REPLY_FAILED",
    )
    log_classification(
        contact_name,
        customer_contact,
        latest_message,
        classification,
        status=f"{context}_HANDLED",
    )


def process_open_chat(driver):
    contact_name = get_contact_name_from_open_chat(driver)
    bubble = get_latest_incoming_bubble(driver)
    customer_phone = get_contact_phone_from_open_chat(driver, bubble=bubble)
    customer_contact = customer_phone or contact_name
    latest_message = ""
    image_analysis = None
    image_path = None
    media_info = detect_bubble_media(bubble)

    if bubble is not None:
        latest_message = extract_text_from_copyable_div(bubble)
        media_info = detect_bubble_media(bubble, caption_text=latest_message)
        if bubble_has_media_image(bubble) or media_info.has_image:
            image_path = capture_bubble_image(driver, bubble, contact_name)

    for attempt in range(1, 4):
        if latest_message or image_path or media_info.media_type not in ("text", "unknown"):
            break

        latest_message = get_latest_incoming_message(driver)
        bubble = get_latest_incoming_bubble(driver)

        if bubble is not None:
            latest_message = extract_text_from_copyable_div(bubble) or latest_message
            media_info = detect_bubble_media(bubble, caption_text=latest_message)
            if bubble_has_media_image(bubble) or media_info.has_image:
                image_path = capture_bubble_image(driver, bubble, contact_name)

        if latest_message or image_path or media_info.media_type not in ("text", "unknown"):
            break

        print(f"⚠️ Message scrape attempt {attempt}/3 returned empty. Retrying...")
        time.sleep(3)

    if image_path:
        image_analysis = {
            "items": [],
            "inquiry_text": latest_message,
            "notes": "Sent directly to local Copilot visual extraction.",
            "source": "copilot_visual",
            "image_path": image_path,
        }

    enrichment = enrich_message_from_attachments(
        driver, bubble, contact_name, latest_message, media_info
    )
    latest_message = enrichment.get("text") or latest_message
    document_items = enrichment.get("document_items") or []
    if enrichment.get("transcript"):
        media_info.caption = enrichment["transcript"]

    print("")
    print("=" * 90)
    print("📲 UNREAD WHATSAPP CHAT OPENED")
    print(f"   Contact: {contact_name}")
    print(f"   Customer Phone: {customer_phone or '(not exposed by WhatsApp DOM)'}")
    print(f"   Media Type: {media_info.media_type}")
    if media_info.filename:
        print(f"   Attachment: {media_info.filename}")
    print("   Latest Incoming Message:")
    print(latest_message)
    if image_path:
        print(f"   Image: {image_path}")
    print("=" * 90)

    if not latest_message and not image_path and media_info.media_type in ("text", "unknown"):
        print("⚠️ No latest incoming message detected after retries.")

        classification = classify_whatsapp_message("", media_info=media_info)
        log_classification(contact_name, customer_contact, "", classification, status="EMPTY_MESSAGE")

        fallback_reply = (
            "Hi, I received your WhatsApp message but could not read the text.\n\n"
            "Please resend as plain text in this format:\n"
            "E3Z-T61 Qty:1\n"
            "178902 Qty:2"
        )
        sent = send_customer_reply(
            driver,
            fallback_reply,
            customer_name=contact_name,
            customer_contact=customer_contact,
            original_message="",
            context="NO_INCOMING_MESSAGE_FALLBACK",
            customer_chat_is_open=True,
            classification_summary=classification.summary(),
        )
        append_log(
            contact_name,
            "",
            [],
            [],
            "NO_INCOMING_MESSAGE_FALLBACK_SENT" if sent else "NO_INCOMING_MESSAGE"
        )
        return

    print("=" * 90)
    print("🧪 RAW MESSAGE SENT TO ENGINE:")
    print(repr(latest_message))
    print("=" * 90)

    classification = classify_whatsapp_message(latest_message, media_info=media_info)
    log_classification(contact_name, customer_contact, latest_message, classification)

    if re.search(r"WA-\d{8}-[A-Z0-9]+-[A-Z0-9]+", latest_message, re.I):
        send_classification_alert(driver, contact_name, customer_contact, latest_message, classification)
        process_supplier_reply(driver, contact_name, latest_message)
        return

    if classification.handler == "supplier_reply":
        send_classification_alert(driver, contact_name, customer_contact, latest_message, classification)
        process_supplier_reply(driver, contact_name, latest_message)
        return

    if classification.handler == "skip":
        send_classification_alert(driver, contact_name, customer_contact, latest_message, classification)
        process_classified_non_inquiry(
            driver, contact_name, latest_message, classification,
            customer_contact=customer_contact, image_analysis=image_analysis,
            document_items=document_items,
        )
        return

    if classification.handler == "rfq_inquiry":
        process_customer_inquiry(
            driver,
            contact_name,
            latest_message,
            image_analysis=image_analysis,
            customer_contact=customer_contact,
            classification=classification,
            document_items=document_items,
        )
        return

    process_classified_non_inquiry(
        driver,
        contact_name,
        latest_message,
        classification,
        customer_contact=customer_contact,
        image_analysis=image_analysis,
        document_items=document_items,
    )


def process_mark_unread_request(driver):
    if not os.path.exists(MARK_UNREAD_FLAG):
        return False

    contact = "+60 16-722 2208"

    try:
        with open(MARK_UNREAD_FLAG, "r", encoding="utf-8") as f:
            contact = f.read().strip() or contact
    except Exception as e:
        print(f"⚠️ Could not read mark-unread flag file: {e}")

    try:
        os.remove(MARK_UNREAD_FLAG)
    except Exception as e:
        print(f"⚠️ Could not remove mark-unread flag file: {e}")

    print("")
    print("=" * 90)
    print("📌 MARK UNREAD REQUEST RECEIVED")
    print(f"   Contact: {contact}")
    print("=" * 90)

    mark_chat_as_unread(driver, contact)
    return True


def watch_unread_with_existing_driver(driver):
    print("🌐 Opening WhatsApp Web...")
    driver.get("https://web.whatsapp.com")

    if not wait_for_whatsapp_ready(driver):
        return

    if process_mark_unread_request(driver):
        print("↩️ Returning to WhatsApp chat list after mark-unread...")
        driver.get("https://web.whatsapp.com")
        wait_for_whatsapp_ready(driver, timeout=30)
        time.sleep(2)

    process_monitor_feedback(driver)

    unread_rows = find_unread_chat_rows(driver)

    if not unread_rows:
        print("✅ No unread WhatsApp chats found.")
        return

    for idx, row in enumerate(unread_rows, start=1):
        print("")
        print("-" * 90)
        print(f"📬 Processing unread chat {idx}/{len(unread_rows)}")

        if not open_unread_chat(row):
            continue

        process_open_chat(driver)

        time.sleep(3)

        print("↩️ Returning to WhatsApp chat list...")
        driver.get("https://web.whatsapp.com")
        wait_for_whatsapp_ready(driver, timeout=30)
        time.sleep(2)


def run_persistent_watcher():
    print(f"🚀 WhatsApp Watcher Persistent Mode ({VERSION})")
    print(f"⏱️ Check interval: {CHECK_INTERVAL_SECONDS} seconds")
    print(f"📁 Chrome Profile: {OPENCLAW_CHROME_PROFILE}")

    driver = None

    try:
        driver = init_driver()

        while True:
            try:
                print("")
                print("=" * 90)
                print(f"🔁 New WhatsApp scan cycle @ {now_iso()}")

                watch_unread_with_existing_driver(driver)

            except Exception as e:
                print(f"⚠️ WhatsApp scan cycle error: {e}")

                try:
                    print("🔄 Recovering WhatsApp Web page...")
                    driver.get("https://web.whatsapp.com")
                    wait_for_whatsapp_ready(driver, timeout=60)
                except Exception as recover_error:
                    print(f"❌ Recovery failed: {recover_error}")
                    print("🔁 Restarting Chrome driver...")

                    try:
                        driver.quit()
                    except Exception:
                        pass

                    driver = init_driver()

            print(f"⏳ Sleeping {CHECK_INTERVAL_SECONDS} seconds...")
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("🛑 WhatsApp watcher stopped by user.")

    finally:
        if driver:
            print("🧹 Closing Chrome driver...")
            driver.quit()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "mark-unread":
        contact = sys.argv[2] if len(sys.argv) > 2 else "+60 16-722 2208"
        driver = None

        try:
            driver = init_driver()
            success = mark_chat_as_unread(driver, contact)
            sys.exit(0 if success else 1)
        finally:
            if driver:
                driver.quit()
    else:
        run_persistent_watcher()
