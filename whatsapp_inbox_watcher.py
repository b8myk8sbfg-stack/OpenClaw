import os
import re
import time
import csv
import json
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
from openclaw_main import (
    extract_rfq_with_copilot,
    build_ai_research_summary,
    build_technical_support_reply,
    _resolve_visual_items,
)
from whatsapp_message_classifier import (
    INTENT_TYPES,
    build_classification_monitor_message,
    classify_whatsapp_message,
    detect_bubble_media,
    is_equivalent_support_request,
    log_classification,
)
from whatsapp_attachment_processor import (
    VOICE_LATEST_OPUS,
    clear_wa_audio_workspace,
    enrich_message_from_attachments,
    ensure_voice_transcript,
)
from message_learning_store import apply_feedback_command

VERSION = "v3.41-COPILOT-TECH-SUPPORT"

CHROME_BINARY_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Users/evon/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

OPENCLAW_CHROME_PROFILE = "/Users/evon/OpenClaw/chrome_whatsapp_profile"
WHATSAPP_INQUIRY_LOG = "/Users/evon/OpenClaw/whatsapp_inquiries.csv"
SUPPLIER_PENDING_CSV = "/Users/evon/OpenClaw/whatsapp_supplier_pending.csv"
MARK_UNREAD_FLAG = "/Users/evon/OpenClaw/whatsapp_mark_unread.flag"
PROCESS_CONTACT_FLAG = "/Users/evon/OpenClaw/whatsapp_process_contact.flag"
WHATSAPP_WATCH_CONTACTS_FILE = "/Users/evon/OpenClaw/whatsapp_watch_contacts.txt"
WHATSAPP_LAST_PROCESSED_FILE = "/Users/evon/OpenClaw/whatsapp_last_processed.json"
WHATSAPP_CUSTOMER_REGISTRY_FILE = "/Users/evon/OpenClaw/whatsapp_customer_registry.json"
IMAGE_CAPTURE_DIR = "/Users/evon/OpenClaw/logs/wa_image_capture"
CUSTOMER_REPLY_MODE_FILE = "/Users/evon/OpenClaw/openclaw_whatsapp_reply_mode.txt"

MAX_UNREAD_CHATS_PER_RUN = 1
CHECK_INTERVAL_SECONDS = int(os.getenv("OPENCLAW_WHATSAPP_POLL_SECONDS", "45"))
INCOMING_LOOKBACK = 6
MARKUP_DIVISOR = 0.8
MONITOR_WHATSAPP_PHONE = os.getenv("OPENCLAW_MONITOR_WHATSAPP_PHONE", "+60167222208")

WHATSAPP_SESSION_READY = False
CHAT_PROCESSING_LOCK = False


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def acquire_chat_processing_lock(label: str) -> bool:
    global CHAT_PROCESSING_LOCK
    if CHAT_PROCESSING_LOCK:
        print(f"⏸️ Skipping {label} — another chat is still being processed")
        return False
    CHAT_PROCESSING_LOCK = True
    print(f"🔒 Chat processing lock ON ({label})")
    return True


def release_chat_processing_lock():
    global CHAT_PROCESSING_LOCK
    CHAT_PROCESSING_LOCK = False
    print("🔓 Chat processing lock OFF")


def whatsapp_session_is_ready(driver) -> bool:
    try:
        return bool(wait_for_search_box(driver, timeout=2))
    except Exception:
        return False


def ensure_whatsapp_session(driver, force_reload: bool = False) -> bool:
    """Load WhatsApp Web once; avoid full page reload every poll cycle."""
    global WHATSAPP_SESSION_READY

    if force_reload:
        WHATSAPP_SESSION_READY = False

    if WHATSAPP_SESSION_READY and whatsapp_session_is_ready(driver) and not force_reload:
        return True

    print("🌐 Loading WhatsApp Web session...")
    driver.get("https://web.whatsapp.com")
    if not wait_for_whatsapp_ready(driver):
        WHATSAPP_SESSION_READY = False
        return False

    WHATSAPP_SESSION_READY = True
    return True


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

    driver = webdriver.Chrome(options=options)
    _install_whatsapp_voice_hooks(driver)
    return driver


def _install_whatsapp_voice_hooks(driver):
    """Hook fetch/XHR on WhatsApp pages so voice media URLs are captured without replaying."""
    if getattr(driver, "_openclaw_voice_hooks_installed", False):
        return
    hook_js = """
window.__openclawVoiceUrls = window.__openclawVoiceUrls || [];
if (!window.__openclawVoiceHooked) {
  window.__openclawVoiceHooked = true;
  const push = (url) => {
    if (!url || typeof url !== 'string') return;
    if (url.indexOf('blob:') === 0 || url.indexOf('mmg.whatsapp.net') > -1) {
      window.__openclawVoiceUrls.push(url);
    }
  };
  const origFetch = window.fetch;
  window.fetch = function(input, init) {
    push(typeof input === 'string' ? input : (input && input.url) || '');
    return origFetch.apply(this, arguments);
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    push(String(url || ''));
    return origOpen.apply(this, arguments);
  };
  if (window.URL && window.URL.createObjectURL) {
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = function(blob) {
      const url = origCreate.call(URL, blob);
      push(url);
      return url;
    };
  }
}
"""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": hook_js},
        )
    except Exception:
        pass
    try:
        driver.execute_script(hook_js)
        driver._openclaw_voice_hooks_installed = True
    except Exception:
        pass


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


def wait_for_search_box(driver, timeout=20):
    """Wait until the left-sidebar search field is visible."""
    end = time.time() + timeout
    selectors = [
        (By.CSS_SELECTOR, '#side div[contenteditable="true"]'),
        (By.CSS_SELECTOR, '#side [role="textbox"][contenteditable="true"]'),
        (By.CSS_SELECTOR, 'div[aria-label="Search input textbox"][contenteditable="true"]'),
        (By.CSS_SELECTOR, 'div[aria-label*="Search or start" i][contenteditable="true"]'),
        (By.CSS_SELECTOR, 'div[title="Search input textbox"][contenteditable="true"]'),
        (By.XPATH, '//div[@contenteditable="true"][@data-tab="3"]'),
        (By.XPATH, '//div[@contenteditable="true"][@title="Search input textbox"]'),
        (By.XPATH, '//div[@role="textbox"][@contenteditable="true"]'),
    ]
    while time.time() < end:
        for by, selector in selectors:
            try:
                for el in driver.find_elements(by, selector):
                    if el.is_displayed():
                        return el
            except Exception:
                continue
        try:
            el = driver.execute_script(
                """
                const side = document.querySelector('#side');
                if (!side) return null;
                for (const node of side.querySelectorAll('[contenteditable="true"], [role="textbox"]')) {
                    const rect = node.getBoundingClientRect();
                    if (rect.width > 40 && rect.height > 8 && rect.top < 500) return node;
                }
                return null;
                """
            )
            if el is not None:
                return el
        except Exception:
            pass
        time.sleep(0.5)
    return None


def reveal_whatsapp_search(driver):
    """Click the sidebar search control if the search field is collapsed."""
    if wait_for_search_box(driver, timeout=2):
        return True

    trigger_selectors = [
        'button[aria-label*="Search" i]',
        'div[title="Search"]',
        'span[data-icon="search"]',
        'span[data-icon="search-refreshed"]',
        'span[data-icon="search-refreshed-thin"]',
        '#side header button',
        '#side [data-testid="chat-list-search"]',
    ]
    for selector in trigger_selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if not el.is_displayed():
                    continue
                target = el
                if el.tag_name.lower() != "button":
                    try:
                        target = el.find_element(By.XPATH, "./ancestor::button[1]")
                    except Exception:
                        pass
                driver.execute_script("arguments[0].click();", target)
                time.sleep(1)
                if wait_for_search_box(driver, timeout=5):
                    return True
        except Exception:
            continue
    return wait_for_search_box(driver, timeout=3) is not None


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
                ' | .//span[@data-testid="icon-unread-count"]'
                ' | .//*[@data-testid="unread-count"]'
                ' | .//*[contains(@aria-label, "unread message")]'
            )

            has_unread = bool(unread_markers)

            if not has_unread:
                try:
                    badge_spans = row.find_elements(
                        By.CSS_SELECTOR,
                        'span[data-testid="icon-unread-count"], span[aria-label*="unread"]',
                    )
                    has_unread = bool(badge_spans)
                except Exception:
                    pass

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


