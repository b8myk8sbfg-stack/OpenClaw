"""
Watch the purchasing WhatsApp Chrome for external supplier replies (WA- refs).

Runs on chrome_purchasing_whatsapp_profile — separate from the sales customer inbox.
"""

from __future__ import annotations

import os
import re
import time

from dotenv import load_dotenv

from openclaw_log import enable_timestamped_logging
from purchasing_whatsapp import (
    VERSION as PURCHASING_VERSION,
    ensure_purchasing_whatsapp_session,
    init_purchasing_driver,
    PURCHASING_CHROME_PROFILE,
)
from supplier_whatsapp_config import get_purchasing_sender_phone, list_purchasing_whatsapp_brands

load_dotenv()
enable_timestamped_logging()

VERSION = "v1.00-PURCHASING-WATCHER"
CHECK_INTERVAL_SECONDS = int(os.getenv("OPENCLAW_PURCHASING_POLL_SECONDS", "60"))


def _has_supplier_ref(text: str) -> bool:
    return bool(re.search(r"WA-\d{8}-[A-Z0-9]+-[A-Z0-9]+", str(text or ""), re.I))


def run_purchasing_scan_cycle(driver) -> None:
    """Scan purchasing WhatsApp for supplier replies containing WA- refs."""
    # Late import avoids circular load at startup.
    from whatsapp_inbox_watcher import (
        ensure_on_chat_list,
        find_unread_chat_rows,
        get_latest_incoming_message,
        get_contact_name_from_open_chat,
        open_unread_chat,
        process_supplier_reply,
    )

    if not ensure_purchasing_whatsapp_session(driver, timeout=30):
        return

    ensure_on_chat_list(driver)
    unread_rows = find_unread_chat_rows(driver)
    if not unread_rows:
        print("✅ [PURCHASING] No unread chats on purchasing line.")
        return

    row = unread_rows[0]
    print(f"📬 [PURCHASING] Processing 1 unread chat (queue: {len(unread_rows)})")
    if not open_unread_chat(driver, row):
        return

    contact_name = get_contact_name_from_open_chat(driver)
    latest_message = get_latest_incoming_message(driver) or ""

    if _has_supplier_ref(latest_message):
        print(f"📥 [PURCHASING] Supplier reply from {contact_name}")
        process_supplier_reply(driver, contact_name, latest_message)
    else:
        print(f"ℹ️ [PURCHASING] Unread from {contact_name} — no WA- ref, skipped.")

    ensure_on_chat_list(driver)


def run_persistent_purchasing_watcher():
    print(f"🚀 Purchasing WhatsApp Watcher ({VERSION})")
    print(f"📁 Chrome profile: {PURCHASING_CHROME_PROFILE}")
    print(f"📱 Purchasing sender: +{get_purchasing_sender_phone()}")
    print(f"🏷️ Brands on purchasing WA: {', '.join(list_purchasing_whatsapp_brands())}")
    print(f"⏱️ Poll interval: {CHECK_INTERVAL_SECONDS}s")

    driver = None
    try:
        driver = init_purchasing_driver()
        while True:
            try:
                print("")
                print("=" * 90)
                print(f"🔁 Purchasing WhatsApp scan @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
                run_purchasing_scan_cycle(driver)
            except Exception as exc:
                print(f"⚠️ [PURCHASING] Scan error: {exc}")
                try:
                    driver = init_purchasing_driver(force_new=True)
                except Exception as reinit_exc:
                    print(f"❌ [PURCHASING] Driver reinit failed: {reinit_exc}")
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("🛑 Purchasing watcher stopped.")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    run_persistent_purchasing_watcher()
