"""
SMC Malaysia dealer portal lookup (my.smccorporation.co:8001).

Flow (Dealer Login → Item Enquiry):
  1. Account/Index → SMC Dealer Login
  2. User Id + Password
  3. Enquiry → Item Inquiry (/ItemEnquiry)
  4. Customer Code/Name: Robo → MYC0003071 | ROBOMATICS (JOHOR) SDN BHD
  5. Part No. → Enquire → parse Item List grid (N/Price, Avail, PNT1/2, Rec Prod No 1)

Lead time:
  - Stock in JH / PG / SJ (Avail>0 or PNT1/PNT2>0) → 1 week
  - No peninsula stock → indent Japan → 4-6 weeks
  - Obsolete (Rec Prod No 1 set, zero stock) → re-search replacement part
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from datetime import datetime, timedelta
from typing import Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

BASE_DIR = os.getenv("OPENCLAW_BASE_DIR", "/Users/evon/OpenClaw")


def _load_env() -> None:
    """Load .env from OpenClaw install dir (scripts do not inherit shell exports)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (
        os.path.join(BASE_DIR, ".env"),
        os.path.expanduser("~/OpenClaw/.env"),
        os.path.expanduser("~/openclaw/.env"),
    ):
        if os.path.isfile(candidate):
            load_dotenv(candidate, override=False)


_load_env()
BASE_DIR = os.getenv("OPENCLAW_BASE_DIR", BASE_DIR)

DEFAULT_BASE_URL = "https://my.smccorporation.co:8001"
DEFAULT_HOME_URL = f"{DEFAULT_BASE_URL}/Home"
DEFAULT_LOGIN_URL = f"{DEFAULT_BASE_URL}/Account/Index?ReturnUrl=%2fHome"
DEFAULT_DEALER_LOGIN_URL = f"{DEFAULT_BASE_URL}/Account/Login"
DEFAULT_ITEM_ENQUIRY_URL = f"{DEFAULT_BASE_URL}/ItemEnquiry"

CHROME_BINARY_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Users/evon/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

SMC_CHROME_PROFILE = os.getenv(
    "SMC_PORTAL_CHROME_PROFILE",
    os.path.join(BASE_DIR, "chrome_smc_profile"),
)
SMC_CACHE_FILE = os.path.join(BASE_DIR, ".openclaw_smc_portal_cache.json")
SMC_CONFIG_FILE = os.path.join(BASE_DIR, "smc_portal_config.json")

_DRIVER: webdriver.Chrome | None = None
_SESSION_READY = False

MONEY_RE = re.compile(
    r"(?:MYR|RM)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
    re.I,
)
PENINSULA_WAREHOUSES = frozenset({"JH", "PG", "SJ"})
LT_EX_STOCK = os.getenv("SMC_PORTAL_LT_EX_STOCK", "1 week")
LT_INDENT = os.getenv("SMC_PORTAL_LT_INDENT", "4-6 weeks")