def load_customer_registry():
    if not os.path.exists(WHATSAPP_CUSTOMER_REGISTRY_FILE):
        return []
    try:
        with open(WHATSAPP_CUSTOMER_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Legacy dict keyed by phone — convert to list without injecting phone numbers.
            return list(data.values())
        return []
    except Exception as exc:
        print(f"⚠️ Could not read customer registry: {exc}")
        return []


def registry_entry_for_hint(contact_hint):
    hint = str(contact_hint or "").strip().lower()
    if not hint:
        return None

    for entry in load_customer_registry():
        aliases = [str(a).strip().lower() for a in entry.get("aliases", [])]
        groups = [str(g).strip().lower() for g in entry.get("group_names", [])]
        canonical = str(entry.get("canonical_name") or "").strip().lower()

        if hint in aliases or hint == canonical:
            return entry
        if hint in groups:
            return entry
        if any(hint in group or group.startswith(hint) for group in groups):
            return entry

    return None


def contact_store_key(contact_name: str = "", contact_hint: str = "") -> str:
    """Stable key for whatsapp_last_processed.json across aliases."""
    for hint in (contact_name, contact_hint):
        entry = registry_entry_for_hint(hint)
        if entry:
            canonical = str(entry.get("canonical_name") or "").strip()
            if canonical:
                return canonical.lower()
    return str(contact_name or contact_hint or "").strip().lower()


def watch_read_contacts_enabled() -> bool:
    return os.getenv("OPENCLAW_WHATSAPP_WATCH_READ_CONTACTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def resolve_search_hint(contact_hint):
    """Map group aliases like 'PSHI' to the preferred 1:1 contact for WhatsApp search."""
    entry = registry_entry_for_hint(contact_hint)
    if not entry:
        return contact_hint
    return entry.get("search_prefer") or entry.get("canonical_name") or contact_hint


def looks_like_group_chat_name(name):
    text = str(name or "").strip().lower()
    if not text:
        return False
    markers = ("consignment", "group chat", "participants", "community")
    return any(marker in text for marker in markers)


def is_monitor_phone(phone):
    return normalize_phone(phone) == normalize_phone(MONITOR_WHATSAPP_PHONE)


def format_customer_phone_display(phone, allow_monitor_match: bool = False):
    """Format a WhatsApp-extracted phone for monitor messages (digits only)."""
    digits = normalize_phone(phone)
    if not digits or len(digits) < 8 or len(digits) > 15:
        return ""
    if not allow_monitor_match and is_monitor_phone(digits):
        return ""
    return digits


def normalize_whatsapp_customer_phone(phone, allow_monitor_match: bool = False):
    """Keep only a plausible customer phone scraped from WhatsApp DOM."""
    return format_customer_phone_display(phone, allow_monitor_match=allow_monitor_match)


def resolve_customer_identity(contact_name, customer_phone):
    """
    Fix group-vs-1:1 display names only.
    Phone must come from WhatsApp DOM — never from registry or monitor config.
    """
    canonical_name = str(contact_name or "").strip() or "WhatsApp Customer"
    entry = registry_entry_for_hint(canonical_name)

    if entry:
        group_names = [str(g).strip().lower() for g in entry.get("group_names", [])]
        name_lower = canonical_name.lower()

        if (
            looks_like_group_chat_name(canonical_name)
            or name_lower in group_names
            or any(group in name_lower for group in group_names)
        ):
            canonical_name = entry.get("canonical_name") or canonical_name

    phone = normalize_whatsapp_customer_phone(customer_phone)
    return canonical_name, phone


SELECT_DIRECT_CHAT_JS = """
const hints = arguments[0].map(function(h) { return String(h || '').toLowerCase(); });
const spans = Array.from(document.querySelectorAll('span[title]'));
const scored = [];

function sectionFlags(node) {
    let inGroups = false;
    let inMessages = false;
    let cur = node;
    for (let i = 0; i < 15 && cur; i++) {
        const text = (cur.innerText || '').slice(0, 160);
        if (/groups in common/i.test(text)) inGroups = true;
        if (/\\bmessages\\b/i.test(text) && text.length < 60) inMessages = true;
        cur = cur.parentElement;
    }
    return { inGroups: inGroups, inMessages: inMessages };
}

for (const span of spans) {
    const title = (span.getAttribute('title') || '').trim();
    if (!title) continue;
    // Ignore sidebar chat-list titles — clicking those does not open #main on Business Web.
    if (span.closest('#side [data-testid="cell-frame-container"], #side [role="listitem"]')) {
        continue;
    }
    const lower = title.toLowerCase();
    let matched = false;
    for (const h of hints) {
        if (!h) continue;
        if (lower.includes(h) || h.includes(lower)) {
            matched = true;
            break;
        }
    }
    if (!matched) continue;

    const flags = sectionFlags(span);
    let score = 0;
    if (flags.inMessages) score += 100;
    if (flags.inGroups && !flags.inMessages) score -= 200;
    if (/consignment|group chat|participants|community/i.test(lower)) score -= 150;
    if (/^\\+?\\d[\\d\\s\\-()]+$/.test(title)) score -= 25;

    scored.push({ span: span, title: title, score: score });
}

scored.sort(function(a, b) { return b.score - a.score; });
if (!scored.length) return null;
scored[0].span.click();
return scored[0].title;
"""


OPEN_CHAT_FROM_LIST_JS = """
const hints = arguments[0].map(function(h) { return String(h || '').toLowerCase(); }).filter(Boolean);
const side = document.querySelector('#side');
if (!side) return null;

function matchesHint(text, title) {
    const blob = ((text || '') + ' ' + (title || '')).toLowerCase();
    for (const h of hints) {
        if (!h) continue;
        if (blob.includes(h) || h.includes((title || '').toLowerCase())) return true;
    }
    return false;
}

function isGroup(title, text) {
    const blob = ((title || '') + ' ' + (text || '')).toLowerCase();
    return /group chat|participants|consignment|community|\\(\\d+\\)/.test(blob);
}

const rows = side.querySelectorAll(
    '[data-testid="cell-frame-container"], [role="listitem"], [role="row"], [data-testid="list-item"]'
);
for (const row of rows) {
    const titleEl = row.querySelector('span[title]');
    const title = titleEl ? (titleEl.getAttribute('title') || '').trim() : '';
    const text = (row.innerText || '').trim();
    if (!matchesHint(text, title)) continue;
    if (isGroup(title, text)) continue;
    const target = row.matches('[data-testid="cell-frame-container"]')
        ? row
        : (row.querySelector('[data-testid="cell-frame-container"]') || row);
    target.scrollIntoView({ block: 'center' });
    target.click();
    return title || text.split('\\n')[0] || null;
}
return null;
"""


IS_CHAT_OPEN_JS = """
const main = document.querySelector('#main');
if (!main) return false;
const sample = (main.innerText || '').slice(0, 800);
if (/WhatsApp Business on Web/i.test(sample) && /Grow,? organise/i.test(sample)) {
    return false;
}
return !!(
    main.querySelector('[data-testid="conversation-panel-messages"]') ||
    main.querySelector('footer') ||
    main.querySelector('[data-testid="conversation-compose-box-input"]') ||
    main.querySelector('[contenteditable="true"][data-tab="10"]') ||
    main.querySelector('[data-pre-plain-text]') ||
    main.querySelector('[data-testid="msg-container"]')
);
"""


def find_chat_list_row(driver, contact_hint):
    hints = contact_hint_variants(resolve_search_hint(contact_hint))

    row_selectors = [
        (By.CSS_SELECTOR, '#side [data-testid="cell-frame-container"]'),
        (By.XPATH, '//div[@role="listitem"]'),
        (By.XPATH, '//div[contains(@aria-label, "Chat list")]//div[@role="row"]'),
    ]

    rows = []
    for by, selector in row_selectors:
        try:
            found = driver.find_elements(by, selector)
            if found:
                rows = found
                break
        except Exception:
            continue

    for row in rows:
        try:
            row_text = (row.text or "").lower()

            for hint in hints:
                if hint.lower() in row_text:
                    if looks_like_group_chat_name(row_text):
                        continue
                    return row

            for title_el in row.find_elements(By.XPATH, './/span[@title]'):
                title = (title_el.get_attribute("title") or "").strip()
                title_lower = title.lower()

                for hint in hints:
                    if hint.lower() in title_lower:
                        if looks_like_group_chat_name(title):
                            continue
                        return row

        except Exception:
            continue

    return None


def click_sidebar_chat_cell(driver, contact_hint):
    """Click a visible sidebar chat row (cell-frame-container) by contact title."""
    resolved = resolve_search_hint(contact_hint)
    hints = contact_hint_variants(resolved)

    for cell in driver.find_elements(By.CSS_SELECTOR, '#side [data-testid="cell-frame-container"]'):
        try:
            title = ""
            for title_el in cell.find_elements(By.CSS_SELECTOR, "span[title]"):
                title = (title_el.get_attribute("title") or "").strip()
                if title:
                    break
            blob = f"{title} {(cell.text or '')}".lower()
            if not any(h.lower() in blob for h in hints):
                continue
            if title and looks_like_group_chat_name(title):
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cell)
            time.sleep(0.4)
            driver.execute_script("arguments[0].click();", cell)
            time.sleep(2)
            if wait_for_open_chat_panel(driver, timeout=20):
                print(f"✅ Opened chat cell: {title or resolved}")
                return True
        except Exception:
            continue
    return False


def open_chat_by_contact(driver, contact_hint):
    """
    Open a direct chat — prefer clicking the sidebar chat row (works without search box),
    then fall back to WhatsApp search.
    """
    resolved_hint = resolve_search_hint(contact_hint)
    if resolved_hint != contact_hint:
        print(f"🔎 Search hint mapped: {contact_hint!r} → {resolved_hint!r}")

    if not wait_for_whatsapp_ready(driver, timeout=30):
        return False

    time.sleep(1.5)

    if click_sidebar_chat_cell(driver, contact_hint):
        return True

    row = find_chat_list_row(driver, contact_hint)
    if row is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", row)
            time.sleep(2)
            if wait_for_open_chat_panel(driver, timeout=25):
                print("✅ Opened chat from sidebar list")
                return True
        except Exception as exc:
            print(f"⚠️ Sidebar list click failed: {exc}")

    try:
        opened = driver.execute_script(
            OPEN_CHAT_FROM_LIST_JS,
            contact_hint_variants(resolved_hint),
        )
        if opened:
            time.sleep(2)
            if wait_for_open_chat_panel(driver, timeout=25):
                print(f"✅ Opened chat via JS list scan: {opened}")
                return True
    except Exception as exc:
        print(f"⚠️ JS list open failed: {exc}")

    print("ℹ️ Chat not found in visible sidebar — trying search...")
    return open_chat_via_search(driver, contact_hint)


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
    resolved_hint = resolve_search_hint(contact_hint)
    if resolved_hint != contact_hint:
        print(f"🔎 Search hint mapped: {contact_hint!r} → {resolved_hint!r}")

    hints = contact_hint_variants(resolved_hint)

    if not reveal_whatsapp_search(driver):
        try:
            count = driver.execute_script(
                "return document.querySelectorAll('#side [contenteditable=\"true\"]').length;"
            )
            print(f"❌ WhatsApp search box not found. (#side contenteditable count: {count})")
        except Exception:
            print("❌ WhatsApp search box not found.")
        return False

    search_box = wait_for_search_box(driver, timeout=5)
    if not search_box:
        print("❌ WhatsApp search box not found after opening search.")
        return False

    search_box.click()
    time.sleep(0.5)

    for key_combo in [Keys.COMMAND, Keys.CONTROL]:
        search_box.send_keys(key_combo, "a")
        search_box.send_keys(Keys.BACKSPACE)

    search_box.send_keys(hints[0])
    time.sleep(2)

    try:
        opened_title = driver.execute_script(SELECT_DIRECT_CHAT_JS, hints)
        if opened_title:
            time.sleep(2)
            if wait_for_open_chat_panel(driver, timeout=30):
                print(f"✅ Opened direct chat via search: {opened_title}")
                return True
            print(f"⚠️ Search selected {opened_title!r} but conversation panel did not open.")
    except Exception as exc:
        print(f"⚠️ Direct-chat search JS failed: {exc}")

    title_selectors = [
        f'//span[@title="{hint}"]' for hint in hints
    ]

    for selector in title_selectors:
        try:
            results = driver.find_elements(By.XPATH, selector)

            for result in results:
                title = (result.get_attribute("title") or "").strip()
                title_lower = title.lower()

                if not any(hint.lower() in title_lower for hint in hints):
                    continue
                if looks_like_group_chat_name(title):
                    print(f"   ⏭️ Skipping group search result: {title}")
                    continue
                try:
                    in_sidebar = driver.execute_script(
                        "return !!arguments[0].closest('#side [data-testid=\"cell-frame-container\"]');",
                        result,
                    )
                    if in_sidebar:
                        continue
                except Exception:
                    pass

                driver.execute_script("arguments[0].click();", result)
                time.sleep(2)
                if wait_for_open_chat_panel(driver, timeout=30):
                    print(f"✅ Opened chat via search: {title}")
                    return True
                print(f"⚠️ Search click on {title!r} did not open conversation panel.")

        except Exception:
            continue

    print(f"❌ Could not find direct chat via search for: {contact_hint}")
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


SCRAPE_CONTACT_DRAWER_PHONE_JS = """
const monitorDigits = String(arguments[0] || '').replace(/\\D/g, '');
const allowMonitor = !!arguments[1];

function cleanPhone(raw) {
    const digits = String(raw || '').replace(/\\D/g, '');
    if (digits.length < 8 || digits.length > 15) return '';
    if (!allowMonitor && monitorDigits && digits === monitorDigits) return '';
    return digits;
}

function looksLikePhoneLine(line) {
    const t = String(line || '').trim();
    if (!t) return false;
    if (!/^[+\\d\\s\\-()]+$/.test(t)) return false;
    const digits = t.replace(/\\D/g, '');
    return digits.length >= 8 && digits.length <= 15;
}

function isAddressOrBusinessLine(line) {
    const t = String(line || '').trim();
    if (!t) return true;
    if (/^(contact info|close|edit)$/i.test(t)) return true;
    if (/^(open now|closed|business account|whatsapp business)$/i.test(t)) return true;
    if (/^(search|add notes|media|links|starred|mute|encryption|groups in common|about and phone number)$/i.test(t)) return true;
    if (/^https?:\\/\\//i.test(t)) return true;
    if (/@/.test(t)) return true;
    if (/\\b(jalan|malaysia|singapore|kawasan|perindustrian|tampoi|johore|johor|street|road|avenue|blvd|postcode|address)\\b/i.test(t)) return true;
    if (/\\b\\d{1,4}\\s*,\\s*[A-Za-z]/.test(t)) return true;
    if (/\\b\\d{1,2}:\\d{2}\\s*(am|pm)?\\b/i.test(t) && /\\d{1,2}:\\d{2}/.test(t)) return true;
    if (/[A-Za-z]{3,}/.test(t) && !/^\\+/.test(t)) return true;
    return false;
}

function phoneFromLine(line) {
    if (!looksLikePhoneLine(line)) return '';
    return cleanPhone(line);
}

function findContactPanel() {
    const selectors = [
        '[data-testid="drawer-right"]',
        '[data-testid="contact-info-drawer"]',
        '[data-testid="contact-info"]',
        'section[data-testid="contact-info"]',
    ];
    for (const sel of selectors) {
        const nodes = document.querySelectorAll(sel);
        for (const node of nodes) {
            const text = node.innerText || '';
            if (/contact info/i.test(text)) return node;
        }
    }
    return document.querySelector('[data-testid="drawer-right"]') || null;
}

function scrapeBusinessPhone(lines) {
    for (let i = 0; i < lines.length; i++) {
        if (!/about and phone number/i.test(lines[i])) continue;
        for (let j = i + 1; j < Math.min(i + 8, lines.length); j++) {
            if (/^(last seen|about|media|links|starred|mute|encryption|groups in common|search|add notes)/i.test(lines[j])) break;
            const p = phoneFromLine(lines[j]);
            if (p) return p;
        }
    }
    return '';
}

function scrapeStandardPhoneUnderName(lines) {
    let seenName = false;
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (/^contact info$/i.test(line)) continue;
        if (/^(search|add notes|media|links|starred|mute|encryption|groups in common)$/i.test(line)) break;
        if (/about and phone number/i.test(line)) break;
        if (isAddressOrBusinessLine(line)) {
            if (seenName) break;
            continue;
        }
        if (!seenName) {
            seenName = true;
            continue;
        }
        const p = phoneFromLine(line);
        if (p) return p;
    }
    return '';
}

const panel = findContactPanel();
if (!panel) return '';

for (const sel of [
    '[data-testid="phone-number"]',
    '[data-testid="contact-phone-number"]',
    'a[href^="tel:"]',
]) {
    for (const node of panel.querySelectorAll(sel)) {
        const raw = node.getAttribute('href') || node.innerText || node.textContent || '';
        const p = cleanPhone(String(raw).replace(/^tel:/i, ''));
        if (p) return p;
    }
}

const lines = (panel.innerText || '').split('\\n').map(function(s) { return s.trim(); }).filter(Boolean);

const businessPhone = scrapeBusinessPhone(lines);
if (businessPhone) return businessPhone;

const standardPhone = scrapeStandardPhoneUnderName(lines);
if (standardPhone) return standardPhone;

return '';
"""


EXTRACT_OPEN_CHAT_PHONE_JS = """
const monitorDigits = String(arguments[0] || '').replace(/\\D/g, '');

function cleanPhone(raw) {
    const digits = String(raw || '').replace(/\\D/g, '');
    if (digits.length < 8 || digits.length > 15) return '';
    if (monitorDigits && digits === monitorDigits) return '';
    return digits;
}

function looksLikePhoneLine(line) {
    const t = String(line || '').trim();
    if (!t) return false;
    if (!/^[+\\d\\s\\-()]+$/.test(t)) return false;
    const digits = t.replace(/\\D/g, '');
    return digits.length >= 8 && digits.length <= 15;
}

function phoneFromLine(line) {
    if (!looksLikePhoneLine(line)) return '';
    return cleanPhone(line);
}

function scrapeBusinessPhone(lines) {
    for (let i = 0; i < lines.length; i++) {
        if (!/about and phone number/i.test(lines[i])) continue;
        for (let j = i + 1; j < Math.min(i + 8, lines.length); j++) {
            if (/^(last seen|about|media|links|starred|mute|encryption|groups in common|search|add notes)/i.test(lines[j])) break;
            const p = phoneFromLine(lines[j]);
            if (p) return p;
        }
    }
    return '';
}

function scrapeStandardPhoneUnderName(lines) {
    let seenName = false;
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (/^contact info$/i.test(line)) continue;
        if (/^(search|add notes|media|links|starred|mute|encryption|groups in common)$/i.test(line)) break;
        if (/about and phone number/i.test(line)) break;
        if (/\\b(jalan|malaysia|singapore|kawasan|perindustrian|https?:|\\@)/i.test(line)) {
            if (seenName) break;
            continue;
        }
        if (/[A-Za-z]{3,}/.test(line) && !/^\\+/.test(line)) {
            if (seenName) break;
            seenName = true;
            continue;
        }
        if (!seenName) {
            seenName = true;
            continue;
        }
        const p = phoneFromLine(line);
        if (p) return p;
    }
    return '';
}

// Contact info drawer if already open
for (const sel of [
    '[data-testid="contact-info-drawer"]',
    '[data-testid="drawer-right"]',
    'div[data-testid="contact-info"]',
]) {
    const drawer = document.querySelector(sel);
    if (!drawer) continue;
    const lines = (drawer.innerText || '').split('\\n').map(function(s) { return s.trim(); }).filter(Boolean);
    const businessPhone = scrapeBusinessPhone(lines);
    if (businessPhone) return businessPhone;
    const standardPhone = scrapeStandardPhoneUnderName(lines);
    if (standardPhone) return standardPhone;
}

// Conversation header subtitle (standard WhatsApp often shows phone here)
const header = document.querySelector('#main header');
if (header) {
    for (const node of header.querySelectorAll('[title]')) {
        const p = phoneFromLine(node.getAttribute('title') || '');
        if (p) return p;
    }
    const lines = (header.innerText || '').split('\\n').map(function(s) { return s.trim(); }).filter(Boolean);
    for (let i = 1; i < lines.length; i++) {
        const p = phoneFromLine(lines[i]);
        if (p) return p;
    }
}

return '';
"""


def contact_info_drawer_is_open(driver):
    try:
        return bool(driver.execute_script(
            "const t = document.body.innerText || '';"
            "if (!/Contact info/i.test(t)) return false;"
            "if (/About and phone number/i.test(t)) return true;"
            "const drawer = document.querySelector('[data-testid=\"drawer-right\"]');"
            "if (!drawer) return false;"
            "const lines = (drawer.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);"
            "let seenName = false;"
            "for (const line of lines) {"
            "  if (/^(search|add notes|groups in common)$/i.test(line)) break;"
            "  if (/\\b(jalan|malaysia|https?:|@)/i.test(line)) { if (seenName) break; continue; }"
            "  if (/^[+\\d\\s\\-()]+$/.test(line) && line.replace(/\\D/g,'').length >= 8) return true;"
            "  if (!/^contact info$/i.test(line) && line.length > 1) seenName = true;"
            "}"
            "return false;"
        ))
    except Exception:
        return False


def open_contact_info_drawer(driver):
    """Click chat header profile to open the Contact info panel on the right."""
    if contact_info_drawer_is_open(driver):
        return True

    click_selectors = [
        '#main header img[draggable="false"]',
        '#main header img',
        '#main header [data-testid="conversation-info-header"]',
        '#main header div[role="button"]',
        '#main header span[dir="auto"]',
    ]

    for selector in click_selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if not element.is_displayed():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();",
                    element,
                )
                time.sleep(2)
                if contact_info_drawer_is_open(driver):
                    print("✅ Opened WhatsApp Contact info drawer.")
                    return True
        except Exception:
            continue

    print("⚠️ Could not open WhatsApp Contact info drawer.")
    return False


