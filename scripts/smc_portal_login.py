#!/usr/bin/env python3
"""Open SMC portal in a dedicated Chrome profile — log in manually once."""

from __future__ import annotations

import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from smc_portal_lookup import (  # noqa: E402
    SMC_CHROME_PROFILE,
    close_driver,
    ensure_portal_session,
    get_driver,
    portal_home_url,
    portal_login_url,
)


def main() -> int:
    print("=" * 72)
    print("SMC Portal — manual login helper")
    print(f"Profile: {SMC_CHROME_PROFILE}")
    print(f"Login:  {portal_login_url()}")
    print(f"Home:   {portal_home_url()}")
    print()
    print("1. Chrome will open with the SMC profile (extensions are kept).")
    print("2. Log in to the SMC portal if prompted.")
    print("3. Leave this window open ~60s, then press Ctrl+C when done.")
    print("=" * 72)

    driver = get_driver()
    driver.get(portal_login_url())
    time.sleep(3)

    if ensure_portal_session(driver):
        print("✅ Portal session looks logged in.")
    else:
        print("⚠️ Still on login page — complete login in Chrome, then re-run probe.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Done — session saved in chrome_smc_profile.")
    finally:
        close_driver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
