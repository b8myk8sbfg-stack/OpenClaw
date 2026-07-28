"""
Purchasing-line WhatsApp: separate Chrome profile for outbound supplier RFQs.

Sender session: purchasing number (60167027683) in chrome_purchasing_whatsapp_profile.
Used for OMRON and other brands configured in supplier_whatsapp_config.
"""

from __future__ import annotations

import os
import socket
import time

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from channel_router import (
    build_supplier_rfq_text,
    log_supplier_pending,
    send_purchasing_internal_rfq_copy,
    send_whatsapp_message,
)
from supplier_whatsapp_config import (
    get_purchasing_sender_phone,
    get_supplier_destination,
    uses_purchasing_whatsapp,
)

load_dotenv()

VERSION = "v1.00-PURCHASING-WHATSAPP"

CHROME_BINARY_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Users/evon/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

PURCHASING_CHROME_PROFILE = os.getenv(
    "OPENCLAW_PURCHASING_CHROME_PROFILE",
    "/Users/evon/OpenClaw/chrome_purchasing_whatsapp_profile",
)

_purchasing_driver = None


def find_chrome_binary() -> str:
    for path in CHROME_BINARY_PATHS:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("Google Chrome binary not found.")


def _get_purchasing_debugger_address() -> str | None:
    port_file = os.path.join(PURCHASING_CHROME_PROFILE, "DevToolsActivePort")
    try:
        with open(port_file, "r", encoding="utf-8") as handle:
            port = int(handle.readline().strip())
        with socket.create_connection(("127.0.0.1", port), timeout=0.75):
            return f"127.0.0.1:{port}"
    except (ValueError, OSError, FileNotFoundError):
        try:
            if os.path.exists(port_file):
                os.remove(port_file)
        except OSError:
            pass
        return None


def init_purchasing_driver(force_new: bool = False):
    """Open or attach to purchasing WhatsApp Chrome (separate from sales inbox)."""
    global _purchasing_driver

    if _purchasing_driver is not None and not force_new:
        try:
            _ = _purchasing_driver.current_url
            return _purchasing_driver
        except Exception:
            _purchasing_driver = None

    chrome_binary = find_chrome_binary()
    os.makedirs(PURCHASING_CHROME_PROFILE, exist_ok=True)

    options = Options()
    options.binary_location = chrome_binary
    debugger = _get_purchasing_debugger_address()
    if debugger and not force_new:
        print(f"♻️ [PURCHASING] Attaching to existing Chrome: {debugger}")
        options.add_experimental_option("debuggerAddress", debugger)
    else:
        options.add_argument(f"--user-data-dir={PURCHASING_CHROME_PROFILE}")
        options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    _purchasing_driver = webdriver.Chrome(options=options)
    print(f"✅ [PURCHASING] Chrome ready — profile: {PURCHASING_CHROME_PROFILE}")
    print(f"   Purchasing sender: +{get_purchasing_sender_phone()}")
    return _purchasing_driver


def ensure_purchasing_whatsapp_session(driver, timeout: int = 90) -> bool:
    from selenium.webdriver.common.by import By

    driver.get("https://web.whatsapp.com")
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.find_elements(By.XPATH, '//div[@id="side"]'):
                print("✅ [PURCHASING] WhatsApp Web session ready.")
                return True
        except Exception:
            pass
        time.sleep(3)
    print("❌ [PURCHASING] WhatsApp Web not logged in — scan QR on purchasing Chrome.")
    return False


def send_purchasing_supplier_rfq(
    brand: str,
    items: list,
    ref: str,
    customer_name=None,
    customer_contact=None,
) -> dict:
    """
    Send RFQ to external supplier via purchasing WhatsApp Chrome.
    Returns router-style result dict.
    """
    brand = str(brand or "").upper().strip()
    if not uses_purchasing_whatsapp(brand):
        return {"channel": "PURCHASING_WHATSAPP", "to": "", "status": "SKIPPED"}

    destination = get_supplier_destination(brand)
    if not destination:
        print(
            f"⚠️ [PURCHASING] No supplier destination for {brand}. "
            f"Set supplier_whatsapp_numbers.json or OPENCLAW_{brand}_SUPPLIER_WHATSAPP(_GROUP)"
        )
        return {"channel": "PURCHASING_WHATSAPP", "to": "", "status": "NO_SUPPLIER_NUMBER"}

    msg = build_supplier_rfq_text(
        brand, items, ref,
        customer_name=customer_name,
        customer_contact=customer_contact,
        include_customer_info=False,
    )

    print("")
    print("=" * 90)
    print(f"📲 [PURCHASING] SUPPLIER RFQ — {brand}")
    print(f"   From: purchasing +{get_purchasing_sender_phone()}")
    print(f"   To: {destination.label} ({destination.kind})")
    print(f"   Ref: {ref}")
    print("=" * 90)

    driver = init_purchasing_driver()
    if not ensure_purchasing_whatsapp_session(driver, timeout=60):
        return {
            "channel": "PURCHASING_WHATSAPP",
            "to": destination.label,
            "status": "SESSION_NOT_READY",
        }

    success = send_whatsapp_message(driver, destination, msg)
    if success:
        log_supplier_pending(
            ref=ref,
            brand=brand,
            customer_name=customer_name,
            customer_contact=customer_contact,
            supplier_channel="PURCHASING_WHATSAPP",
            supplier_to=destination.label,
            items=items,
        )
        send_purchasing_internal_rfq_copy(
            brand=brand,
            items=items,
            ref=ref,
            customer_name=customer_name,
            customer_contact=customer_contact,
        )
        return {"channel": "PURCHASING_WHATSAPP", "to": destination.label, "status": "SENT"}

    return {"channel": "PURCHASING_WHATSAPP", "to": destination.label, "status": "FAILED"}