def close_contact_info_drawer(driver):
    if not contact_info_drawer_is_open(driver):
        return

    for selector in (
        '[data-testid="btn-closer-drawer"]',
        '[data-testid="drawer-back"]',
        'span[data-testid="back"]',
        'header [data-icon="x"]',
        '[aria-label="Close"]',
    ):
        try:
            for button in driver.find_elements(By.CSS_SELECTOR, selector):
                if button.is_displayed():
                    driver.execute_script("arguments[0].click();", button)
                    time.sleep(0.8)
                    return
        except Exception:
            continue

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except Exception:
        pass


def scrape_contact_drawer_phone(driver, monitor_digits):
    try:
        raw = driver.execute_script(SCRAPE_CONTACT_DRAWER_PHONE_JS, monitor_digits, True)
        return normalize_whatsapp_customer_phone(raw, allow_monitor_match=True)
    except Exception as exc:
        print(f"⚠️ Contact info phone scrape failed: {exc}")
        return ""


def get_contact_phone_from_open_chat(driver, bubble=None):
    """
    Read customer phone from WhatsApp Contact info (profile panel on the right).
    Falls back to header text. Never uses message bubble JIDs.
    """
    monitor_digits = normalize_phone(MONITOR_WHATSAPP_PHONE)

    phone = scrape_contact_drawer_phone(driver, monitor_digits)
    if phone:
        if is_monitor_phone(phone):
            print(
                f"📞 Customer phone from WhatsApp Contact info: {phone} "
                "(matches monitor number — kept for 1:1 contact)"
            )
        else:
            print(f"📞 Customer phone from WhatsApp Contact info (drawer open): {phone}")
        return phone

    drawer_opened = False
    try:
        if open_contact_info_drawer(driver):
            drawer_opened = True
            time.sleep(1.5)
            phone = scrape_contact_drawer_phone(driver, monitor_digits)
            if phone:
                if is_monitor_phone(phone):
                    print(
                        f"📞 Customer phone from WhatsApp Contact info: {phone} "
                        "(matches monitor number — kept for 1:1 contact)"
                    )
                else:
                    print(f"📞 Customer phone from WhatsApp Contact info: {phone}")
                return phone
    finally:
        if drawer_opened:
            close_contact_info_drawer(driver)
            time.sleep(0.5)

    try:
        scraped = driver.execute_script(EXTRACT_OPEN_CHAT_PHONE_JS, monitor_digits)
        phone = normalize_whatsapp_customer_phone(scraped)
        if phone:
            print(f"📞 Customer phone from WhatsApp header UI: {phone}")
            return phone
    except Exception as exc:
        print(f"⚠️ WhatsApp phone scrape (JS) failed: {exc}")

    print("ℹ️ Customer phone not found in WhatsApp Contact info — omitting Customer Contact line.")
    return ""


def open_unread_chat(driver, row):
    try:
        row.click()
        time.sleep(3)
        wait_for_open_chat_panel(driver, timeout=30)
        return True
    except Exception as e:
        print(f"❌ Could not open unread chat: {e}")
        return False


def wait_for_open_chat_panel(driver, timeout=30):
    """Wait until a conversation is fully open (#main appears and landing page is gone)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script(IS_CHAT_OPEN_JS):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print("⚠️ Open chat panel (#main) did not appear in time.")
    return False


def wait_for_chat_messages(driver, timeout=20):
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


def is_playback_speed_label(text):
    """WhatsApp voice-note UI shows 1× / 1.5× / 2× — not message content."""
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*[×xX]", str(text or "").strip()))


def is_profile_or_ui_image_src(src):
    lowered = str(src or "").lower()
    if not lowered:
        return False
    return any(
        token in lowered
        for token in (
            "emoji",
            "avatar",
            "gif",
            "sticker",
            "pps.whatsapp",
            "profile",
            "contact-photo",
        )
    )


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

        if is_playback_speed_label(line):
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


def is_bot_noise_message(text):
    """Ignore OpenClaw monitor/learning echoes that appear in customer chat history."""
    normalized = str(text or "")
    return any(
        marker in normalized
        for marker in (
            "[OpenClaw Monitor Mode]",
            "[OpenClaw Learning Updated]",
            "Generated Reply:",
            "Context: QUOTATION_REPLY",
            "Context: NO_ITEMS",
            "Context: IMAGE_NO_ITEMS",
        )
    )


def is_monitor_noise_unit(unit) -> bool:
    """Monitor alerts misclassified as voice when long text is scraped from the bubble."""
    text = str(unit.get("text") or "")
    kind = str(unit.get("kind") or "")
    if is_bot_noise_message(text):
        return True
    if kind == "voice" and len(text) > 120 and ("OpenClaw" in text or "Classification:" in text):
        return True
    return False


def is_monitor_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", str(phone or ""))
    monitor = re.sub(r"\D", "", MONITOR_WHATSAPP_PHONE)
    if not digits or not monitor:
        return False
    return digits == monitor or digits.endswith(monitor[-9:])


def get_processed_data_ids(contact_name: str = "", contact_hint: str = "") -> set:
    store = load_last_processed_store()
    key = contact_store_key(contact_name, contact_hint)
    entry = store.get(key) or {}
    ids = entry.get("processed_data_ids") or []
    return set(str(x) for x in ids if x)


def filter_processable_units(units, contact_name: str = "", contact_hint: str = ""):
    """Keep only new, non-bot incoming customer messages."""
    processed_ids = get_processed_data_ids(contact_name, contact_hint)
    kept = []
    for unit in units or []:
        kind = unit.get("kind") or "empty"
        if kind == "empty":
            continue
        if is_monitor_noise_unit(unit):
            preview = str(unit.get("text") or "")[:70]
            print(f"   ⏭️ Skip bot/monitor echo ({kind}): {preview!r}...")
            continue
        data_id = str(unit.get("data_id") or "").strip()
        if data_id and data_id in processed_ids:
            print(f"   ⏭️ Skip already-processed WhatsApp message {data_id[:28]!r}")
            continue
        kept.append(unit)
    return kept


SCRAPE_INCOMING_JS = """
const lookback = arguments[0];
const main = document.querySelector('#main');
if (!main) {
    return { ok: false, reason: 'no_main', items: [] };
}
const mainSample = (main.innerText || '').slice(0, 800);
if (/WhatsApp Business on Web/i.test(mainSample) && /Grow,? organise/i.test(mainSample)) {
    return { ok: false, reason: 'landing_page', items: [] };
}
const panel = main.querySelector('[data-testid="conversation-panel-messages"]')
    || main.querySelector('[role="application"]')
    || main;
if (!panel) {
    return { ok: false, reason: 'no_panel', items: [] };
}

function isOutgoing(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        if (node.classList && node.classList.contains('message-out')) return true;
        if (node.classList && node.classList.contains('message-in')) return false;
        node = node.parentElement;
    }
    const copyables = container.querySelectorAll('[data-pre-plain-text]');
    for (const c of copyables) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) return true;
    }
    const dataId = resolveDataId(container);
    if (dataId && /^3EB/i.test(dataId)) return true;
    return false;
}

function isBotNoise(text) {
    const t = String(text || '');
    return /\\[OpenClaw Monitor Mode\\]/i.test(t)
        || /\\[OpenClaw Learning Updated\\]/i.test(t);
}

function resolveDataId(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        const id = node.getAttribute && node.getAttribute('data-id');
        if (id) return id;
        node = node.parentElement;
    }
    const child = container.querySelector('[data-id]');
    return child ? (child.getAttribute('data-id') || '') : '';
}

function isPlaybackSpeed(text) {
    return /^\\d+(?:\\.\\d+)?\\s*[×xX]$/.test(String(text || '').trim());
}

function hasRealImage(container) {
    const imgs = container.querySelectorAll(
        '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
        + 'img[src*="blob"]:not([src*="emoji"]), img[src*="mmg"], img[src*="cdn.whatsapp"]'
    );
    for (const img of imgs) {
        const src = (img.getAttribute('src') || '').toLowerCase();
        if (src.includes('pps.whatsapp') || src.includes('avatar') || src.includes('profile')) {
            continue;
        }
        return true;
    }
    return false;
}

function hasExplicitVoiceUi(container) {
    return !!container.querySelector(
        'audio, [data-testid="audio-play"], [data-testid="ptt-play-button"], [data-testid="ptt"], '
        + '[data-testid="audio"], [data-icon="ptt"], [data-icon="audio-play"], [data-icon="audio-download"]'
    );
}

function hasVoiceDurationLine(container) {
    const lines = (container.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
        if (/^\\d{1,2}:[0-5]\\d(\\s*[×xX])?$/.test(line)) return true;
    }
    return false;
}

function hasVoice(container) {
    if (hasExplicitVoiceUi(container)) return true;
    const text = extractText(container);
    if (text.length > 100) return false;
    if (container.querySelector('[data-testid="video-thumb"], video[src]')) {
        return false;
    }
    if (hasRealImage(container)) {
        return false;
    }
    if (hasVoiceDurationLine(container) && !!container.querySelector(
        'canvas, [data-testid="ptt"], span[data-icon="ptt"], span[data-icon="audio-play"]'
    )) {
        return true;
    }
    return false;
}

function hasImage(container) {
    if (hasVoice(container)) return false;
    if (hasRealImage(container)) return true;
    return false;
}

function hasDocument(container) {
    return !!container.querySelector(
        '[data-testid="document-thumb"], [data-icon="document"], [data-icon="document-pdf"]'
    );
}

function extractText(container) {
    const mediaCaption = container.querySelector(
        '[data-testid="media-caption"] [data-testid="selectable-text"], '
        + '[data-testid="media-caption"] span.selectable-text, '
        + '[data-testid="media-caption"]'
    );
    if (mediaCaption) {
        const t = (mediaCaption.innerText || mediaCaption.textContent || '').trim();
        if (t && !isPlaybackSpeed(t)) return t;
    }

    const parts = [];
    for (const c of container.querySelectorAll('[data-pre-plain-text]')) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) continue;
        const t = (c.innerText || c.textContent || '').trim();
        if (t && !isPlaybackSpeed(t)) parts.push(t);
    }
    if (parts.length) return parts.join('\\n').trim();
    const raw = (container.innerText || '').trim().slice(0, 800);
    const lines = raw.split('\\n').map(x => x.trim()).filter(x => x && !isPlaybackSpeed(x));
    return lines.join('\\n').trim();
}

const seen = new Set();
const incoming = [];

const candidates = panel.querySelectorAll(
    '[data-testid="msg-container"], div.message-in[data-id], div.message-in'
);

for (const node of candidates) {
    const container = node.matches('[data-testid="msg-container"]')
        ? node
        : (node.closest('[data-testid="msg-container"]') || node);
    if (!container || seen.has(container)) continue;
    if (isOutgoing(container)) continue;

    const voice = hasVoice(container);
    const img = hasImage(container);
    const doc = hasDocument(container);
    const text = extractText(container);
    if (isBotNoise(text)) continue;
    if (!voice && !img && !doc && !text) continue;

    seen.add(container);
    incoming.push({
        element: container,
        incomingIndex: incoming.length,
        dataId: resolveDataId(container),
        text: text,
        hasVoice: voice,
        hasImage: img,
        hasDocument: doc,
    });
}

const slice = incoming.slice(-lookback);
return { ok: true, reason: 'ok', total: incoming.length, items: slice };
"""


RELOCATE_INCOMING_BY_INDEX_JS = """
const targetIndex = arguments[0];
const panel = document.querySelector('#main [data-testid="conversation-panel-messages"]')
    || document.querySelector('#main [role="application"]')
    || document.querySelector('#main');
if (!panel) return null;

function resolveDataId(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        const id = node.getAttribute && node.getAttribute('data-id');
        if (id) return id;
        node = node.parentElement;
    }
    const child = container.querySelector('[data-id]');
    return child ? (child.getAttribute('data-id') || '') : '';
}

function isOutgoing(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        if (node.classList && node.classList.contains('message-out')) return true;
        if (node.classList && node.classList.contains('message-in')) return false;
        node = node.parentElement;
    }
    const copyables = container.querySelectorAll('[data-pre-plain-text]');
    for (const c of copyables) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) return true;
    }
    const dataId = resolveDataId(container);
    if (dataId && /^3EB/i.test(dataId)) return true;
    return false;
}

function isBotNoise(text) {
    const t = String(text || '');
    return /\\[OpenClaw Monitor Mode\\]/i.test(t)
        || /\\[OpenClaw Learning Updated\\]/i.test(t);
}

function isPlaybackSpeed(text) {
    return /^\\d+(?:\\.\\d+)?\\s*[×xX]$/.test(String(text || '').trim());
}

function hasRealImage(container) {
    const imgs = container.querySelectorAll(
        '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
        + 'img[src*="blob"]:not([src*="emoji"]), img[src*="mmg"], img[src*="cdn.whatsapp"]'
    );
    for (const img of imgs) {
        const src = (img.getAttribute('src') || '').toLowerCase();
        if (src.includes('pps.whatsapp') || src.includes('avatar') || src.includes('profile')) {
            continue;
        }
        return true;
    }
    return false;
}

