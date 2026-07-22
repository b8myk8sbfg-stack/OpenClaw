#!/usr/bin/env python3
"""Open SMC dealer portal — log in manually once (saved in chrome_smc_profile)."""

from __future__ import annotations

import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.runtime_deps import require_selenium_for_scripts

require_selenium_for_scripts()

try:
    from dotenv import load_dotenv

    for _env in (
        os.path.join(_REPO_ROOT, ".env"),
        os.path.expanduser("~/OpenClaw/.env"),
        os.path.expanduser("~/openclaw/.env"),
    ):
        if os.path.isfile(_env):
            load_dotenv(_env)
            break
except ImportError:
    pass

from smc_portal_lookup import (  # noqa: E402
    SMC_CHROME_PROFILE,
    close_driver,
    ensure_portal_session,
    get_driver,
    portal_credentials,
    portal_item_enquiry_url,
    portal_login_url,
    search_portal_part,
)


def main() -> int:
    user, _ = portal_credentials()
    print("=" * 72)
    print("SMC Dealer Portal — login helper")
    print(f"Profile:  {SMC_CHROME_PROFILE}")
    print(f"Landing:  {portal_login_url()}")
    print(f"Enquiry:  {portal_item_enquiry_url()}")
    print(f"User:     {user or '(set SMC_PORTAL_USERNAME or USER_ID in .env)'}")
    print()
    print("Automated flow: Account/Index → SMC Dealer Login → Item Enquiry")
    print("Press Ctrl+C when done — session saved; portal log off by default on exit.")
    print("Set SMC_PORTAL_LOGOUT_ON_CLOSE=0 to keep portal logged in.")
    print("=" * 72)

    driver = get_driver()
    if ensure_portal_session(driver):
        print("✅ Logged in.")
        test_part = sys.argv[1] if len(sys.argv) > 1 else ""
        if test_part:
            hit = search_portal_part(driver, test_part)
            print(hit)
    else:
        print("⚠️ Login not complete — finish in Chrome window.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Session saved.")
    finally:
        close_driver(logout=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

