"""
SMC Malaysia distributor portal lookup (my.smccorporation.co).

Uses a dedicated Chrome profile (separate from WhatsApp) so you can log in once
manually — including any Chrome extension SMC requires — and OpenClaw reuses that
session for price / lead-time lookups.

Configure via environment:
  SMC_PORTAL_ENABLED=1
  SMC_PORTAL_BASE_URL=https://my.smccorporation.co:8001
  SMC_PORTAL_HOME_URL=https://my.smccorporation.co:8001/Home
  OPENCLAW_BASE_DIR=/Users/evon/OpenClaw
  SMC_PORTAL_CHROME_PROFILE=.../chrome_smc_profile   (optional)
  SMC_PORTAL_CACHE_HOURS=24
  SMC_PORTAL_USERNAME / SMC_PORTAL_PASSWORD          (optional auto-login)
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
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = os.getenv("OPENCLAW_BASE_DIR", "/Users/evon/OpenClaw")
DEFAULT_BASE_URL = "https://my.smccorporation.co:8001"
DEFAULT_HOME_URL = f"{DEFAULT_BASE_URL}/Home"
DEFAULT_LOGIN_URL = f"{DEFAULT_BASE_URL}/Account/Index?ReturnUrl=%2fHome"

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
_DRIVER_LAST_USED = 0.0

PRICE_LABEL_RE = re.compile(
    r"(?:net\s*)?(?:unit\s*)?price|amount|selling|list\s*price|rm\b",
    re.I,
)
LEAD_TIME_LABEL_RE = re.compile(
    r"lead\s*time|delivery|lt\b|availability|stock\s*status|eta",
    re.I,
)
MOQ_LABEL_RE = re.compile(r"moq|min(?:imum)?\s*(?:order)?\s*qty", re.I)
MONEY_RE = re.compile(r"(?:RM\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)", re.I)


def smc_portal_enabled() -> bool:
    return os.getenv("SMC_PORTAL_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def portal_base_url() -> str:
    return os.getenv("SMC_PORTAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def portal_home_url() -> str:
    return os.getenv("SMC_PORTAL_HOME_URL", f"{portal_base_url()}/Home")


def portal_login_url() -> str:
    return os.getenv("SMC_PORTAL_LOGIN_URL", DEFAULT_LOGIN_URL)


def cache_ttl_hours() -> float:
    try:
        return float(os.getenv("SMC_PORTAL_CACHE_HOURS", "24"))
    except ValueError:
        return 24.0


def normalize_smc_part(part: str) -> str:
    """Normalize SMC model numbers for portal search."""
    raw = str(part or "").strip().upper()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    if re.match(r"^\d{1,2}[A-Z]", compact) and "-" not in compact:
        # 10KQ2H06M5N → 10-KQ2H06-M5N (common SMC catalogue form)
        m = re.match(r"^(\d{1,2})([A-Z].+)$", compact)
        if m:
            body = m.group(2)
            if len(body) > 6 and "-" not in body:
                return f"{m.group(1)}-{body[:6]}-{body[6:]}"
            return f"{m.group(1)}-{body}"
    return raw


def smc_lookup_keys(part_no: str) -> list[str]:
    """Return part-number variants to try on the portal."""
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
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    add(compact)
    if compact.startswith("10") and len(compact) > 2:
        add(f"10-{compact[2:]}")
    return keys


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


def load_portal_config() -> dict[str, Any]:
    if not os.path.exists(SMC_CONFIG_FILE):
        return {}
    try:
        with open(SMC_CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️ [SMC] Could not read {SMC_CONFIG_FILE}: {exc}")
        return {}


def _selector_list(config: dict[str, Any], key: str, defaults: list[str]) -> list[str]:
    custom = config.get(key)
    if isinstance(custom, str) and custom.strip():
        return [custom.strip()]
    if isinstance(custom, list):
        return [str(x).strip() for x in custom if str(x).strip()]
    return list(defaults)


DEFAULT_CONFIG: dict[str, list[str]] = {
    "login_email": ["#Email", "input[name='Email']", "input[type='email']"],
    "login_password": ["#Password", "input[name='Password']", "input[type='password']"],
    "login_submit": [
        "button[type='submit']",
        "input[type='submit']",
        "button.btn-primary",
        ".btn-login",
    ],
    "search_input": [
        "input[name*='model' i]",
        "input[name*='part' i]",
        "input[name*='search' i]",
        "input[placeholder*='model' i]",
        "input[placeholder*='part' i]",
        "input[placeholder*='search' i]",
        "#txtSearch",
        "#search",
        "#Search",
        "input[type='search']",
    ],
    "search_button": [
        "button[type='submit']",
        "input[type='submit']",
        "button.btn-search",
        ".btn-search",
    ],
    "search_url_templates": [
        "{base}/Home/Search?model={part}",
        "{base}/Product/Search?model={part}",
        "{base}/Catalog/Search?partNo={part}",
        "{base}/Home/ProductSearch?query={part}",
    ],
    "result_table": ["table", ".table", "[role='grid']", ".search-results"],
}


def get_driver(force_new: bool = False) -> webdriver.Chrome:
    """Return a warm SMC portal Chrome session (separate profile from WhatsApp)."""
    global _DRIVER, _DRIVER_LAST_USED

    if _DRIVER is not None and not force_new:
        try:
            _ = _DRIVER.current_url
            _DRIVER_LAST_USED = time.time()
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
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.page_load_strategy = "eager"

    if os.getenv("SMC_PORTAL_HEADLESS", "0").strip().lower() in ("1", "true", "yes"):
        options.add_argument("--headless=new")

    _DRIVER = webdriver.Chrome(options=options)
    _DRIVER_LAST_USED = time.time()
    print(f"✅ [SMC] Chrome ready (profile: {SMC_CHROME_PROFILE})")
    return _DRIVER


def close_driver() -> None:
    global _DRIVER
    if _DRIVER is not None:
        try:
            _DRIVER.quit()
        except Exception:
            pass
        _DRIVER = None


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


def _page_looks_logged_in(driver) -> bool:
    url = str(driver.current_url or "").lower()
    if "/account/index" in url or "/account/login" in url:
        return False

    login_markers = [
        "input[type='password']",
        "#Password",
        "input[name='Password']",
    ]
    for selector in login_markers:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed():
                    return False
        except Exception:
            continue

    home_markers = [
        "#side",
        "[data-testid='chat-list']",
        "nav",
        ".navbar",
        "#main",
        "header",
    ]
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        if "sign in" in body and "password" in body and len(body) < 2000:
            return False
    except Exception:
        pass

    for selector in home_markers:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            continue

    return "/home" in url or url.rstrip("/") == portal_base_url().lower()


def ensure_portal_session(driver, force_reload: bool = False) -> bool:
    """Navigate to SMC portal and confirm we are past the login page."""
    config = load_portal_config()
    target = portal_home_url() if not force_reload else portal_login_url()

    try:
        current = str(driver.current_url or "")
    except Exception:
        current = ""

    if force_reload or portal_base_url().lower() not in current.lower():
        print(f"🌐 [SMC] Opening portal: {target}")
        driver.get(target)
        time.sleep(2)

    if _page_looks_logged_in(driver):
        return True

    username = os.getenv("SMC_PORTAL_USERNAME", "").strip()
    password = os.getenv("SMC_PORTAL_PASSWORD", "").strip()
    if username and password:
        print("🔐 [SMC] Attempting portal login with SMC_PORTAL_USERNAME...")
        if _attempt_login(driver, config, username, password):
            time.sleep(2)
            if _page_looks_logged_in(driver):
                print("✅ [SMC] Portal login succeeded.")
                return True

    print(
        "⚠️ [SMC] Portal not logged in. Run once on your Mac:\n"
        "   uv run python scripts/smc_portal_login.py\n"
        "   Log in manually in the Chrome window (extension OK). Session is saved in chrome_smc_profile."
    )
    return False


def _attempt_login(driver, config: dict[str, Any], username: str, password: str) -> bool:
    login_url = portal_login_url()
    if login_url not in str(driver.current_url or ""):
        driver.get(login_url)
        time.sleep(1.5)

    email_el = _find_first(
        driver,
        _selector_list(config, "login_email", DEFAULT_CONFIG["login_email"]),
    )
    pass_el = _find_first(
        driver,
        _selector_list(config, "login_password", DEFAULT_CONFIG["login_password"]),
    )
    if not email_el or not pass_el:
        return False

    email_el.clear()
    email_el.send_keys(username)
    pass_el.clear()
    pass_el.send_keys(password)

    submit = _find_first(
        driver,
        _selector_list(config, "login_submit", DEFAULT_CONFIG["login_submit"]),
    )
    if submit:
        submit.click()
    else:
        pass_el.send_keys(Keys.RETURN)
    time.sleep(2)
    return True


def _parse_money(text: str) -> float | None:
    match = MONEY_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_table_result(driver, part_no: str) -> dict[str, Any] | None:
    config = load_portal_config()
    tables = []
    for selector in _selector_list(config, "result_table", DEFAULT_CONFIG["result_table"]):
        try:
            tables.extend(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            continue

    part_keys = {normalize_smc_part(part_no), re.sub(r"[^A-Z0-9]", "", part_no.upper())}
    best: dict[str, Any] | None = None

    for table in tables:
        try:
            rows = table.find_elements(By.CSS_SELECTOR, "tr")
        except Exception:
            continue

        for row in rows:
            cells = []
            try:
                cells = [c.text.strip() for c in row.find_elements(By.CSS_SELECTOR, "td, th")]
            except Exception:
                continue
            if len(cells) < 2:
                continue

            row_text = " | ".join(cells)
            row_compact = re.sub(r"[^A-Z0-9]", "", row_text.upper())
            if not any(key and key in row_compact for key in part_keys if key):
                if part_no.upper() not in row_text.upper():
                    continue

            parsed: dict[str, Any] = {
                "part_no": part_no,
                "desc": cells[1] if len(cells) > 1 else row_text[:120],
                "raw_row": row_text[:300],
            }

            for idx, cell in enumerate(cells):
                label = cell.lower()
                value = cells[idx + 1] if idx + 1 < len(cells) else cell
                if PRICE_LABEL_RE.search(label):
                    parsed["net_price"] = _parse_money(value)
                elif LEAD_TIME_LABEL_RE.search(label):
                    parsed["lead_time"] = value.strip()
                elif MOQ_LABEL_RE.search(label):
                    parsed["moq"] = _parse_moq(value)

            if parsed.get("net_price") is None:
                for cell in cells:
                    money = _parse_money(cell)
                    if money is not None and money > 0:
                        parsed["net_price"] = money
                        break

            if parsed.get("net_price") is not None or parsed.get("lead_time"):
                best = parsed
                break
        if best:
            break

    return best


def _parse_moq(value: str) -> int:
    match = re.search(r"\d+", str(value or ""))
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def _scrape_detail_page(driver) -> dict[str, Any] | None:
    """Fallback: scan visible page text for price / lead-time labels."""
    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return None

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    result: dict[str, Any] = {}

    for idx, line in enumerate(lines):
        lower = line.lower()
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if PRICE_LABEL_RE.search(lower):
            money = _parse_money(nxt or line)
            if money is not None:
                result["net_price"] = money
        if LEAD_TIME_LABEL_RE.search(lower) and nxt:
            result["lead_time"] = nxt
        if MOQ_LABEL_RE.search(lower) and nxt:
            result["moq"] = _parse_moq(nxt)

    if result.get("net_price") is not None or result.get("lead_time"):
        return result
    return None


def _try_direct_search_url(driver, part_no: str) -> bool:
    config = load_portal_config()
    templates = _selector_list(
        config,
        "search_url_templates",
        DEFAULT_CONFIG["search_url_templates"],
    )
    base = portal_base_url()
    encoded = part_no.replace(" ", "%20")

    for template in templates:
        url = template.format(base=base, part=encoded)
        try:
            print(f"   🔎 [SMC] Trying URL: {url}")
            driver.get(url)
            time.sleep(2)
            if _parse_table_result(driver, part_no) or _scrape_detail_page(driver):
                return True
        except Exception as exc:
            print(f"   ⚠️ [SMC] URL search failed: {exc}")
    return False


def _try_form_search(driver, part_no: str) -> bool:
    config = load_portal_config()
    search_input = _find_first(
        driver,
        _selector_list(config, "search_input", DEFAULT_CONFIG["search_input"]),
    )
    if not search_input:
        return False

    try:
        search_input.clear()
        search_input.send_keys(part_no)
        search_btn = _find_first(
            driver,
            _selector_list(config, "search_button", DEFAULT_CONFIG["search_button"]),
        )
        if search_btn:
            search_btn.click()
        else:
            search_input.send_keys(Keys.RETURN)
        time.sleep(2)
        return True
    except Exception as exc:
        print(f"   ⚠️ [SMC] Form search failed: {exc}")
        return False


def search_portal_part(driver, part_no: str) -> dict[str, Any] | None:
    """Search SMC portal for a part and return scraped price / lead-time fields."""
    if not ensure_portal_session(driver):
        return None

    if portal_home_url() not in str(driver.current_url or ""):
        driver.get(portal_home_url())
        time.sleep(1.5)

    for key in smc_lookup_keys(part_no):
        print(f"   🔎 [SMC] Searching portal for {key!r}")

        if _try_form_search(driver, key):
            hit = _parse_table_result(driver, key) or _scrape_detail_page(driver)
            if hit:
                hit["part_no"] = key
                return hit

        if _try_direct_search_url(driver, key):
            hit = _parse_table_result(driver, key) or _scrape_detail_page(driver)
            if hit:
                hit["part_no"] = key
                return hit

    print(f"   ⚠️ [SMC] No portal match for {part_no!r}")
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
    entry = cache.get(normalize_smc_part(part_no)) or cache.get(part_no.upper())
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
    key = normalize_smc_part(part_no) or part_no.upper()
    cache[key] = {"cached_at": datetime.now().isoformat(timespec="seconds"), "data": data}
    # Keep cache bounded
    if len(cache) > 500:
        oldest = sorted(
            cache.items(),
            key=lambda kv: str((kv[1] or {}).get("cached_at") or ""),
        )[:100]
        for old_key, _ in oldest:
            cache.pop(old_key, None)
    _save_cache(cache)


def lookup_smc_quote(
    part_no: str,
    qty: int = 1,
    markup_divisor: float = 0.72,
    search_context: str = "",
) -> dict[str, Any] | None:
    """
    Look up SMC part price and lead time from the distributor web portal.

    Returns quote dict compatible with openclaw_inquiry_engine (desc, price, lt, moq, ...).
    """
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
    lead_time = str(hit.get("lead_time") or "[TBC]").strip() or "[TBC]"
    moq = int(hit.get("moq") or 0)
    requested_qty = max(1, int(qty))
    quoted_qty = max(requested_qty, moq) if moq > 1 else requested_qty
    moq_applied = quoted_qty > requested_qty

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
        "qty": quoted_qty,
        "requested_qty": requested_qty,
        "moq": moq,
        "moq_applied": moq_applied,
        "net_price": net_price,
        "sell_price": sell_price,
        "price": price_display,
        "lt": lead_time,
        "smc_part": matched_part,
        "source": "SMC_PORTAL",
    }
    _cache_put(part_no, quote)
    print(
        f"   ✅ [SMC] Portal match for {matched_part}: "
        f"RM {price_display} | LT {lead_time}"
        f"{f' | MOQ {moq}' if moq > 1 else ''}"
    )
    return quote