function hasExplicitVoiceUi(container) {
    return !!container.querySelector(
        'audio, [data-testid="audio-play"], [data-testid="ptt-play-button"], [data-testid="ptt"], '
        + '[data-testid="audio"], [data-icon="ptt"], [data-icon="audio-play"], [data-icon="audio-download"]'
    );
}

function hasVoiceDurationLine(container) {
    const lines = (container.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
        if (/^\\d{1,2}:[0-5]\\d(\\s*[×xX])?$/.test(line)) return true;
    }
    return false;
}

function hasVoice(container) {
    if (hasExplicitVoiceUi(container)) return true;
    const text = extractText(container);
    if (text.length > 100) return false;
    if (container.querySelector('[data-testid="video-thumb"], video[src]')) {
        return false;
    }
    if (hasRealImage(container)) {
        return false;
    }
    if (hasVoiceDurationLine(container) && !!container.querySelector(
        'canvas, [data-testid="ptt"], span[data-icon="ptt"], span[data-icon="audio-play"]'
    )) {
        return true;
    }
    return false;
}

function hasImage(container) {
    if (hasVoice(container)) return false;
    if (hasRealImage(container)) return true;
    return false;
}

function extractText(container) {
    const mediaCaption = container.querySelector('[data-testid="media-caption"]');
    if (mediaCaption) {
        const t = (mediaCaption.innerText || mediaCaption.textContent || '').trim();
        if (t && !isPlaybackSpeed(t)) return t;
    }
    const parts = [];
    for (const c of container.querySelectorAll('[data-pre-plain-text]')) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) continue;
        const t = (c.innerText || c.textContent || '').trim();
        if (t && !isPlaybackSpeed(t)) parts.push(t);
    }
    const joined = parts.join('\\n').trim();
    if (joined) return joined;
    const raw = (container.innerText || '').trim().slice(0, 800);
    const lines = raw.split('\\n').map(x => x.trim()).filter(x => x && !isPlaybackSpeed(x));
    return lines.join('\\n').trim();
}

const seen = new Set();
const incoming = [];
for (const node of panel.querySelectorAll(
    '[data-testid="msg-container"], div.message-in[data-id], div.message-in'
)) {
    const container = node.matches('[data-testid="msg-container"]')
        ? node
        : (node.closest('[data-testid="msg-container"]') || node);
    if (!container || seen.has(container)) continue;
    if (isOutgoing(container)) continue;
    const text = extractText(container);
    if (isBotNoise(text)) continue;
    const voice = hasVoice(container);
    const img = hasImage(container);
    const doc = !!container.querySelector('[data-testid="document-thumb"], [data-icon="document"]');
    if (!voice && !img && !doc && !text) continue;
    seen.add(container);
    incoming.push(container);
}
if (targetIndex < 0 || targetIndex >= incoming.length) return null;
return incoming[targetIndex];
"""


RELOCATE_LAST_INCOMING_VOICE_JS = """
const lookback = arguments[0];
const panel = document.querySelector('#main [data-testid="conversation-panel-messages"]')
    || document.querySelector('#main');
if (!panel) return null;

function isOutgoing(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        if (node.classList && node.classList.contains('message-out')) return true;
        if (node.classList && node.classList.contains('message-in')) return false;
        node = node.parentElement;
    }
    for (const c of container.querySelectorAll('[data-pre-plain-text]')) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) return true;
    }
    return false;
}

function hasRealImage(container) {
    return !!container.querySelector(
        '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
        + 'img[src*="blob"]:not([src*="emoji"]), img[src*="mmg"], img[src*="cdn.whatsapp"]'
    );
}

function hasVoice(container) {
    if (container.querySelector(
        'audio, [data-testid="audio-play"], [data-testid="ptt-play-button"], [data-testid="ptt"], '
        + '[data-testid="audio"], [data-icon="ptt"], [data-icon="audio-play"]'
    )) {
        return true;
    }
    if (hasRealImage(container)) return false;
    const inner = container.innerText || '';
    const hasDuration = /\\b\\d{1,2}:\\d{2}\\b/.test(inner);
    const hasPlay = !!container.querySelector(
        'span[data-icon="audio-play"], span[data-icon="ptt"], [data-testid="audio-play"], canvas'
    );
    return hasDuration && hasPlay;
}

const voices = [];
const seen = new Set();
for (const node of panel.querySelectorAll('[data-testid="msg-container"], div.message-in')) {
    const container = node.matches('[data-testid="msg-container"]')
        ? node
        : (node.closest('[data-testid="msg-container"]') || node);
    if (!container || seen.has(container)) continue;
    if (isOutgoing(container)) continue;
    if (!hasVoice(container)) continue;
    seen.add(container);
    voices.push(container);
}
const slice = voices.slice(-lookback);
return slice.length ? slice[slice.length - 1] : null;
"""


RELOCATE_LAST_INCOMING_IMAGE_JS = """
const lookback = arguments[0];
const panel = document.querySelector('#main [data-testid="conversation-panel-messages"]')
    || document.querySelector('#main');
if (!panel) return null;

function isOutgoing(container) {
    let node = container;
    for (let depth = 0; depth < 8 && node; depth++) {
        if (node.classList && node.classList.contains('message-out')) return true;
        if (node.classList && node.classList.contains('message-in')) return false;
        node = node.parentElement;
    }
    for (const c of container.querySelectorAll('[data-pre-plain-text]')) {
        const pre = c.getAttribute('data-pre-plain-text') || '';
        if (/\\]\\s*You:\\s*$/i.test(pre)) return true;
    }
    return false;
}

function hasImage(container) {
    if (container.querySelector('[data-testid="media-caption"]')) return true;
    return !!container.querySelector(
        '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
        + '[data-icon="media"], img[src*="blob"], img[src*="mmg"], img[src*="cdn.whatsapp"]'
    );
}

