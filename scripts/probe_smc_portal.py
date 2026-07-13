#!/usr/bin/env python3
"""
Probe SMC distributor portal DOM — run on Mac after logging in.

Usage:
  uv run python scripts/smc_portal_login.py          # log in first (separate step)
  uv run python scripts/probe_smc_portal.py          # dump portal structure
  uv run python scripts/probe_smc_portal.py 10-KQ2H06-M5N
"""

from __future__ import annotations

import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.runtime_deps import require_selenium_for_scripts

require_selenium_for_scripts()

from selenium.webdriver.common.by import By

from smc_portal_lookup import (  # noqa: E402
    SMC_CACHE_FILE,
    SMC_CHROME_PROFILE,
    SMC_CONFIG_FILE,
    close_driver,
    ensure_portal_session,
    get_driver,
    lookup_smc_quote,
    portal_base_url,
    portal_home_url,
)


def _dump_inputs(driver) -> None:
    print("\n=== Visible input fields ===")
    for el in driver.find_elements(By.CSS_SELECTOR, "input, textarea, select"):
        try:
            if not el.is_displayed():
                continue
            print(
                f"  tag={el.tag_name} type={el.get_attribute('type')!r} "
                f"name={el.get_attribute('name')!r} id={el.get_attribute('id')!r} "
                f"placeholder={el.get_attribute('placeholder')!r}"
            )
        except Exception:
            continue


def _dump_links(driver, limit: int = 40) -> None:
    print(f"\n=== First {limit} links (href contains Search/Product/Catalog) ===")
    count = 0
    for el in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        try:
            href = el.get_attribute("href") or ""
            text = (el.text or "").strip()
            if not href:
                continue
            lower = href.lower()
            if not any(token in lower for token in ("search", "product", "catalog", "home", "price")):
                continue
            print(f"  {text[:40]!r} -> {href}")
            count += 1
            if count >= limit:
                break
        except Exception:
            continue


def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else ""
    print(f"Profile: {SMC_CHROME_PROFILE}")
    print(f"Base URL: {portal_base_url()}")

    driver = get_driver()
    driver.get(portal_home_url())
    time.sleep(2)

    logged_in = ensure_portal_session(driver)
    print(f"Logged in: {logged_in}")
    print(f"Current URL: {driver.current_url}")
    print(f"Title: {driver.title}")

    _dump_inputs(driver)
    _dump_links(driver)

    artifact_dir = os.path.join(_REPO_ROOT, "logs")
    os.makedirs(artifact_dir, exist_ok=True)
    html_path = os.path.join(artifact_dir, "smc_portal_probe.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(driver.page_source)
    print(f"\nSaved HTML: {html_path}")

    if part:
        print(f"\n=== Live lookup test: {part} ===")
        quote = lookup_smc_quote(part, qty=1)
        print("\nQuote dict:")
        print(json.dumps(quote, indent=2, default=str))

    if not os.path.exists(SMC_CONFIG_FILE):
        example = {
            "search_input": ["#your-search-field"],
            "search_button": ["#your-search-button"],
            "search_url_templates": [
                "{base}/Your/SearchPath?model={part}",
            ],
            "result_table": ["table.your-results"],
        }
        example_path = os.path.join(_REPO_ROOT, "smc_portal_config.json.example")
        with open(example_path, "w", encoding="utf-8") as handle:
            json.dump(example, handle, indent=2)
        print(f"\nWrote selector template: {example_path}")
        print(f"Copy to {SMC_CONFIG_FILE} after editing selectors from probe output.")

    print(f"\nCache file: {SMC_CACHE_FILE}")
    close_driver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