SCRAPE_ITEM_LIST_JS = """
const wanted = new Set(arguments[0].map(h => String(h || '').toLowerCase().trim()));

function headerKey(text) {
    return String(text || '').toLowerCase().replace(/\\s+/g, ' ').trim();
}

function parseTables() {
    const results = [];
    const tables = document.querySelectorAll('table');
    for (const table of tables) {
        const headerCells = [];
        const headerRow = table.querySelector('thead tr') || table.querySelector('tr');
        if (!headerRow) continue;
        for (const cell of headerRow.querySelectorAll('th, td')) {
            headerCells.push(headerKey(cell.innerText));
        }
        if (!headerCells.some(h => h.includes('p/n') || h.includes('n/price'))) continue;

        const colMap = {};
        headerCells.forEach((h, i) => { if (h) colMap[h] = i; });

        const bodyRows = table.querySelectorAll('tbody tr');
        const dataRows = bodyRows.length ? bodyRows : table.querySelectorAll('tr');
        for (const row of dataRows) {
            const cells = Array.from(row.querySelectorAll('td')).map(td => (td.innerText || '').trim());
            if (cells.length < 3) continue;
            const get = (...names) => {
                for (const name of names) {
                    const idx = colMap[name];
                    if (idx !== undefined && cells[idx] !== undefined) return cells[idx];
                }
                for (const [key, idx] of Object.entries(colMap)) {
                    for (const name of names) {
                        if (key.includes(name)) return cells[idx];
                    }
                }
                return '';
            };
            const pn = get('p/n', 'pn', 'part');
            if (!pn || pn === '#' || pn.toLowerCase() === 'p/n') continue;
            results.push({
                pn: pn,
                whs: get('whs', 'warehouse'),
                net_price_text: get('n/price', 'n price', 'net price'),
                avail: get('avail', 'available'),
                pnt1: get('pnt 1', 'pnt1'),
                pnt2: get('pnt 2', 'pnt2'),
                description: get('description', 'desc'),
                ex_fac_days: get('ex-fac (days)', 'ex-fac', 'ex fac'),
                rec_prod_no: get('rec prod no 1', 'rec prod', 'replacement'),
                price_source: get('price source'),
                raw: cells.join(' | '),
            });
        }
    }
    return results;
}

// Scroll Kendo grids horizontally to reveal hidden columns
const scrollers = document.querySelectorAll('.k-grid-content, .k-virtual-scrollable-wrap');
for (const el of scrollers) {
    try { el.scrollLeft = el.scrollWidth; } catch (e) {}
}
return parseTables();
"""