const images = [];
const seen = new Set();
for (const node of panel.querySelectorAll('[data-testid="msg-container"], div.message-in')) {
    const container = node.matches('[data-testid="msg-container"]')
        ? node
        : (node.closest('[data-testid="msg-container"]') || node);
    if (!container || seen.has(container)) continue;
    if (isOutgoing(container)) continue;
    if (!hasImage(container)) continue;
    seen.add(container);
    images.push(container);
}
const slice = images.slice(-lookback);
return slice.length ? slice[slice.length - 1] : null;
"""


def relocate_last_incoming_voice(driver, lookback=INCOMING_LOOKBACK):
    """Return the newest incoming voice/PTT bubble element."""
    try:
        return driver.execute_script(RELOCATE_LAST_INCOMING_VOICE_JS, lookback)
    except Exception as exc:
        print(f"⚠️ Last incoming voice relocate failed: {exc}")
        return None


def relocate_message_container(driver, data_id):
    """Find a message bubble by WhatsApp data-id attribute."""
    if not data_id:
        return None
    try:
        element = driver.find_element(By.CSS_SELECTOR, f'div[data-id="{data_id}"]')
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element,
        )
        time.sleep(1)
        return element
    except Exception:
        pass
    try:
        element = driver.find_element(
            By.CSS_SELECTOR,
            f'div[data-testid="msg-container"][data-id="{data_id}"]',
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element,
        )
        time.sleep(1)
        return element
    except Exception:
        return None


def relocate_incoming_by_index(driver, incoming_index):
    """Relocate an incoming bubble by stable index in the filtered incoming list."""
    if incoming_index is None or incoming_index < 0:
        return None
    try:
        element = driver.execute_script(RELOCATE_INCOMING_BY_INDEX_JS, incoming_index)
        if element is not None:
            return element
    except Exception as exc:
        print(f"⚠️ Incoming index relocate failed ({incoming_index}): {exc}")
    return None


def relocate_last_incoming_image(driver, lookback=INCOMING_LOOKBACK):
    """Return the newest incoming image bubble element."""
    try:
        return driver.execute_script(RELOCATE_LAST_INCOMING_IMAGE_JS, lookback)
    except Exception as exc:
        print(f"⚠️ Last incoming image relocate failed: {exc}")
        return None


def normalize_unit_text(text):
    return clean_bubble_text(str(text or "").strip())


def scrape_incoming_units_js(driver, lookback=6):
    """Primary incoming-message scanner — handles image-only bubbles without text nodes."""
    try:
        driver.execute_script(
            "const panel = document.querySelector('#main [data-testid=\"conversation-panel-messages\"]');"
            "if (panel) { panel.scrollTop = panel.scrollHeight; }"
        )
        time.sleep(2)
        raw = driver.execute_script(SCRAPE_INCOMING_JS, lookback)
    except Exception as exc:
        print(f"⚠️ JS incoming scrape failed: {exc}")
        return []

    if not isinstance(raw, dict) or not raw.get("ok"):
        reason = raw.get("reason") if isinstance(raw, dict) else "unknown"
        print(f"⚠️ JS incoming scrape returned not-ok: {reason}")
        return []

    print(f"✅ JS incoming scrape: {raw.get('total', 0)} total, using last {len(raw.get('items') or [])}")

    units = []
    for item in raw.get("items") or []:
        data_id = str(item.get("dataId") or "").strip()
        incoming_index = item.get("incomingIndex")
        container = item.get("element")

        if container is not None:
            try:
                container.is_enabled()
            except Exception:
                container = None

        if container is None and data_id:
            container = relocate_message_container(driver, data_id)

        if container is None and incoming_index is not None:
            container = relocate_incoming_by_index(driver, incoming_index)

        text = normalize_unit_text(item.get("text"))
        if is_bot_noise_message(text):
            continue

        if container is not None:
            py_kind, py_text = classify_incoming_unit(container)
            if py_kind != "empty":
                kind = py_kind
                if py_text:
                    text = normalize_unit_text(py_text)
        elif item.get("hasDocument"):
            kind = "document"
        elif item.get("hasVoice"):
            kind = "voice"
        elif item.get("hasImage"):
            kind = "image"
        elif text:
            kind = "text"
        else:
            kind = "empty"

        if kind == "empty":
            continue

        if container is not None and not data_id:
            data_id = _data_id_from_container(container)

        units.append({
            "container": container,
            "data_id": data_id,
            "incoming_index": incoming_index,
            "kind": kind,
            "text": text,
        })
        print(
            f"   📩 JS unit: kind={kind} idx={incoming_index} data_id={data_id[:24]!r} "
            f"text={text[:80]!r} has_container={container is not None}"
        )

    return units


def find_last_incoming_image_data_id(driver, lookback=8):
    """Last-resort: locate the newest incoming image bubble by data-id only."""
    try:
        return driver.execute_script(
            """
            const lookback = arguments[0];
            const panel = document.querySelector('#main [data-testid="conversation-panel-messages"]')
                || document.querySelector('#main');
            if (!panel) return '';

            function isOutgoing(container) {
                if (container.classList.contains('message-out')) return true;
                if (container.classList.contains('message-in')) return false;
                for (const c of container.querySelectorAll('[data-pre-plain-text]')) {
                    const pre = c.getAttribute('data-pre-plain-text') || '';
                    if (/\\]\\s*You:\\s*$/i.test(pre)) return true;
                }
                return false;
            }

            function hasImage(container) {
                if (container.querySelector('[data-testid="media-caption"]')) {
                    return true;
                }
                return !!container.querySelector(
                    '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
                    + '[data-icon="media"], [data-icon="image"], '
                    + 'img[src*="blob"], img[src*="mmg"], img[src*="cdn.whatsapp"]'
                );
            }

            const containers = [];
            for (const node of panel.querySelectorAll('[data-testid="msg-container"], div.message-in')) {
                const container = node.matches('[data-testid="msg-container"]')
                    ? node
                    : (node.closest('[data-testid="msg-container"]') || node);
                if (!container || containers.includes(container)) continue;
                if (isOutgoing(container)) continue;
                if (hasImage(container)) containers.push(container);
            }

            const slice = containers.slice(-lookback);
            const last = slice[slice.length - 1];
            return last ? (last.getAttribute('data-id') || '') : '';
            """,
            lookback,
        )
    except Exception as exc:
        print(f"⚠️ Last incoming image scan failed: {exc}")
        return ""


def _data_id_from_container(container) -> str:
    if container is None:
        return ""
    try:
        data_id = str(container.get_attribute("data-id") or "").strip()
        if data_id:
            return data_id
        for child in container.find_elements(By.CSS_SELECTOR, "[data-id]"):
            data_id = str(child.get_attribute("data-id") or "").strip()
            if data_id:
                return data_id
    except Exception:
        pass
    return ""


def _caption_from_container(container) -> str:
    return normalize_unit_text(extract_text_from_message_container(container))


def container_matches_unit(unit, container) -> bool:
    """Verify a DOM bubble belongs to the scraped WhatsApp unit."""
    if container is None or unit is None:
        return False

    expected_id = str(unit.get("data_id") or "").strip()
    actual_id = _data_id_from_container(container)
    if expected_id and actual_id and expected_id != actual_id:
        return False

    expected_text = normalize_unit_text(unit.get("text") or "")
    if expected_text:
        actual_text = _caption_from_container(container)
        if actual_text and expected_text != actual_text:
            exp_key = expected_text.lower().replace(" ", "")
            act_key = actual_text.lower().replace(" ", "")
            if exp_key not in act_key and act_key not in exp_key:
                return False
    return True


def _voice_data_id_from_unit(unit, container) -> str:
    data_id = str(unit.get("data_id") or "").strip()
    if data_id:
        return data_id
    if container is None:
        return ""
    try:
        data_id = str(container.get_attribute("data-id") or "").strip()
        if data_id:
            return data_id
        for child in container.find_elements(By.CSS_SELECTOR, "[data-id]"):
            data_id = str(child.get_attribute("data-id") or "").strip()
            if data_id:
                return data_id
    except Exception:
        pass
    return ""


def resolve_unit_container(driver, unit):
    """Ensure we have a live WebElement for a scraped message unit."""
    expected_id = str(unit.get("data_id") or "").strip()

    container = unit.get("container")
    if container is not None:
        try:
            container.is_enabled()
            if container_matches_unit(unit, container):
                return container
            print(
                f"   ⚠️ Stale container for data_id={expected_id[:24]!r} "
                f"(actual={_data_id_from_container(container)[:24]!r}) — relocating"
            )
            container = None
        except Exception:
            container = None

    if expected_id:
        container = relocate_message_container(driver, expected_id)
        if container is not None and container_matches_unit(unit, container):
            unit["container"] = container
            return container
        if container is not None:
            print(
                f"   ❌ data-id {expected_id[:24]!r} resolved to mismatched bubble "
                f"(caption={_caption_from_container(container)[:60]!r})"
            )
            container = None

    incoming_index = unit.get("incoming_index")
    if incoming_index is not None:
        container = relocate_incoming_by_index(driver, incoming_index)
        if container is not None and container_matches_unit(unit, container):
            unit["container"] = container
            print(f"   📍 Relocated bubble via incoming index {incoming_index}")
            return container

    if unit.get("kind") == "image" and not expected_id:
        container = relocate_last_incoming_image(driver)
        if container is not None:
            unit["container"] = container
            print("   🖼️ Relocated image bubble via last-incoming-image fallback")
            return container

    if unit.get("kind") == "voice":
        data_id = str(unit.get("data_id") or "").strip()
        if data_id:
            container = relocate_message_container(driver, data_id)
            if container is not None:
                unit["container"] = container
                print(f"   🎤 Relocated voice bubble via data-id {data_id[:24]!r}")
                return container
        container = relocate_last_incoming_voice(driver)
        if container is not None:
            unit["container"] = container
            print("   🎤 Relocated voice bubble via last-incoming-voice fallback")
            return container

    return None


def get_incoming_message_containers(driver, retries=3):
    """Return incoming WhatsApp message containers oldest → newest."""
    if not wait_for_chat_messages(driver, timeout=15):
        print("⚠️ Chat message panel not ready when loading incoming containers.")

    for attempt in range(1, retries + 1):
        try:
            driver.execute_script(
                "const panel = document.querySelector('[data-testid=\"conversation-panel-messages\"]');"
                "if (panel) { panel.scrollTop = panel.scrollHeight; }"
            )
            time.sleep(2 if attempt == 1 else 3)
        except Exception:
            pass

        for selector in (
            'div.message-in[data-testid="msg-container"]',
            'div[data-testid="msg-container"].message-in',
            'div[data-testid="msg-container"]:not(.message-out)',
            'div.message-in',
        ):
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"✅ Found {len(elements)} incoming message container(s).")
                    return elements
            except Exception:
                continue

        try:
            count = driver.execute_script(
                "return document.querySelectorAll('div.message-in').length;"
            )
            if count:
                elements = driver.find_elements(By.CSS_SELECTOR, "div.message-in")
                if elements:
                    print(f"✅ JS fallback found {len(elements)} incoming message container(s).")
                    return elements
        except Exception:
            pass

        print(f"⚠️ Incoming container scrape attempt {attempt}/{retries} returned empty.")

    return []


def bubble_contains_document(bubble):
    if bubble is None:
        return False
    media = detect_bubble_media(bubble)
    if media.has_document or media.media_type in ("pdf", "office_word", "office_excel", "office_powerpoint", "document"):
        return True
    if (media.filename or "").lower().endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx")):
        return True
    try:
        return bool(re.search(r"\.pdf\b", bubble.text or "", re.I))
    except Exception:
        return False


def is_trivial_ack(text):
    compact = re.sub(r"[^a-zA-Z]", "", str(text or "")).upper()
    return compact in {
        "YA", "YAA", "YUP", "YES", "SURE", "OK", "OKAY", "K", "KK", "NOTED", "THANKS",
        "THX", "TY", "GOOD", "FINE", "ALRIGHT", "SAME", "YEP", "YEAH", "ROGER", "COPY",
        "GOTIT", "RECEIVED",
    }


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

    containers = get_incoming_message_containers(driver)
    if containers:
        # Newest document/attachment first (PDF POs often lack data-pre-plain-text).
        for container in reversed(containers):
            if bubble_contains_document(container):
                print("📄 Latest incoming bubble is a document attachment.")
                return container

        for container in reversed(containers):
            try:
                pre_plain = ""
                copyable = container.find_elements(By.CSS_SELECTOR, "div[data-pre-plain-text]")
                if copyable:
                    pre_plain = copyable[-1].get_attribute("data-pre-plain-text") or ""
                    if is_outgoing_pre_plain(pre_plain):
                        continue
                    text = extract_text_from_copyable_div(copyable[-1])
                else:
                    text = clean_bubble_text(container.text)

                if text and not is_whatsapp_system_promotion(text):
                    if is_trivial_ack(text):
                        for candidate in reversed(containers):
                            if bubble_contains_document(candidate):
                                print("📄 Trivial ack ignored — using nearby PDF/document bubble.")
                                return candidate
                            if container_has_image(candidate) or container_has_media_caption(candidate):
                                print("🖼️ Trivial ack ignored — using nearby image bubble.")
                                return candidate
                    if is_quote_without_part_number(text):
                        image_container = find_image_container(containers)
                        if image_container is not None:
                            print("🖼️ Quote caption without part no — using image bubble.")
                            return image_container
                    if bubble_contains_image(container):
                        print("🖼️ Latest incoming bubble contains image (+ caption).")
                    return container
            except Exception:
                continue

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


def resolve_message_container(bubble):
    """Walk up DOM to the full incoming msg-container (image + caption live here)."""
    if bubble is None:
        return None
    try:
        testid = bubble.get_attribute("data-testid") or ""
        classes = bubble.get_attribute("class") or ""
        if testid == "msg-container" or "message-in" in classes:
            return bubble
        node = bubble
        for _ in range(10):
            node = node.find_element(By.XPATH, "..")
            testid = node.get_attribute("data-testid") or ""
            classes = node.get_attribute("class") or ""
            if testid == "msg-container" or "message-in" in classes:
                return node
    except Exception:
        pass
    return bubble


def extract_text_from_message_container(container):
    if container is None:
        return ""
    try:
        caption_nodes = container.find_elements(
            By.CSS_SELECTOR,
            '[data-testid="media-caption"] span[data-testid="selectable-text"], '
            '[data-testid="media-caption"] span.selectable-text, '
            '[data-testid="media-caption"]',
        )
        for node in caption_nodes:
            text = extract_text_from_copyable_div(node)
            if text:
                return text
    except Exception:
        pass
    try:
        copyables = container.find_elements(By.CSS_SELECTOR, "div[data-pre-plain-text]")
        for div in reversed(copyables):
            pre_plain = div.get_attribute("data-pre-plain-text") or ""
            if is_outgoing_pre_plain(pre_plain):
                continue
            text = extract_text_from_copyable_div(div)
            if text:
                return text
    except Exception:
        pass
    return clean_bubble_text(container.text)


def is_quote_without_part_number(text):
    """Caption like 'Quote 2 pcs' with no part number — needs paired image."""
    text_u = str(text or "").upper().strip()
    if not text_u:
        return False
    if not re.search(r"\b(QUOTE|QUOTATION|RFQ|ENQ|PRICE|PLS QUOTE|KINDLY QUOTE|QUOTE ME)\b", text_u):
        return False
    if re.search(r"[A-Z]{1,4}\d{3,}[A-Z0-9#\-/]*", text_u):
        return False
    if re.search(r"\b(QTY|PCS|PC|PIECES|PIECE|EA|EACH|UNIT|UNITS)\b", text_u):
        return True
    return len(text_u) < 80


def is_support_without_part_number(text):
    """Caption like 'find equivalent' with no part number — needs paired image."""
    text_u = str(text or "").upper().strip()
    if not text_u:
        return False
    support_markers = (
        "EQUIVALENT", "REPLACEMENT", "SUBSTITUTE", "ALTERNATIVE",
        "SUCCESSOR", "REPLACE", "COMPATIBLE",
    )
    if not any(marker in text_u for marker in support_markers):
        return False
    if re.search(r"[A-Z]{1,4}[-]?\d{1,}[A-Z0-9#\-/]*", text_u):
        return False
    return True


def bubble_contains_image(bubble):
    container = resolve_message_container(bubble)
    return find_media_image_in_bubble(container) is not None


def container_has_image(container):
    """Detect image messages — requires a real image thumb, not just media-caption."""
    if container is None or container_has_voice(container):
        return False
    return container_has_real_image(container)


def find_image_container_js(driver, lookback=8):
    """JavaScript scan for recent incoming image bubbles (most reliable on WhatsApp Web)."""
    try:
        idx = driver.execute_script(
            """
            const nodes = document.querySelectorAll(
                'div.message-in[data-testid="msg-container"], div.message-in'
            );
            const start = Math.max(0, nodes.length - arguments[0]);
            for (let i = nodes.length - 1; i >= start; i--) {
                const el = nodes[i];
                if (el.querySelector(
                    '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
                    + 'img[src*="blob"], img[src*="mmg"], img[src*="cdn.whatsapp"]'
                )) {
                    return i;
                }
            }
            return -1;
            """,
            lookback,
        )
        containers = get_incoming_message_containers(driver)
        if isinstance(idx, int) and 0 <= idx < len(containers):
            print(f"🖼️ JS image scan matched incoming container index {idx}.")
            return containers[idx]
    except Exception as exc:
        print(f"⚠️ JS image scan failed: {exc}")
    return None


def container_has_real_image(container):
    if container is None:
        return False
    if bubble_contains_image(container):
        return True
    try:
        markers = container.find_elements(
            By.CSS_SELECTOR,
            '[data-testid="image-thumb"], [data-testid="media-url-provider"], '
            'img[src*="blob"], img[src*="mmg"], img[src*="cdn.whatsapp"]',
        )
        return bool(markers)
    except Exception:
        return False


def container_has_voice(container):
    if container is None:
        return False
    try:
        info = detect_bubble_media(container)
        if info.has_voice:
            return True
    except Exception:
        pass
    try:
        markers = container.find_elements(
            By.CSS_SELECTOR,
            '[data-testid="audio-play"], [data-testid="ptt-play-button"], [data-testid="ptt"], '
            '[data-testid="audio"], [data-icon="ptt"], [data-icon="audio-play"], '
            '[data-icon="audio-download"], audio',
        )
        if markers:
            return True
    except Exception:
        pass
    try:
        if container.find_elements(By.CSS_SELECTOR, '[data-testid="video-thumb"], video[src]'):
            return False
        if container_has_real_image(container):
            return False
        text = (container.text or "").strip()
        if len(text) > 100:
            return False
        if re.search(r"^\d{1,2}:[0-5]\d", text, re.M):
            if container.find_elements(
                By.CSS_SELECTOR,
                'canvas, [data-testid="ptt"], span[data-icon="audio-play"], span[data-icon="ptt"]',
            ):
                return True
    except Exception:
        pass
    return False


def container_has_media_caption(container):
    if container is None:
        return False
    try:
        return bool(container.find_elements(By.CSS_SELECTOR, '[data-testid="media-caption"]'))
    except Exception:
        return False


def classify_incoming_unit(container):
    text = extract_text_from_message_container(container)
    if is_bot_noise_message(text):
        return "empty", ""
    if container_has_voice(container):
        if is_bot_noise_message(text) or (len(text) > 120 and "OpenClaw" in text):
            return "empty", ""
        return "voice", text
    if bubble_contains_document(container):
        return "document", text
    if container_has_image(container):
        return "image", text
    if text.strip():
        return "text", text.strip()
    return "empty", ""


def collect_incoming_units(driver, lookback=INCOMING_LOOKBACK):
    units = scrape_incoming_units_js(driver, lookback=lookback)
    if units:
        return units

    print("⚠️ JS scrape empty — trying last incoming image fallback.")
    container = relocate_last_incoming_image(driver, lookback=lookback)
    if container is not None:
        print("✅ Image-only fallback matched last incoming image bubble")
        return [{
            "container": container,
            "data_id": "",
            "incoming_index": None,
            "kind": "image",
            "text": normalize_unit_text(extract_text_from_message_container(container)),
        }]

    print("⚠️ Image fallback empty — trying last incoming voice fallback.")
    container = relocate_last_incoming_voice(driver, lookback=lookback)
    if container is not None:
        print("✅ Voice-only fallback matched last incoming voice bubble")
        return [{
            "container": container,
            "data_id": "",
            "incoming_index": None,
            "kind": "voice",
            "text": normalize_unit_text(extract_text_from_message_container(container)),
        }]

    print("⚠️ Voice fallback empty — falling back to Selenium container scan.")
    containers = get_incoming_message_containers(driver)
    recent = containers[-lookback:] if len(containers) > lookback else list(containers)
    units = []
    for container in recent:
        kind, text = classify_incoming_unit(container)
        units.append({
            "container": container,
            "kind": kind,
            "text": text,
        })
    return units


def find_image_unit(units):
    for unit in reversed(units or []):
        if unit.get("kind") == "image":
            return unit
    return None


def plan_sequential_units(units, contact_name: str = "", contact_hint: str = ""):
    """
    When customer sends photo then 'Quote 2 pcs' as separate messages,
    process image bubble first, then text bubble — one at a time.
    Only returns NEW unprocessed units (never the whole lookback window).
    """
    units = filter_processable_units(units, contact_name, contact_hint)
    if not units:
        return []

    meaningful = [
        u for u in units
        if u["kind"] in ("image", "document", "text", "voice")
        and not is_monitor_noise_unit(u)
    ]
    if not meaningful:
        return []

    working = list(meaningful)
    while working and working[-1]["kind"] == "text" and is_trivial_ack(working[-1].get("text")):
        dropped = working.pop()
        print(f"📨 Ignoring trailing ack message: {dropped.get('text')!r}")

    if not working:
        return meaningful[-1:]

    newest = working[-1]
    prev = working[-2] if len(working) >= 2 else None

    if newest["kind"] == "voice":
        print("📨 Sequential plan: voice message")
        return [newest]

    if newest["kind"] == "image":
        print("📨 Sequential plan: image message (caption may be attached)")
        return [newest]

    if newest["kind"] == "text" and prev and prev["kind"] == "image":
        print("📨 Sequential plan: 1) image message  2) text caption")
        return [prev, newest]

    if newest["kind"] == "image" and prev and prev["kind"] == "text":
        print("📨 Sequential plan: 1) image message  2) earlier text caption")
        return [newest, prev]

    if newest["kind"] == "document" and prev and prev["kind"] == "text":
        print("📨 Sequential plan: 1) document  2) text caption")
        return [newest, prev]

    if newest["kind"] == "text" and (
        is_quote_without_part_number(newest.get("text"))
        or is_support_without_part_number(newest.get("text"))
    ):
        image_unit = find_image_unit(working)
        if image_unit and image_unit is not newest:
            print("📨 Sequential plan: image + caption without part number (within lookback window)")
            return [image_unit, newest]

    if newest["kind"] == "document":
        print("📨 Sequential plan: single document message")
        return [newest]

    if newest["kind"] == "text" and not prev:
        print("📨 Sequential plan: single text message")
        return [newest]

    print(f"📨 Sequential plan: single {newest['kind']} message")
    return [newest]


def process_units_sequentially(driver, contact_name, plan, customer_contact):
    """
    Read each WhatsApp bubble one-by-one. Returns merged inquiry payload
    for a single customer reply at the end.
    """
    close_contact_info_drawer(driver)
    time.sleep(0.5)

    copilot_items = []
    document_items = []
    latest_message = ""
    image_path = None
    image_analysis = None
    media_info = None
    enrichment = {}
    processed_voice = False
    voice_ok = True

    for step, unit in enumerate(plan, start=1):
        container = resolve_unit_container(driver, unit)
        kind = unit["kind"]
        unit_text = unit.get("text") or ""

        print("")
        print("-" * 90)
        print(f"📨 Step {step}/{len(plan)}: {kind.upper()} message")
        print(f"   Text preview: {unit_text[:160] if unit_text else '(none)'}")

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                container,
            )
            time.sleep(1)
        except Exception:
            pass

        if kind == "image":
            if container is not None and container_has_voice(container):
                kind = "voice"
                unit["kind"] = "voice"
                print("   🎤 Reclassified image → voice (PTT bubble detected)")
        if kind == "image":
            if container is None:
                print("   ❌ Image step: container element missing — cannot capture photo.")
                continue
            if not container_matches_unit(unit, container):
                print(
                    f"   ❌ Image step: bubble mismatch for data_id="
                    f"{str(unit.get('data_id') or '')[:24]!r} — skipping to avoid wrong photo."
                )
                continue
            caption = normalize_unit_text(unit_text or latest_message)
            if caption:
                latest_message = caption
            media_info = detect_bubble_media(container, caption_text=caption)
            message_data_id = str(unit.get("data_id") or "").strip()
            image_path = capture_bubble_image(
                driver, container, contact_name, message_data_id=message_data_id
            )
            items = _resolve_visual_items(caption, image_path=image_path, copilot_items=None)
            if items:
                copilot_items = items
                print(f"   👁️ Image step: Copilot extracted {len(items)} item(s)")
            elif image_path:
                print("   ⚠️ Image step: photo captured but Copilot found no parts yet.")

        elif kind == "voice":
            processed_voice = True
            if container is None:
                print("   ❌ Voice step: container missing before relocation.")
            close_contact_info_drawer(driver)
            container = resolve_unit_container(driver, unit)
            if container is None:
                print("   ❌ Voice step: could not locate voice bubble — download skipped.")
            else:
                data_id = _voice_data_id_from_unit(unit, container)
                print(f"   🎤 Voice step: container ready data_id={data_id[:48]!r}")
            media_info = detect_bubble_media(container, caption_text=unit_text or latest_message)
            if container is not None:
                enrichment = enrich_message_from_attachments(
                    driver,
                    container,
                    contact_name,
                    unit_text or latest_message,
                    media_info,
                    message_data_id=_voice_data_id_from_unit(unit, container),
                )
            transcript = enrichment.get("transcript") or ""
            caption = normalize_unit_text(unit_text or "")
            if not transcript and (enrichment.get("voice_path") or os.path.exists(VOICE_LATEST_OPUS)):
                print("   🎤 Voice step: retrying transcription from saved .opus...")
                voice_data_id = _voice_data_id_from_unit(unit, container)
                transcript = ensure_voice_transcript(
                    enrichment.get("voice_path") or VOICE_LATEST_OPUS,
                    message_data_id=voice_data_id,
                )
                if transcript:
                    enrichment["transcript"] = transcript
            if transcript:
                latest_message = transcript
                if caption and caption.lower() not in transcript.lower():
                    latest_message = f"{transcript}\n(caption: {caption})"
                print(f"   🎤 Voice step: transcript={transcript[:160]!r}")
            elif enrichment.get("voice_path"):
                print("   ⚠️ Voice step: audio saved but transcription empty.")
                latest_message = caption or latest_message
            elif caption:
                latest_message = caption
            voice_ok = bool(
                enrichment.get("transcript")
                or (
                    enrichment.get("voice_path")
                    and os.path.exists(VOICE_LATEST_OPUS)
                )
            )
            if not voice_ok:
                print("   ⚠️ Voice step: download/transcribe failed — will retry next cycle.")
            inquiry_for_extract = transcript or latest_message
            if not copilot_items and inquiry_for_extract:
                items = extract_rfq_with_copilot(inquiry_for_extract, image_path=None)
                if items:
                    copilot_items = items
                    print(f"   🎤 Voice step: Copilot extracted {len(items)} item(s) from transcript")
                else:
                    print("   🎤 Voice step: running inquiry engine on transcript text...")

        elif kind == "document":
            media_info = detect_bubble_media(container, caption_text=unit_text or latest_message)
            enrichment = enrich_message_from_attachments(
                driver, container, contact_name, unit_text or latest_message, media_info
            )
            if enrichment.get("document_items"):
                document_items = enrichment["document_items"]
            if enrichment.get("text"):
                latest_message = enrichment["text"]
            elif unit_text:
                latest_message = unit_text

        elif kind == "text":
            if is_trivial_ack(unit_text) and (copilot_items or image_path or document_items):
                print(f"   📝 Text step: skipping ack {unit_text!r} — keeping photo/document extraction")
                continue
            latest_message = unit_text
            media_info = detect_bubble_media(container, caption_text=unit_text)
            if not copilot_items and not document_items and not (
                is_quote_without_part_number(unit_text) or is_support_without_part_number(unit_text)
            ):
                items = extract_rfq_with_copilot(unit_text, image_path=None)
                if items:
                    copilot_items = items
                    print(f"   📝 Text step: Copilot extracted {len(items)} item(s)")
            elif (
                is_quote_without_part_number(unit_text) or is_support_without_part_number(unit_text)
            ) and not image_path:
                print(
                    "   📝 Text step: caption without part number — "
                    "waiting for paired image step (no text-only guess)."
                )
            else:
                print("   📝 Text step: caption/qty merged with prior image/document extraction.")

        else:
            print(f"   ⚠️ Skipping empty/unknown message unit at step {step}.")

    if image_path and not processed_voice:
        image_analysis = {
            "items": copilot_items,
            "inquiry_text": latest_message,
            "notes": "Sequential image then caption processing.",
            "source": "copilot_visual",
            "image_path": image_path,
        }
        if media_info is None:
            media_info = detect_bubble_media(plan[-1]["container"], caption_text=latest_message)
        media_info.media_type = "image"
        media_info.has_image = True

    if media_info is None:
        media_info = detect_bubble_media(
            plan[-1]["container"] if plan else None,
            caption_text=latest_message,
        )

    if processed_voice or enrichment.get("voice_path") or enrichment.get("transcript"):
        media_info.media_type = "voice"
        media_info.has_voice = True
        media_info.has_image = False
        if processed_voice and not image_path:
            image_analysis = None
    elif enrichment.get("transcript") and media_info is not None:
        media_info.media_type = "voice"
        media_info.has_voice = True

    return {
        "latest_message": latest_message,
        "copilot_items": copilot_items,
        "document_items": document_items,
        "image_path": image_path,
        "image_analysis": image_analysis,
        "media_info": media_info,
        "enrichment": enrichment,
        "voice_ok": voice_ok if processed_voice else True,
    }


def gather_recent_inquiry_context(driver, lookback=8):
    """
    Collect caption text + image from the last few incoming messages.
    Handles separate bubbles: photo first, then 'Quote 2 pcs' text below.
    """
    try:
        driver.execute_script(
            "const panel = document.querySelector('[data-testid=\"conversation-panel-messages\"]');"
            "if (panel) { panel.scrollTop = panel.scrollHeight; }"
        )
        time.sleep(2)
    except Exception:
        pass

    containers = get_incoming_message_containers(driver)
    recent = containers[-lookback:] if len(containers) > lookback else list(containers)

    caption_parts = []
    image_container = None

    for container in reversed(recent):
        text = extract_text_from_message_container(container)
        if text and not is_whatsapp_system_promotion(text):
            if text not in caption_parts:
                caption_parts.append(text)
        if image_container is None and container_has_image(container):
            image_container = container

    if image_container is None:
        image_container = find_image_container_js(driver, lookback=lookback)
    if image_container is None:
        image_container = find_image_container(recent)

    latest_message = caption_parts[0] if caption_parts else ""
    primary_container = recent[-1] if recent else None

    if image_container is not None:
        print(f"🖼️ Inquiry context: image={'yes' if image_container else 'no'}, caption={latest_message!r}")
    elif is_quote_without_part_number(latest_message):
        print(f"⚠️ Quote caption without detected image: {latest_message!r}")

    return {
        "latest_message": latest_message,
        "image_container": image_container,
        "primary_container": primary_container or image_container,
        "all_recent": recent,
    }


def find_image_container(containers):
    for container in reversed(containers or []):
        if container_has_image(container):
            return container
    return None


def find_media_image_in_bubble(bubble):
    bubble = resolve_message_container(bubble)
    if bubble is None:
        return None

    selectors = [
        'img[src]',
        'img[src*="blob:"]',
        'video[src]',
        '[data-testid="image-thumb"] img',
        '[data-testid="image-thumb"]',
        '[data-testid="media-url-provider"] img',
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
                if is_profile_or_ui_image_src(src):
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

    if best is None:
        try:
            thumbs = bubble.find_elements(By.CSS_SELECTOR, '[data-testid="image-thumb"]')
            for thumb in thumbs:
                if thumb.is_displayed():
                    return thumb
        except Exception:
            pass

    return best


def bubble_has_media_image(bubble):
    return find_media_image_in_bubble(bubble) is not None


def wait_for_media_image_ready(driver, media, timeout=8):
    try:
        return bool(
            driver.execute_async_script(
                """
                const img = arguments[0];
                const timeoutMs = arguments[1];
                const done = arguments[arguments.length - 1];
                const started = Date.now();
                function ready() {
                    return img && img.complete && (img.naturalWidth || 0) > 80;
                }
                if (ready()) return done(true);
                const timer = setInterval(() => {
                    if (ready()) {
                        clearInterval(timer);
                        done(true);
                    } else if (Date.now() - started > timeoutMs) {
                        clearInterval(timer);
                        done(false);
                    }
                }, 200);
                """,
                media,
                int(timeout * 1000),
            )
        )
    except Exception:
        time.sleep(1)
        return True


def close_media_viewer(driver):
    for selector in (
        '[data-testid="btn-closer-drawer"]',
        '[data-testid="x"]',
        '[aria-label="Close"]',
        'span[data-icon="x"]',
    ):
        try:
            for button in driver.find_elements(By.CSS_SELECTOR, selector):
                if button.is_displayed():
                    button.click()
                    time.sleep(0.5)
                    return
        except Exception:
            continue
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except Exception:
        pass


def capture_image_via_media_viewer(driver, bubble, image_path):
    """Open WhatsApp's full-screen viewer so we capture the correct photo."""
    thumb = find_media_image_in_bubble(bubble)
    if thumb is None:
        return None
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            thumb,
        )
        time.sleep(0.8)
        driver.execute_script("arguments[0].click();", thumb)
        time.sleep(2)

        viewer_selectors = [
            '[data-testid="media-viewer-panel"] img[src]',
            'div[data-animate-media-viewer="true"] img[src]',
            'img[src*="blob:"]',
            'img[src*="mmg"]',
        ]
        best = None
        best_area = 0
        for selector in viewer_selectors:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not element.is_displayed():
                        continue
                    src = (element.get_attribute("src") or "").lower()
                    if is_profile_or_ui_image_src(src):
                        continue
                    size = element.size or {}
                    area = int(size.get("width") or 0) * int(size.get("height") or 0)
                    if area > best_area:
                        best = element
                        best_area = area
                except Exception:
                    continue

        if best is not None and wait_for_media_image_ready(driver, best):
            best.screenshot(image_path)
            print(f"🖼️ Saved WhatsApp image from media viewer: {image_path}")
            return image_path
    except Exception as exc:
        print(f"⚠️ Media viewer capture failed: {exc}")
    finally:
        close_media_viewer(driver)
    return None


