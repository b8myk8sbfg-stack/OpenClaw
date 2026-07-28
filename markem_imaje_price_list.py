"""Markem-Imaje offline price list lookup — customer quote uses MI List Price (MYR), no markup."""

from __future__ import annotations

import os
import pickle
import re
import time
from typing import Any

import pandas as pd

DEFAULT_PRICE_LIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Markem-Imaje Price List 2026_Reseller_Robomatics SB 2.xlsx",
)
DEFAULT_SHEET = "2026 Price List"
HEADER_ROW = 5
CACHE_VERSION = 1
BRAND_NAME = "MARKEM-IMAJE"

_PRICE_LIST_LOADED = False
_LOOKUP_BY_KEY: dict[str, dict[str, Any]] = {}


def normalize_material_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _price_list_path() -> str:
    return os.getenv("MARKEM_IMAJE_PRICE_LIST_XLSX", DEFAULT_PRICE_LIST_PATH).strip()


def _default_lead_time() -> str:
    return os.getenv("OPENCLAW_MARKEM_DEFAULT_LT", "4-6 weeks").strip() or "4-6 weeks"


def looks_like_markem_imaje_material(part_no: str) -> bool:
    """True when a token looks like a Markem-Imaje material key (e.g. ENM10053306)."""
    compact = normalize_material_key(part_no)
    if not compact:
        return False
    if re.fullmatch(r"ENM\d{5,}", compact):
        return True
    if re.fullmatch(r"\d{7,8}", compact):
        return True
    return False


def is_markem_imaje_brand(brand: str) -> bool:
    brand_u = str(brand or "").upper().replace("_", " ").replace("-", " ").strip()
    return brand_u in {
        "MARKEM IMAJE",
        "MARKEM",
        "IMAJE",
        "MARKEMIMAJE",
    } or "MARKEM" in brand_u and "IMAJE" in brand_u


def material_lookup_keys(part_no: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    compact = normalize_material_key(part_no)
    add(compact)
    if compact.isdigit():
        add(compact.zfill(7))
        add(compact.zfill(8))
        stripped = compact.lstrip("0") or "0"
        add(stripped)
        add(stripped.zfill(7))
    return keys


def _cache_file(path: str) -> str:
    base = os.path.basename(path)
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f".openclaw_markem_imaje_cache_v{CACHE_VERSION}_{base}.pkl",
    )


def _load_from_cache(path: str) -> dict[str, dict[str, Any]] | None:
    cache_file = _cache_file(path)
    if not os.path.isfile(cache_file):
        return None
    try:
        if os.path.getmtime(cache_file) < os.path.getmtime(path):
            return None
        with open(cache_file, "rb") as handle:
            payload = pickle.load(handle)
        if payload.get("version") != CACHE_VERSION:
            return None
        return payload.get("lookup") or {}
    except Exception:
        return None


def _save_cache(path: str, lookup: dict[str, dict[str, Any]]) -> None:
    cache_file = _cache_file(path)
    try:
        with open(cache_file, "wb") as handle:
            pickle.dump({"version": CACHE_VERSION, "lookup": lookup}, handle)
        print(f"💾 [MARKEM-IMAJE] Saved lookup cache: {cache_file}")
    except Exception as exc:
        print(f"⚠️ [MARKEM-IMAJE] Cache write failed: {exc}")


def _parse_list_price(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"NAN", "NONE", "N/A", "-"}:
        return None
    try:
        amount = float(text)
        return amount if amount > 0 else None
    except ValueError:
        match = re.search(r"\d+(?:\.\d+)?", text)
        if match:
            amount = float(match.group(0))
            return amount if amount > 0 else None
    return None


def _resolve_columns(df: pd.DataFrame) -> tuple[str, str, str]:
    key_col = desc_col = list_col = ""
    for col in df.columns:
        label = str(col).replace("\n", " ").strip().lower()
        if "material (key" in label:
            key_col = col
        elif "material (description" in label:
            desc_col = col
        elif "list price" in label:
            list_col = col
    if not all([key_col, desc_col, list_col]):
        raise ValueError(f"Unexpected Markem-Imaje columns: {list(df.columns)}")
    return key_col, desc_col, list_col