def smc_portal_enabled() -> bool:
    return os.getenv("SMC_PORTAL_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def portal_base_url() -> str:
    return os.getenv("SMC_PORTAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def portal_home_url() -> str:
    return os.getenv("SMC_PORTAL_HOME_URL", f"{portal_base_url()}/Home")


def portal_login_url() -> str:
    return os.getenv("SMC_PORTAL_LOGIN_URL", DEFAULT_LOGIN_URL)


def portal_dealer_login_url() -> str:
    return os.getenv("SMC_PORTAL_DEALER_LOGIN_URL", DEFAULT_DEALER_LOGIN_URL)


def portal_item_enquiry_url() -> str:
    return os.getenv("SMC_PORTAL_ITEM_ENQUIRY_URL", DEFAULT_ITEM_ENQUIRY_URL)


def portal_credentials() -> tuple[str, str]:
    """Read dealer credentials from env (never log the password)."""
    _load_env()
    user = (
        os.getenv("SMC_PORTAL_USERNAME")
        or os.getenv("SMC_PORTAL_USER_ID")
        or os.getenv("USER_ID")
        or ""
    ).strip()
    password = (
        os.getenv("SMC_PORTAL_PASSWORD")
        or os.getenv("PASSWORD")
        or ""
    ).strip()
    return user, password


def customer_search_hint() -> str:
    return os.getenv("SMC_PORTAL_CUSTOMER_HINT", "Robo").strip() or "Robo"


def customer_select_match() -> str:
    return os.getenv(
        "SMC_PORTAL_CUSTOMER_CODE",
        "MYC0003071",
    ).strip() or "MYC0003071"


def cache_ttl_hours() -> float:
    try:
        return float(os.getenv("SMC_PORTAL_CACHE_HOURS", "24"))
    except ValueError:
        return 24.0


def normalize_smc_part(part: str) -> str:
    raw = str(part or "").strip().upper()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    if re.match(r"^\d{1,2}[A-Z]", compact) and "-" not in compact:
        m = re.match(r"^(\d{1,2})([A-Z].+)$", compact)
        if m:
            body = m.group(2)
            if len(body) > 6 and "-" not in body:
                return f"{m.group(1)}-{body[:6]}-{body[6:]}"
            return f"{m.group(1)}-{body}"
    return raw


def compact_part(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def parts_equal(a: str, b: str) -> bool:
    return compact_part(a) == compact_part(b)


def smc_lookup_keys(part_no: str) -> list[str]:
    raw = str(part_no or "").strip()
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            keys.append(value)

    add(raw)
    add(raw.upper())
    add(normalize_smc_part(raw))
    return keys


def parse_qty(value: Any) -> int:
    text = str(value or "").strip()
    if not text or text in ("-", "—"):
        return 0
    match = re.search(r"-?\d+", text.replace(",", ""))
    if not match:
        return 0
    try:
        return max(0, int(match.group(0)))
    except ValueError:
        return 0


def parse_money(text: str) -> float | None:
    match = MONEY_RE.search(str(text or ""))
    if not match:
        match = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def compute_lead_time_from_rows(rows: list[dict[str, Any]]) -> str:
    """JH/PG/SJ Avail or PNT assembly → 1 week; else indent 4-6 weeks."""
    for row in rows:
        whs = str(row.get("whs") or "").upper().strip()
        if whs not in PENINSULA_WAREHOUSES:
            continue
        if parse_qty(row.get("avail")) > 0:
            return LT_EX_STOCK
        if parse_qty(row.get("pnt1")) > 0 or parse_qty(row.get("pnt2")) > 0:
            return LT_EX_STOCK
    return LT_INDENT


def pick_exact_part_rows(rows: list[dict[str, Any]], searched_part: str) -> list[dict[str, Any]]:
    return [r for r in rows if parts_equal(str(r.get("pn") or ""), searched_part)]


def pick_best_price_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None

    def sort_key(row: dict[str, Any]) -> tuple:
        whs = str(row.get("whs") or "").upper()
        in_peninsula = whs in PENINSULA_WAREHOUSES
        avail = parse_qty(row.get("avail"))
        pnt = parse_qty(row.get("pnt1")) + parse_qty(row.get("pnt2"))
        price = parse_money(str(row.get("net_price_text") or "")) or 999999999.0
        return (0 if in_peninsula and (avail > 0 or pnt > 0) else 1, price)

    return sorted(rows, key=sort_key)[0]


def build_stock_note(rows: list[dict[str, Any]]) -> str:
    bits = []
    for row in rows:
        whs = str(row.get("whs") or "").upper()
        if whs not in PENINSULA_WAREHOUSES:
            continue
        avail = parse_qty(row.get("avail"))
        pnt1 = parse_qty(row.get("pnt1"))
        pnt2 = parse_qty(row.get("pnt2"))
        if avail > 0:
            bits.append(f"{whs} Avail={avail}")
        elif pnt1 or pnt2:
            bits.append(f"{whs} assemble PNT1={pnt1} PNT2={pnt2}")
    return "; ".join(bits)


def row_to_hit(
    row: dict[str, Any],
    searched_part: str,
    all_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    net_price = parse_money(str(row.get("net_price_text") or ""))
    exact_rows = pick_exact_part_rows(all_rows, searched_part) or [row]
    lead_time = compute_lead_time_from_rows(exact_rows)
    desc = str(row.get("description") or "").strip() or f"SMC {row.get('pn') or searched_part}"
    stock_note = build_stock_note(exact_rows)
    if stock_note:
        desc = f"{desc} ({stock_note})"
    return {
        "part_no": str(row.get("pn") or searched_part).strip(),
        "desc": desc,
        "net_price": net_price,
        "lead_time": lead_time,
        "whs": row.get("whs"),
        "avail": parse_qty(row.get("avail")),
        "pnt1": parse_qty(row.get("pnt1")),
        "pnt2": parse_qty(row.get("pnt2")),
        "ex_fac_days": row.get("ex_fac_days"),
        "rec_prod_no": str(row.get("rec_prod_no") or "").strip(),
        "obsolete": bool(str(row.get("rec_prod_no") or "").strip()),
        "raw_row": row.get("raw"),
    }


def resolve_hit_from_rows(
    rows: list[dict[str, Any]],
    searched_part: str,
) -> dict[str, Any] | None:
    if not rows:
        return None

    exact = pick_exact_part_rows(rows, searched_part)
    if exact:
        best = pick_best_price_row(exact)
        if best:
            hit = row_to_hit(best, searched_part, exact)
            rec = hit.get("rec_prod_no")
            if rec and hit.get("lead_time") == LT_INDENT and not build_stock_note(exact):
                hit["replacement_part"] = rec
            return hit

    # Accessory variants (e.g. -M9B) listed but no exact row — use closest prefix match
    compact = compact_part(searched_part)
    prefix_rows = [
        r for r in rows
        if compact_part(str(r.get("pn") or "")).startswith(compact)
    ]
    if prefix_rows:
        best = pick_best_price_row(prefix_rows)
        if best:
            return row_to_hit(best, searched_part, prefix_rows)

    return None


def _find_chrome_binary() -> str:
    for path in CHROME_BINARY_PATHS:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("Google Chrome binary not found.")


def _get_debugger_address(profile_dir: str) -> str | None:
    port_file = os.path.join(profile_dir, "DevToolsActivePort")
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


def get_driver(force_new: bool = False) -> webdriver.Chrome:
    global _DRIVER
    if _DRIVER is not None and not force_new:
        try:
            _ = _DRIVER.current_url
            return _DRIVER
        except Exception:
            try:
                _DRIVER.quit()
            except Exception:
                pass
            _DRIVER = None

    chrome_binary = _find_chrome_binary()
    os.makedirs(SMC_CHROME_PROFILE, exist_ok=True)
    options = Options()
    options.binary_location = chrome_binary
    debugger = _get_debugger_address(SMC_CHROME_PROFILE)
    if debugger:
        print(f"♻️ [SMC] Attaching to existing Chrome session: {debugger}")
        options.add_experimental_option("debuggerAddress", debugger)
    else:
        options.add_argument(f"--user-data-dir={SMC_CHROME_PROFILE}")
        options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-first-run")
    options.page_load_strategy = "eager"
    if os.getenv("SMC_PORTAL_HEADLESS", "0").strip().lower() in ("1", "true", "yes"):
        options.add_argument("--headless=new")
    _DRIVER = webdriver.Chrome(options=options)
    print(f"✅ [SMC] Chrome ready (profile: {SMC_CHROME_PROFILE})")
    return _DRIVER


def close_driver() -> None:
    global _DRIVER, _SESSION_READY
    if _DRIVER is not None:
        try:
            _DRIVER.quit()
        except Exception:
            pass
        _DRIVER = None
    _SESSION_READY = False


def _find_first(driver, selectors: list[str]):
    for selector in selectors:
        try:
            if selector.startswith("//"):
                elements = driver.find_elements(By.XPATH, selector)
            else:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                if el.is_displayed():
                    return el
        except Exception:
            continue
    return None


def _wait_for_page(driver, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            ready = driver.execute_script("return document.readyState")
            body_len = len((driver.find_element(By.TAG_NAME, "body").text or "").strip())
            if ready == "complete" and body_len > 20:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _on_dealer_login_page(driver) -> bool:
    url = str(driver.current_url or "").lower()
    if "/account/login" in url:
        return True
    return _input_by_label(driver, "User Id") is not None or _input_by_label(driver, "User ID") is not None


def _click_element_by_text(driver, text: str) -> bool:
    """Click link/button whose visible text contains `text` (JS — works when Selenium click fails)."""
    try:
        return bool(
            driver.execute_script(
                """
                const needle = String(arguments[0] || '').toLowerCase();
                const nodes = document.querySelectorAll(
                    'a, button, input[type=submit], input[type=button], [role=link]'
                );
                for (const el of nodes) {
                    const label = (el.innerText || el.textContent || el.value || '').trim().toLowerCase();
                    if (!label.includes(needle)) continue;
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
                return false;
                """,
                text,
            )
        )
    except Exception:
        return False


def _click_link_by_text(driver, text: str) -> bool:
    if _click_element_by_text(driver, text):
        return True
    xpath = f"//a[contains(normalize-space(.), {json.dumps(text)})]"
    try:
        for el in driver.find_elements(By.XPATH, xpath):
            if el.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                driver.execute_script("arguments[0].click();", el)
                return True
    except Exception:
        pass
    return False


def _go_to_dealer_login_page(driver) -> bool:
    """Land on the Dealer Login form — direct URL first, then click from landing page."""
    print("🌐 [SMC] Navigating to Dealer Login...")
    driver.get(portal_dealer_login_url())
    _wait_for_page(driver, timeout=15)
    time.sleep(1)
    if _on_dealer_login_page(driver):
        print(f"✅ [SMC] Dealer Login page ready: {driver.current_url}")
        return True

    print("🔄 [SMC] Direct URL missed — trying landing page link click...")
    driver.get(portal_login_url())
    _wait_for_page(driver, timeout=15)
    time.sleep(1)

    for label in ("SMC Dealer Login", "Dealer Login", "Dealer's Login"):
        print(f"   🖱️ [SMC] Clicking {label!r}...")
        if _click_link_by_text(driver, label):
            time.sleep(2)
            if _on_dealer_login_page(driver):
                print(f"✅ [SMC] Dealer Login page ready: {driver.current_url}")
                return True

    driver.get(portal_dealer_login_url())
    _wait_for_page(driver, timeout=15)
    ok = _on_dealer_login_page(driver)
    if ok:
        print(f"✅ [SMC] Dealer Login page ready: {driver.current_url}")
    else:
        print(f"⚠️ [SMC] Still not on Dealer Login. URL={driver.current_url}")
    return ok


def _input_by_label(driver, label: str):
    xpath = (
        f"//label[contains(normalize-space(.), {json.dumps(label)})]"
        f"/following::input[1]"
    )
    try:
        el = driver.find_element(By.XPATH, xpath)
        if el.is_displayed():
            return el
    except Exception:
        pass
    return _find_first(driver, [
        f"input[name*='{label.split()[0]}' i]",
        f"input[id*='{label.split()[0]}' i]",
    ])


def _page_looks_logged_in(driver) -> bool:
    url = str(driver.current_url or "").lower()
    if "/account/index" in url or "/account/login" in url:
        return False
    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
        if re.search(r"hello\s+\w+", body, re.I):
            return True
        if "log off" in body.lower() and "dealer login" not in body.lower():
            return True
    except Exception:
        pass
    return "/home" in url or "/itemenquiry" in url


def _click_dealer_login(driver) -> bool:
    return _go_to_dealer_login_page(driver)


def _dealer_login(driver, username: str, password: str) -> bool:
    if not username or not password:
        return False

    if not _on_dealer_login_page(driver):
        if not _go_to_dealer_login_page(driver):
            return False

    user_el = _input_by_label(driver, "User Id") or _input_by_label(driver, "User ID")
    pass_el = _input_by_label(driver, "Password")
    if not user_el or not pass_el:
        print("⚠️ [SMC] Dealer login fields not found on page.")
        print(f"   URL: {driver.current_url}")
        return False

    print(f"🔐 [SMC] Filling login for {username!r}...")
    user_el.clear()
    user_el.send_keys(username)
    pass_el.clear()
    pass_el.send_keys(password)

    submit = _find_first(driver, [
        "//input[@value='Log in']",
        "//button[contains(.,'Log in')]",
        "//input[contains(@value,'Log in')]",
        "input[type='submit']",
        "button[type='submit']",
    ])
    if submit:
        driver.execute_script("arguments[0].click();", submit)
    else:
        pass_el.send_keys(Keys.RETURN)
    time.sleep(3)
    if _page_looks_logged_in(driver):
        print(f"✅ [SMC] Logged in — {driver.current_url}")
        return True
    print(f"⚠️ [SMC] Login submit done but session not detected. URL={driver.current_url}")
    return False


def ensure_portal_session(driver, force_reload: bool = False) -> bool:
    global _SESSION_READY
    _load_env()

    if not force_reload and _SESSION_READY and _page_looks_logged_in(driver):
        return True

    current = str(driver.current_url or "").lower()
    if portal_base_url().lower() not in current:
        print(f"🌐 [SMC] Opening portal: {portal_login_url()}")
        driver.get(portal_login_url())
        _wait_for_page(driver)

    if _page_looks_logged_in(driver):
        _SESSION_READY = True
        print("✅ [SMC] Already logged in.")
        return True

    # Always open Dealer Login form before checking credentials
    if not _on_dealer_login_page(driver):
        _go_to_dealer_login_page(driver)

    username, password = portal_credentials()
    if not username or not password:
        print(
            "⚠️ [SMC] No credentials found in .env — set SMC_PORTAL_USERNAME + SMC_PORTAL_PASSWORD "
            "(or USER_ID + PASSWORD). Dealer Login page is open for manual login."
        )
        return _page_looks_logged_in(driver)

    if _dealer_login(driver, username, password):
        _SESSION_READY = True
        return True

    print("⚠️ [SMC] Auto-login failed — complete login manually in Chrome.")
    return False


def _select_customer(driver) -> bool:
    hint = customer_search_hint()
    match_code = customer_select_match()

    field = _input_by_label(driver, "Customer Code/Name")
    if not field:
        print("⚠️ [SMC] Customer Code/Name field not found.")
        return False

    try:
        field.clear()
        field.send_keys(hint)
        time.sleep(1.5)
    except Exception as exc:
        print(f"⚠️ [SMC] Could not type customer hint: {exc}")
        return False

    # Autocomplete dropdown (Kendo / jQuery UI / plain list)
    option_xpaths = [
        f"//li[contains(., {json.dumps(match_code)})]",
        f"//*[contains(@class,'k-list')]//*[contains(., {json.dumps(match_code)})]",
        f"//ul[contains(@class,'ui-autocomplete')]//li[contains(., {json.dumps(match_code)})]",
        f"//*[contains(., 'ROBOMATICS') and contains(., {json.dumps(match_code)})]",
    ]
    for xpath in option_xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    el.click()
                    time.sleep(1)
                    return True
        except Exception:
            continue

    # Already selected from saved session?
    try:
        val = field.get_attribute("value") or ""
        if match_code in val.upper():
            return True
    except Exception:
        pass

    print(f"⚠️ [SMC] Could not select customer {match_code} from autocomplete.")
    return False


def _navigate_item_enquiry(driver) -> bool:
    url = str(driver.current_url or "").lower()
    if "/itemenquiry" in url:
        return True

    driver.get(portal_item_enquiry_url())
    time.sleep(2)
    if "/itemenquiry" in str(driver.current_url or "").lower():
        return True

    # Menu fallback: Enquiry → Item Inquiry
    try:
        for el in driver.find_elements(By.XPATH, "//a[contains(.,'Enquiry')]"):
            if el.is_displayed():
                el.click()
                time.sleep(0.8)
                break
        if _click_link_by_text(driver, "Item Inquiry") or _click_link_by_text(driver, "Item Enquiry"):
            time.sleep(2)
            return "/itemenquiry" in str(driver.current_url or "").lower()
    except Exception:
        pass
    return False


def _scrape_item_list(driver) -> list[dict[str, Any]]:
    try:
        rows = driver.execute_script(SCRAPE_ITEM_LIST_JS, [])
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:
        print(f"⚠️ [SMC] Item list scrape failed: {exc}")
    return []


def _run_item_enquiry(driver, part_no: str) -> list[dict[str, Any]]:
    if not _navigate_item_enquiry(driver):
        print("⚠️ [SMC] Could not open Item Enquiry page.")
        return []

    if not _select_customer(driver):
        return []

    part_field = _input_by_label(driver, "Part No.")
    if not part_field:
        print("⚠️ [SMC] Part No. field not found.")
        return []

    try:
        part_field.clear()
        part_field.send_keys(part_no)
    except Exception as exc:
        print(f"⚠️ [SMC] Could not enter part number: {exc}")
        return []

    enquire = _find_first(driver, [
        "//input[@value='Enquire']",
        "//button[contains(.,'Enquire')]",
        "//input[contains(@value,'Enquire')]",
    ])
    if not enquire:
        print("⚠️ [SMC] Enquire button not found.")
        return []

    enquire.click()
    time.sleep(3)
    return _scrape_item_list(driver)


def search_portal_part(driver, part_no: str, _depth: int = 0) -> dict[str, Any] | None:
    """Item Enquiry search → price, stock, lead time; handles obsolete replacement."""
    if _depth > 2:
        return None

    if not ensure_portal_session(driver):
        return None

    for key in smc_lookup_keys(part_no):
        print(f"   🔎 [SMC] Item Enquiry search: {key!r}")
        rows = _run_item_enquiry(driver, key)
        if not rows:
            continue

        hit = resolve_hit_from_rows(rows, key)
        if not hit:
            print(f"   ⚠️ [SMC] {len(rows)} row(s) returned but no match for {key!r}")
            continue

        replacement = str(hit.get("replacement_part") or "").strip()
        if replacement and not parts_equal(replacement, key):
            print(f"   🔄 [SMC] Obsolete — re-searching replacement {replacement!r}")
            repl_hit = search_portal_part(driver, replacement, _depth=_depth + 1)
            if repl_hit:
                repl_hit["replaced_obsolete_part"] = key
                repl_hit["desc"] = (
                    f"{repl_hit.get('desc', '')} "
                    f"(replaces obsolete {key})"
                ).strip()
                return repl_hit

        hit["searched_part"] = part_no
        return hit

    print(f"   ⚠️ [SMC] No Item Enquiry match for {part_no!r}")
    return None


def _load_cache() -> dict[str, Any]:
    if not os.path.exists(SMC_CACHE_FILE):
        return {}
    try:
        with open(SMC_CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        tmp = f"{SMC_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2)
        os.replace(tmp, SMC_CACHE_FILE)
    except Exception as exc:
        print(f"⚠️ [SMC] Could not save cache: {exc}")


def _cache_get(part_no: str) -> dict[str, Any] | None:
    cache = _load_cache()
    entry = cache.get(compact_part(part_no)) or cache.get(part_no.upper())
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(str(entry.get("cached_at") or ""))
        if datetime.now() - cached_at > timedelta(hours=cache_ttl_hours()):
            return None
    except Exception:
        return None
    return entry.get("data")


def _cache_put(part_no: str, data: dict[str, Any]) -> None:
    cache = _load_cache()
    key = compact_part(part_no) or part_no.upper()
    cache[key] = {"cached_at": datetime.now().isoformat(timespec="seconds"), "data": data}
    if len(cache) > 500:
        oldest = sorted(cache.items(), key=lambda kv: str((kv[1] or {}).get("cached_at") or ""))[:100]
        for old_key, _ in oldest:
            cache.pop(old_key, None)
    _save_cache(cache)


def lookup_smc_quote(
    part_no: str,
    qty: int = 1,
    markup_divisor: float = 0.72,
    search_context: str = "",
) -> dict[str, Any] | None:
    if not smc_portal_enabled():
        return None

    part_no = str(part_no or "").strip()
    if not part_no:
        return None

    cached = _cache_get(part_no)
    if cached:
        print(f"   ♻️ [SMC] Cache hit for {part_no!r}")
        return dict(cached)

    try:
        driver = get_driver()
        hit = search_portal_part(driver, part_no)
    except Exception as exc:
        print(f"❌ [SMC] Portal lookup failed for {part_no!r}: {exc}")
        return None

    if not hit:
        return None

    net_price = hit.get("net_price")
    lead_time = str(hit.get("lead_time") or LT_INDENT)
    requested_qty = max(1, int(qty))
    sell_price = None
    price_display = "[TBC]"
    if net_price is not None and markup_divisor > 0:
        sell_price = float(net_price) / markup_divisor
        price_display = f"{sell_price:,.2f}"

    matched_part = str(hit.get("part_no") or part_no).strip()
    desc = str(hit.get("desc") or f"SMC {matched_part}").strip()
    if not desc.upper().startswith("SMC"):
        desc = f"SMC {desc}"

    quote = {
        "desc": desc,
        "qty": requested_qty,
        "requested_qty": requested_qty,
        "moq": 0,
        "moq_applied": False,
        "net_price": net_price,
        "sell_price": sell_price,
        "price": price_display,
        "lt": lead_time,
        "smc_part": matched_part,
        "source": "SMC_PORTAL",
        "replaced_obsolete_part": hit.get("replaced_obsolete_part"),
    }
    _cache_put(part_no, quote)
    print(f"   ✅ [SMC] {matched_part}: RM {price_display} | LT {lead_time}")
    return quote