def capture_bubble_image(driver, bubble, contact_name, message_data_id=""):
    bubble = resolve_message_container(bubble)
    if bubble is None:
        return None

    os.makedirs(IMAGE_CAPTURE_DIR, exist_ok=True)
    safe_contact = re.sub(r"[^A-Za-z0-9._-]+", "_", str(contact_name or "contact"))[:60]
    id_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(message_data_id or ""))[:24]
    suffix = f"_{id_slug}" if id_slug else ""
    image_path = os.path.join(
        IMAGE_CAPTURE_DIR,
        f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_contact}{suffix}.png",
    )

    viewer_path = capture_image_via_media_viewer(driver, bubble, image_path)
    if viewer_path:
        return viewer_path

    media = find_media_image_in_bubble(bubble)
    if media is not None:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                media,
            )
            wait_for_media_image_ready(driver, media)
            media.screenshot(image_path)
            print(f"🖼️ Saved incoming WhatsApp image (thumb): {image_path}")
            return image_path
        except Exception as e:
            print(f"❌ Failed to capture WhatsApp image thumb: {e}")

    if container_has_image(bubble):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                bubble,
            )
            time.sleep(1)
            bubble.screenshot(image_path)
            print(f"🖼️ Saved container screenshot (image thumb fallback): {image_path}")
            return image_path
        except Exception as e:
            print(f"❌ Container screenshot fallback failed: {e}")

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


def open_whatsapp_chat_by_phone(driver, phone, chat_label="customer"):
    phone = normalize_phone(phone)

    if not phone:
        print(f"❌ {chat_label.title()} phone missing. Cannot open WhatsApp chat.")
        return False

    print(f"🌐 Opening {chat_label} WhatsApp chat: {phone}")

    driver.get(f"https://web.whatsapp.com/send?phone={phone}")

    end = time.time() + 90

    while time.time() < end:
        try:
            box = driver.find_elements(By.XPATH, '//footer//div[@contenteditable="true"]')
            if box:
                print(f"✅ {chat_label.title()} WhatsApp chat opened.")
                return True
        except Exception:
            pass

        time.sleep(3)

    print(f"❌ {chat_label.title()} WhatsApp chat did not open.")
    return False