def load_markem_imaje_price_list(force: bool = False) -> bool:
    global _PRICE_LIST_LOADED, _LOOKUP_BY_KEY

    if _PRICE_LIST_LOADED and not force:
        return bool(_LOOKUP_BY_KEY)

    path = _price_list_path()
    if not os.path.exists(path):
        print(f"⚠️ [MARKEM-IMAJE] Price list not found: {path}")
        _PRICE_LIST_LOADED = True
        _LOOKUP_BY_KEY = {}
        return False

    cached = None if force else _load_from_cache(path)
    if cached is not None:
        _LOOKUP_BY_KEY = cached
        _PRICE_LIST_LOADED = True
        print(f"✅ [MARKEM-IMAJE] Loaded cached index: {len(_LOOKUP_BY_KEY)} keys.")
        return bool(_LOOKUP_BY_KEY)

    print(f"📦 [MARKEM-IMAJE] Loading price list: {path}")
    started = time.time()
    try:
        df = pd.read_excel(
            path,
            sheet_name=os.getenv("MARKEM_IMAJE_PRICE_LIST_SHEET", DEFAULT_SHEET),
            header=HEADER_ROW,
            engine="openpyxl",
        )
        key_col, desc_col, list_col = _resolve_columns(df)
    except Exception as exc:
        print(f"❌ [MARKEM-IMAJE] Failed to read price list: {exc}")
        _PRICE_LIST_LOADED = True
        _LOOKUP_BY_KEY = {}
        return False

    lookup: dict[str, dict[str, Any]] = {}
    loaded_rows = 0

    for _, row in df.iterrows():
        material_key = str(row.get(key_col) or "").strip()
        if not material_key or material_key.upper() == "NAN":
            continue

        list_price = _parse_list_price(row.get(list_col))
        description = str(row.get(desc_col) or "").strip()
        product_range = str(row.get("Techno-Range-Product (Description)", "") or "").strip()

        entry = {
            "material_key": material_key.upper(),
            "description": description,
            "product_range": product_range,
            "list_price": list_price,
        }

        for key in material_lookup_keys(material_key):
            lookup[key] = entry
        loaded_rows += 1

    _LOOKUP_BY_KEY = lookup
    _PRICE_LIST_LOADED = True
    elapsed = time.time() - started
    print(
        f"✅ [MARKEM-IMAJE] Loaded {loaded_rows} rows, {len(lookup)} lookup keys in {elapsed:.1f}s."
    )
    _save_cache(path, lookup)
    return bool(lookup)


def lookup_markem_imaje_entry(part_no: str) -> dict[str, Any] | None:
    if not _PRICE_LIST_LOADED:
        load_markem_imaje_price_list()

    for key in material_lookup_keys(part_no):
        entry = _LOOKUP_BY_KEY.get(key)
        if entry:
            print(f"   ✅ [MARKEM-IMAJE] Matched {part_no!r} → {entry.get('material_key')}")
            return entry

    tried = ", ".join(material_lookup_keys(part_no)[:4])
    print(f"   ⚠️ [MARKEM-IMAJE] No price list match for {part_no!r} (tried: {tried})")
    return None


def lookup_markem_imaje_quote(
    part_no: str,
    qty: int = 1,
    search_context: str = "",
) -> dict[str, Any] | None:
    """Return quote dict using MI List Price directly — no 0.72 markup."""
    _ = search_context
    entry = lookup_markem_imaje_entry(part_no)
    if not entry:
        return None

    list_price = entry.get("list_price")
    quoted_qty = max(1, int(qty))
    price_display = "[TBC]"
    if list_price is not None:
        price_display = f"{float(list_price):,.2f}"

    material_key = str(entry.get("material_key") or part_no).upper()
    description = str(entry.get("description") or "").strip()
    desc = f"MARKEM-IMAJE {material_key}"
    if description and description.upper() not in desc.upper():
        desc = f"{desc} — {description}"

    customer_lt = _default_lead_time()
    return {
        "desc": desc,
        "qty": quoted_qty,
        "requested_qty": quoted_qty,
        "list_price": list_price,
        "price": price_display,
        "lt": customer_lt,
        "material_key": material_key,
        "source": "MARKEM_IMAJE_PRICE_LIST",
        "needs_supplier": price_display == "[TBC]" or customer_lt == "[TBC]",
    }