def customer_replies_go_to_monitor():
    return get_customer_reply_mode() in ("monitor", "debug", "test")


def build_monitor_reply(context, customer_name, customer_contact, original_message, reply_message,
                        classification_summary=None):
    lines = [
        "[OpenClaw Monitor Mode]",
        f"Context: {context or 'Customer reply'}",
        f"Customer: {customer_name or '-'}",
    ]

    phone_display = format_customer_phone_display(customer_contact)
    if phone_display:
        lines.append(f"Customer Contact: {phone_display}")

    if classification_summary:
        lines.append("")
        lines.append("Classification:")
        lines.append(classification_summary)

    lines.extend([
        "",
        "Original Message:",
        original_message or "(empty)",
        "",
        "Generated Reply:",
        reply_message or "(empty)",
    ])

    return "\n".join(lines)


def send_customer_reply(driver, reply_message, customer_name=None, customer_contact=None,
                        original_message=None, context="CUSTOMER_REPLY",
                        customer_chat_is_open=True, classification_summary=None):
    if customer_replies_go_to_monitor():
        print(
            "🧪 Monitor mode active. Redirecting alert to your monitor number "
            f"{MONITOR_WHATSAPP_PHONE} (OPENCLAW_MONITOR_WHATSAPP_PHONE)."
        )

        if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE, chat_label="monitor"):
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
        if not open_whatsapp_chat_by_phone(driver, customer_contact, chat_label="customer"):
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

    if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE, chat_label="monitor"):
        print("❌ Could not open monitor chat for classification alert.")
        return False

    return send_reply_in_current_chat(driver, alert)


def process_monitor_feedback(driver):
    """Check monitor chat for teaching commands like: correct: purchase_order"""
    if not open_whatsapp_chat_by_phone(driver, MONITOR_WHATSAPP_PHONE, chat_label="monitor"):
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
    classification=None, document_items=None, pre_extracted_copilot_items=None,
    voice_enrichment=None, image_path=None,
):
    customer_contact = customer_contact or contact_name
    classification_summary = classification.summary() if classification else None
    document_items = document_items or []
    image_path = image_path or (image_analysis.get("image_path") if image_analysis else None)

    if pre_extracted_copilot_items:
        copilot_items = pre_extracted_copilot_items
        print(f"🤖 Using {len(copilot_items)} item(s) from sequential visual extraction.")
    elif image_path:
        print(f"🤖 Image-first Copilot extraction: {os.path.basename(image_path)}")
        copilot_items = _resolve_visual_items(latest_message, image_path=image_path)
        print(f"🤖 Visual extraction result: {len(copilot_items)} item(s)")
    elif not is_quote_without_part_number(latest_message):
        copilot_items = extract_rfq_with_copilot(latest_message, image_path=None)
    else:
        print("⚠️ Quote caption without part number and no image — skipping text-only guess.")
        copilot_items = []

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
        if image_path is None and is_quote_without_part_number(latest_message):
            print("⚠️ RFQ caption with no parts and no image captured — cannot extract.")
            reply = (
                "Hi, I received your quote request but could not access the photo.\n\n"
                "Please resend the product label photo together with the part numbers, or type:\n"
                "P36203010#1 Qty:1"
            )
        elif image_analysis:
            reply = (
                "Hi, I analyzed your photo but could not match the parts in our system.\n\n"
                "Please send a clearer label/nameplate photo, or type:\n"
                "E3Z-T61 Qty:1\n"
                "178902 Qty:2"
            )
        elif voice_enrichment and (
            voice_enrichment.get("transcript")
            or voice_enrichment.get("voice_path")
            or (classification and getattr(classification, "media_type", "") == "voice")
        ):
            if voice_enrichment.get("transcript"):
                reply = (
                    "Hi, we transcribed your voice message but could not match the parts in our system.\n\n"
                    "Please resend with clearer part numbers, or type:\n"
                    "E3Z-T61 Qty:1\n"
                    "178902 Qty:2"
                )
            else:
                reply = (
                    "Hi, we received your voice message but could not transcribe it.\n\n"
                    "Please resend the voice note, or type the part numbers:\n"
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

    customer_reply = build_plain_quotation_reply(
        formatted_rows,
        ai_research=build_ai_research_summary(formatted_rows),
    )
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
    image_path=None,
    copilot_items=None,
):
    """Handle non-RFQ intents with appropriate acknowledgement while learning in background."""
    customer_contact = customer_contact or contact_name
    handler = classification.handler
    image_path = image_path or (image_analysis.get("image_path") if image_analysis else None)
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

    if handler == "purchase_order" and (image_analysis or document_items or classification.media_type == "pdf"):
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

    if handler == "technical_support":
        copilot_reply = build_technical_support_reply(
            latest_message,
            image_path=image_path,
            copilot_items=None,
        )
        if copilot_reply:
            reply = copilot_reply
            context = "TECHNICAL_SUPPORT_COPILOT"
            print("🧠 Copilot technical support reply generated.")
        else:
            print("⚠️ Copilot technical support unavailable — using default acknowledgement.")

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
    raw_contact_name = get_contact_name_from_open_chat(driver)
    if not acquire_chat_processing_lock(raw_contact_name or "open-chat"):
        return

    try:
        _process_open_chat_body(driver, raw_contact_name)
    finally:
        release_chat_processing_lock()


def _process_open_chat_body(driver, raw_contact_name):
    plan = []
    contact_name = raw_contact_name or "WhatsApp Customer"
    voice_ok = True
    try:
        if not wait_for_open_chat_panel(driver, timeout=25):
            print("❌ Cannot process chat — conversation panel is not open (still on Business Web landing?).")
            return
        wait_for_chat_messages(driver, timeout=15)
        units = collect_incoming_units(driver, lookback=INCOMING_LOOKBACK)
        if not units:
            print(
                "ℹ️ No incoming customer messages in this chat lookback window. "
                "OpenClaw ignores outgoing bubbles (messages sent FROM this WhatsApp account)."
            )
        store_key = contact_store_key(raw_contact_name)
        plan = plan_sequential_units(units, store_key, raw_contact_name)
        bubble = plan[-1]["container"] if plan else None

        if not plan:
            print("ℹ️ No new messages to process — already handled or bot/monitor echoes only.")
            return

        print(f"📨 Processing {len(plan)} new message unit(s) from WhatsApp (not history replay)")
        payload = process_units_sequentially(driver, contact_name, plan, "")
        voice_ok = payload.get("voice_ok", True)
        customer_phone = get_contact_phone_from_open_chat(driver, bubble=bubble)
        contact_name, customer_contact = resolve_customer_identity(raw_contact_name, customer_phone)
        if is_monitor_phone(customer_contact):
            print(f"ℹ️ Skipping monitor chat {contact_name!r} — not a customer inquiry.")
            return
        if contact_name != raw_contact_name or customer_contact:
            detail = f"{raw_contact_name!r} → {contact_name!r}"
            if customer_contact:
                detail += f" | phone from WhatsApp: {customer_contact}"
            else:
                detail += " | phone not exposed by WhatsApp"
            print(f"👤 Customer identity: {detail}")
        latest_message = payload["latest_message"]
        image_analysis = payload["image_analysis"]
        image_path = payload["image_path"]
        media_info = payload["media_info"]
        document_items = payload["document_items"]
        copilot_items = payload["copilot_items"]
        enrichment = payload.get("enrichment") or {}

        if media_info.media_type == "voice" and not voice_ok and not enrichment.get("transcript"):
            print(
                "⚠️ Voice download/transcribe failed — skipping customer reply; "
                "message stays unread for retry."
            )
            return

        print("")
        print("=" * 90)
        print("📲 WHATSAPP CHAT PROCESSED (SEQUENTIAL)")
        print(f"   Contact: {contact_name}")
        print(f"   Customer Phone: {customer_contact or '(not exposed by WhatsApp DOM)'}")
        print(f"   Steps processed: {len(plan)}")
        print(f"   Media Type: {media_info.media_type}")
        print(f"   Combined caption: {latest_message}")
        if image_path:
            print(f"   Image: {image_path}")
        if copilot_items:
            print(f"   Copilot items: {len(copilot_items)}")
        if enrichment.get("voice_path"):
            print(f"   Voice: {enrichment['voice_path']}")
        if enrichment.get("transcript"):
            print(f"   Transcript: {enrichment['transcript'][:120]}")
        print("=" * 90)

        if (
            not latest_message
            and not image_path
            and not copilot_items
            and not document_items
            and not enrichment.get("transcript")
            and media_info.media_type in ("text", "unknown")
        ):
            print("⚠️ Sequential processing found no usable content.")
            classification = classify_whatsapp_message("", media_info=media_info)
            log_classification(contact_name, customer_contact, "", classification, status="EMPTY_MESSAGE")
            fallback_reply = (
                "Hi, I received your WhatsApp message but could not read the content.\n\n"
                "Please resend the product photo and part numbers, or type:\n"
                "P36203010#1 Qty:1"
            )
            send_customer_reply(
                driver, fallback_reply,
                customer_name=contact_name, customer_contact=customer_contact,
                original_message="", context="NO_INCOMING_MESSAGE_FALLBACK",
                customer_chat_is_open=True,
                classification_summary=classification.summary(),
            )
            append_log(contact_name, "", [], [], "NO_INCOMING_MESSAGE_FALLBACK")
            return

        inquiry_text = latest_message
        if enrichment.get("transcript"):
            inquiry_text = enrichment["transcript"]
            caption = re.sub(r"^\[Voice transcript\]\n", "", latest_message or "").strip()
            if caption and caption not in inquiry_text:
                inquiry_text = f"{inquiry_text}\n(caption: {caption})"

        print("=" * 90)
        print("🧪 COMBINED MESSAGE FOR ENGINE:")
        print(repr(inquiry_text))
        print("=" * 90)

        classification = classify_whatsapp_message(inquiry_text or "(voice inquiry)", media_info=media_info)
        if is_equivalent_support_request(inquiry_text):
            classification.intent = "technical_support"
            classification.handler = "technical_support"
            classification.confidence = max(classification.confidence, 0.93)
            classification.reasoning = (
                "Equivalent/replacement request — technical support, not RFQ quotation."
            )
            print("🔧 Equivalent/replacement detected — forcing technical_support handler.")
        if media_info.media_type == "voice":
            classification.media_type = "voice"
            if classification.media_info is not None:
                classification.media_info.media_type = "voice"
                classification.media_info.has_voice = True
                classification.media_info.has_image = False
        if (
            classification.intent in ("unknown", "greeting", "general_chat")
            and (document_items or enrichment.get("document_text") or media_info.media_type == "pdf")
        ):
            classification.intent = "purchase_order"
            classification.confidence = max(classification.confidence, 0.9)
            classification.handler = "purchase_order"
            classification.reasoning = "PDF/PO document content detected after attachment extraction."
            classification.suggested_reply = (
                "Hi, thank you for sending your purchase order.\n\n"
                "Our team is reviewing the document and will confirm shortly."
            )
        if copilot_items and classification.intent in ("unknown", "general_chat"):
            if not is_equivalent_support_request(inquiry_text):
                classification.intent = "rfq_inquiry"
                classification.handler = "rfq_inquiry"
                classification.confidence = max(classification.confidence, 0.85)
                if enrichment.get("transcript"):
                    classification.reasoning = "Parts extracted from voice transcript."
                elif image_path:
                    classification.reasoning = "Parts extracted from customer photo."
                else:
                    classification.reasoning = "Parts extracted from customer message."
        if enrichment.get("transcript") and classification.handler in (
            "monitor_only", "voice_note", "unknown", "general_chat"
        ):
            if not is_equivalent_support_request(inquiry_text):
                classification.intent = "rfq_inquiry"
                classification.handler = "rfq_inquiry"
                classification.confidence = max(classification.confidence, 0.85)
                classification.reasoning = "Voice note transcribed — processing as inquiry."
        if (
            (image_path or getattr(media_info, "has_image", False))
            and classification.intent in ("unknown", "general_chat", "greeting")
            and getattr(media_info, "media_type", "") != "voice"
            and not is_equivalent_support_request(inquiry_text)
        ):
            classification.intent = "rfq_inquiry"
            classification.handler = "rfq_inquiry"
            classification.confidence = max(classification.confidence, 0.85)
            classification.reasoning = "Customer photo inquiry detected."
        log_classification(contact_name, customer_contact, inquiry_text, classification)

        if re.search(r"WA-\d{8}-[A-Z0-9]+-[A-Z0-9]+", inquiry_text, re.I):
            send_classification_alert(driver, contact_name, customer_contact, inquiry_text, classification)
            process_supplier_reply(driver, contact_name, inquiry_text)
            return

        if classification.handler == "supplier_reply":
            send_classification_alert(driver, contact_name, customer_contact, inquiry_text, classification)
            process_supplier_reply(driver, contact_name, inquiry_text)
            return

        if classification.handler == "skip":
            send_classification_alert(driver, contact_name, customer_contact, inquiry_text, classification)
            process_classified_non_inquiry(
                driver, contact_name, inquiry_text, classification,
                customer_contact=customer_contact, image_analysis=image_analysis,
                document_items=document_items,
                image_path=image_path,
                copilot_items=copilot_items,
            )
            return

        if classification.handler == "rfq_inquiry" or (
            classification.handler == "purchase_order"
            and (document_items or image_analysis or media_info.media_type == "pdf")
        ) or (
            enrichment.get("transcript")
            and inquiry_text
            and not is_equivalent_support_request(inquiry_text)
        ):
            process_customer_inquiry(
                driver,
                contact_name,
                inquiry_text,
                image_analysis=image_analysis,
                customer_contact=customer_contact,
                classification=classification,
                document_items=document_items,
                pre_extracted_copilot_items=copilot_items or None,
                voice_enrichment=enrichment,
                image_path=image_path,
            )
            return

        process_classified_non_inquiry(
            driver,
            contact_name,
            inquiry_text,
            classification,
            customer_contact=customer_contact,
            image_analysis=image_analysis,
            document_items=document_items,
            image_path=image_path,
            copilot_items=copilot_items,
        )
    finally:
        finalize_chat_processing(
            contact_name,
            plan,
            voice_ok=voice_ok,
            contact_hint=raw_contact_name,
        )


def _process_open_chat_legacy_removed(driver):
    """Placeholder to anchor diff — legacy combined-context path removed."""
    pass


def process_open_chat_OLD(driver):
    contact_name = get_contact_name_from_open_chat(driver)
    ctx = gather_recent_inquiry_context(driver)
    bubble = ctx["primary_container"]
    image_container = ctx["image_container"]
    latest_message = ctx["latest_message"]
    customer_phone = get_contact_phone_from_open_chat(driver, bubble=bubble)
    customer_contact = customer_phone or contact_name
    image_analysis = None
    image_path = None
    media_info = detect_bubble_media(bubble, caption_text=latest_message)

    if image_container is not None:
        image_path = capture_bubble_image(driver, image_container, contact_name)
        media_info = detect_bubble_media(image_container, caption_text=latest_message)
        if image_path:
            print(f"🖼️ Captured inquiry photo for Copilot: {image_path}")

    for attempt in range(1, 4):
        if latest_message or image_path or media_info.media_type not in ("text", "unknown"):
            break

        print(f"⚠️ Message scrape attempt {attempt}/3 — refreshing inquiry context...")
        time.sleep(3)
        ctx = gather_recent_inquiry_context(driver)
        bubble = ctx["primary_container"]
        image_container = ctx["image_container"]
        latest_message = ctx["latest_message"] or latest_message
        media_info = detect_bubble_media(bubble, caption_text=latest_message)
        if image_container is not None and not image_path:
            image_path = capture_bubble_image(driver, image_container, contact_name)

        if latest_message or image_path or media_info.media_type not in ("text", "unknown"):
            break

        print(f"⚠️ Message scrape attempt {attempt}/3 returned empty. Retrying...")
        time.sleep(3)

    if image_path and not image_analysis:
        image_analysis = {
            "items": [],
            "inquiry_text": latest_message,
            "notes": "Photo + caption sent to Copilot visual extraction.",
            "source": "copilot_visual",
            "image_path": image_path,
        }
    elif image_path:
        image_analysis["image_path"] = image_path
        image_analysis["inquiry_text"] = latest_message

    if image_path:
        media_info.media_type = "image"
        media_info.has_image = True

    enrichment = enrich_message_from_attachments(
        driver, bubble, contact_name, latest_message, media_info
    )
    latest_message = enrichment.get("text") or latest_message
    document_items = enrichment.get("document_items") or []
    if enrichment.get("transcript"):
        media_info.caption = enrichment["transcript"]
    if enrichment.get("preview_image_path") and not image_path:
        image_path = enrichment["preview_image_path"]
        print(f"📄 Using PDF preview screenshot for visual extraction: {image_path}")
    if enrichment.get("document_path") and media_info.media_type in ("text", "unknown"):
        media_info.media_type = "pdf" if str(enrichment["document_path"]).lower().endswith(".pdf") else "document"
    if enrichment.get("document_text") and "PURCHASE ORDER" in enrichment["document_text"].upper():
        media_info.media_type = "pdf"
    if image_path and media_info.media_type in ("text", "unknown"):
        media_info.media_type = "image"
        media_info.has_image = True

    if image_path and not image_analysis:
        image_analysis = {
            "items": [],
            "inquiry_text": latest_message,
            "notes": "Document preview or image sent to Copilot visual extraction.",
            "source": "copilot_visual",
            "image_path": image_path,
        }
    elif image_path and image_analysis:
        image_analysis["image_path"] = image_path
        image_analysis["inquiry_text"] = latest_message

    print("")
    print("=" * 90)
    print("📲 UNREAD WHATSAPP CHAT OPENED")
    print(f"   Contact: {contact_name}")
    print(f"   Customer Phone: {customer_contact or '(not exposed by WhatsApp DOM)'}")
    print(f"   Media Type: {media_info.media_type}")
    if media_info.filename:
        print(f"   Attachment: {media_info.filename}")
    print("   Latest Incoming Message:")
    print(latest_message)
    if image_path:
        print(f"   Image: {image_path}")
    print("=" * 90)

    if (
        not latest_message
        and not image_path
        and media_info.media_type in ("text", "unknown")
        and not bubble_contains_document(bubble)
    ):
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
    if (
        classification.intent in ("unknown", "greeting", "general_chat")
        and (document_items or enrichment.get("document_text") or media_info.media_type == "pdf")
    ):
        classification.intent = "purchase_order"
        classification.confidence = max(classification.confidence, 0.9)
        classification.handler = "purchase_order"
        classification.reasoning = "PDF/PO document content detected after attachment extraction."
        classification.suggested_reply = (
            "Hi, thank you for sending your purchase order.\n\n"
            "Our team is reviewing the document and will confirm shortly."
        )
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

    if classification.handler == "rfq_inquiry" or (
        classification.handler == "purchase_order"
        and (document_items or image_analysis or media_info.media_type == "pdf")
    ):
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


def ensure_on_chat_list(driver, force_reload: bool = False):
    """Return to the main chat list (sidebar visible) after opening a specific chat."""
    if CHAT_PROCESSING_LOCK and not force_reload:
        print("ℹ️ Chat processing in progress — staying on open conversation")
        return True

    try:
        if not force_reload:
            if wait_for_search_box(driver, timeout=2):
                return True
            try:
                driver.execute_script(
                    "var b=document.querySelector('[data-testid=\"back\"]'); if(b){b.click();}"
                )
                time.sleep(1)
                if wait_for_search_box(driver, timeout=5):
                    return True
            except Exception:
                pass

        if CHAT_PROCESSING_LOCK and not force_reload:
            print("ℹ️ Chat processing in progress — skip WhatsApp reload")
            return True

        driver.get("https://web.whatsapp.com")
        global WHATSAPP_SESSION_READY
        WHATSAPP_SESSION_READY = False
        if not wait_for_whatsapp_ready(driver, timeout=30):
            return False
        WHATSAPP_SESSION_READY = True
        if wait_for_search_box(driver, timeout=15):
            return True
        time.sleep(2)
        return wait_for_search_box(driver, timeout=10) is not None
    except Exception as exc:
        print(f"⚠️ Could not return to chat list: {exc}")
        return False


def load_watch_contacts():
    """Contacts to re-check each cycle even when the chat is already read."""
    defaults = ["Robomatics Stephen", "Stephen"]
    if not os.path.exists(WHATSAPP_WATCH_CONTACTS_FILE):
        return defaults

    contacts = []
    try:
        with open(WHATSAPP_WATCH_CONTACTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    contacts.append(line)
    except Exception as exc:
        print(f"⚠️ Could not read watch contacts file: {exc}")
        return defaults

    return contacts or defaults


def load_last_processed_store():
    if not os.path.exists(WHATSAPP_LAST_PROCESSED_FILE):
        return {}
    try:
        with open(WHATSAPP_LAST_PROCESSED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_last_processed_store(store):
    try:
        with open(WHATSAPP_LAST_PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
    except Exception as exc:
        print(f"⚠️ Could not save last-processed store: {exc}")


def build_plan_fingerprint(plan):
    parts = []
    for unit in plan or []:
        data_id = str(unit.get("data_id") or "").strip()
        if data_id:
            parts.append(f"id:{data_id}")
            continue
        parts.append(
            f"{unit.get('incoming_index') or ''}|{unit.get('kind') or ''}|"
            f"{(unit.get('text') or '')[:80]}"
        )
    return "||".join(parts)


def build_process_fingerprint(units, contact_name: str = "", contact_hint: str = ""):
    plan = plan_sequential_units(units, contact_name, contact_hint)
    return build_plan_fingerprint(plan)


def finalize_chat_processing(contact_name, plan, voice_ok: bool = True, contact_hint: str = ""):
    if not plan:
        return
    if not voice_ok:
        print(
            "⚠️ Voice message not saved/transcribed — NOT marking processed "
            "(will retry on next scan)"
        )
        clear_wa_audio_workspace()
        return
    fingerprint = build_plan_fingerprint(plan)
    data_ids = [str(u.get("data_id") or "").strip() for u in plan if u.get("data_id")]
    store_key = contact_store_key(contact_name, contact_hint)
    mark_watch_contact_processed(store_key, fingerprint, data_ids)
    clear_wa_audio_workspace()


def should_process_watch_contact(contact_name, fingerprint, plan=None, contact_hint: str = ""):
    if not fingerprint:
        return False
    store_key = contact_store_key(contact_name, contact_hint)
    store = load_last_processed_store()
    prev = store.get(store_key)
    if prev and prev.get("fingerprint") == fingerprint:
        return False

    processed_ids = get_processed_data_ids(store_key)
    if plan and processed_ids:
        units_with_ids = [
            u for u in plan
            if str(u.get("data_id") or "").strip()
        ]
        if units_with_ids and all(
            str(u.get("data_id") or "").strip() in processed_ids for u in units_with_ids
        ):
            return False

    return True


def mark_watch_contact_processed(contact_name, fingerprint, data_ids=None):
    if not fingerprint and not data_ids:
        return
    store_key = contact_store_key(contact_name)
    store = load_last_processed_store()
    prev = store.get(store_key) or {}
    prev_ids = list(prev.get("processed_data_ids") or [])
    merged_ids = list(dict.fromkeys([*(data_ids or []), *prev_ids]))[:80]
    store[store_key] = {
        "fingerprint": fingerprint or prev.get("fingerprint") or "",
        "processed_at": now_iso(),
        "processed_data_ids": merged_ids,
    }
    save_last_processed_store(store)
    if data_ids:
        print(f"✅ Marked {len(data_ids)} WhatsApp message(s) processed for {store_key!r}")


def process_watched_contacts(driver, max_contacts: int = 1):
    """
    Re-check configured contacts even when WhatsApp shows them as read.
    FIFO: at most one contact per scan cycle.
    """
    contacts = load_watch_contacts()
    if not contacts:
        return False

    print("")
    print("👁️ No unread chats — checking watch-list contacts for new incoming content...")
    processed_any = False
    attempts = 0

    for contact_hint in contacts:
        if attempts >= max_contacts:
            break
        attempts += 1
        print(f"   🔎 Watch contact: {contact_hint}")
        ensure_on_chat_list(driver)

        if not open_chat_by_contact(driver, contact_hint):
            print(f"   ⚠️ Could not open watch contact: {contact_hint}")
            continue

        wait_for_open_chat_panel(driver, timeout=20)
        wait_for_chat_messages(driver, timeout=15)

        raw_contact_name = get_contact_name_from_open_chat(driver)
        units = collect_incoming_units(driver, lookback=INCOMING_LOOKBACK)
        contact_name = raw_contact_name or contact_hint
        store_key = contact_store_key(contact_name, contact_hint)
        plan = plan_sequential_units(units, contact_name, contact_hint)
        if not plan:
            print(f"   ℹ️ {contact_name}: no new unprocessed incoming messages")
            continue

        fingerprint = build_plan_fingerprint(plan)

        if not should_process_watch_contact(contact_name, fingerprint, plan=plan, contact_hint=contact_hint):
            print(f"   ℹ️ {contact_name}: already processed latest incoming cluster")
            continue

        print(f"   🆕 {contact_name}: new incoming detected — processing now")
        process_open_chat(driver)
        processed_any = True
        print("↩️ Returning to WhatsApp chat list after watch contact...")
        ensure_on_chat_list(driver)
        time.sleep(1)
        break

    return processed_any


def process_forced_contact(driver):
    """
    Process a specific contact even when the chat is already read.
    Trigger by creating whatsapp_process_contact.flag with the contact name/phone.
    """
    if not os.path.exists(PROCESS_CONTACT_FLAG):
        return False

    contact = "Stephen"
    try:
        with open(PROCESS_CONTACT_FLAG, "r", encoding="utf-8") as f:
            contact = f.read().strip() or contact
    except Exception as exc:
        print(f"⚠️ Could not read process-contact flag: {exc}")

    try:
        os.remove(PROCESS_CONTACT_FLAG)
    except Exception as exc:
        print(f"⚠️ Could not remove process-contact flag: {exc}")

    print("")
    print("=" * 90)
    print("🎯 FORCED CONTACT PROCESS REQUEST")
    print(f"   Contact: {contact}")
    print("   (Processing even if chat is already read)")
    print("=" * 90)

    ensure_on_chat_list(driver)

    if not open_chat_by_contact(driver, contact):
        print(f"❌ Forced process failed — could not open chat: {contact}")
        return False

    process_open_chat(driver)
    time.sleep(2)
    ensure_on_chat_list(driver)
    return True


def process_contact_now(driver, contact_hint):
    """Open and process one contact immediately (CLI helper)."""
    print("")
    print("=" * 90)
    print(f"🎯 MANUAL PROCESS: {contact_hint}")
    print("=" * 90)
    if not open_chat_by_contact(driver, contact_hint):
        return False
    process_open_chat(driver)
    return True


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
    if not ensure_whatsapp_session(driver):
        return

    if process_mark_unread_request(driver):
        print("↩️ Returning to WhatsApp chat list after mark-unread...")
        ensure_on_chat_list(driver)

    if not CHAT_PROCESSING_LOCK:
        process_monitor_feedback(driver)
    ensure_on_chat_list(driver)

    if process_forced_contact(driver):
        print("✅ Forced contact processed.")
        return

    unread_rows = find_unread_chat_rows(driver)

    if not unread_rows:
        print("✅ No unread WhatsApp chats found.")
        if watch_read_contacts_enabled() and process_watched_contacts(driver, max_contacts=1):
            return
        print(
            'ℹ️ To force-process a read chat: uv run python whatsapp_inbox_watcher.py process "Stephen"\n'
            'ℹ️ To disable read-chat polling: set OPENCLAW_WHATSAPP_WATCH_READ_CONTACTS=0'
        )
        return

    row = unread_rows[0]
    print("")
    print("-" * 90)
    print(f"📬 FIFO: processing 1 unread chat (queue has {len(unread_rows)})")

    if not open_unread_chat(driver, row):
        return

    process_open_chat(driver)

    print("↩️ Returning to WhatsApp chat list after completed chat...")
    ensure_on_chat_list(driver)
    time.sleep(1)


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
                    global WHATSAPP_SESSION_READY
                    WHATSAPP_SESSION_READY = False
                    wait_for_whatsapp_ready(driver, timeout=60)
                    WHATSAPP_SESSION_READY = True
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
    elif len(sys.argv) > 1 and sys.argv[1] == "process":
        contact = sys.argv[2] if len(sys.argv) > 2 else "Stephen"
        driver = None

        try:
            driver = init_driver()
            driver.get("https://web.whatsapp.com")
            if not wait_for_whatsapp_ready(driver):
                sys.exit(1)
            success = process_contact_now(driver, contact)
            sys.exit(0 if success else 1)
        finally:
            if driver:
                driver.quit()
    else:
        run_persistent_watcher()
