"""Burkert offline price list lookup and ex-factory → customer lead time mapping."""

from __future__ import annotations

import os
import re
from typing import Any

import pandas as pd

# Column indices in the XLSM (header row 18, data from row 19).
COL_ID = 0
COL_TYPE = 1
COL_DESC = 2
COL_NET_PRICE = 10
COL_LEAD_TIME = 11
DATA_START_ROW = 19

DEFAULT_PRICE_LIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Price List 220015_MYR_15.01.2026-3 (Level 3) Robomatics.xlsm",
)

# Ex-factory working days (Burkert) → customer lead time (Germany → Malaysia transfer).
FACTORY_DAY_BUCKETS: list[tuple[int, int, str]] = [
    (3, 5, "4-5 weeks"),
    (6, 10, "5-6 weeks"),
    (11, 20, "6-8 weeks"),
    (21, 50, "8-10 weeks"),
    (51, 100, "10-14 weeks"),
]

_PRICE_LIST_LOADED = False
_LOOKUP_BY_KEY: dict[str, dict[str, Any]] = {}


def normalize_part(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def parse_factory_days(value: Any) -> tuple[int | None, int | None]:
    """Parse Burkert 'Estimate lead time (working days)' into (min_days, max_days)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        days = int(round(float(value)))
        return days, days

    text = str(value).strip().lower()
    if not text or text in {"nan", "none", "tbc", "n/a", "-"}:
        return None, None

    numbers = [int(float(n)) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers), max(numbers)


def factory_days_to_customer_lead_time(
    min_days: int | None,
    max_days: int | None = None,
) -> str:
    """
    Map Burkert ex-factory working days to customer-facing lead time in weeks.

    Uses the upper bound of the factory range so cross-bucket ranges stay conservative.
    """
    if min_days is None and max_days is None:
        return "[TBC]"

    effective_max = max_days if max_days is not None else min_days
    if effective_max is None:
        return "[TBC]"

    if effective_max < 3:
        return FACTORY_DAY_BUCKETS[0][2]

    for low, high, customer_lt in FACTORY_DAY_BUCKETS:
        if low <= effective_max <= high:
            return customer_lt

    if effective_max > 100:
        return FACTORY_DAY_BUCKETS[-1][2]

    return "[TBC]"


def customer_lead_time_from_field(factory_lead_time_value: Any) -> str:
    """Parse a price-list lead-time cell and return the customer-facing lead time."""
    min_days, max_days = parse_factory_days(factory_lead_time_value)
    return factory_days_to_customer_lead_time(min_days, max_days)


def _price_list_path() -> str:
    return os.getenv("BURKERT_PRICE_LIST_XLSM", DEFAULT_PRICE_LIST_PATH).strip()


def _parse_net_price(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        price = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def load_burkert_price_list(force: bool = False) -> bool:
    """Load the Burkert XLSM into memory. Returns True when the index is ready."""
    global _PRICE_LIST_LOADED, _LOOKUP_BY_KEY

    if _PRICE_LIST_LOADED and not force:
        return bool(_LOOKUP_BY_KEY)

    path = _price_list_path()
    if not os.path.exists(path):
        print(f"⚠️ [BURKERT] Price list not found: {path}")
        _PRICE_LIST_LOADED = True
        _LOOKUP_BY_KEY = {}
        return False

    print(f"📦 [BURKERT] Loading price list: {path}")
    df = pd.read_excel(path, sheet_name=0, header=None)

    lookup: dict[str, dict[str, Any]] = {}
    loaded_rows = 0

    for row_idx in range(DATA_START_ROW, len(df)):
        row = df.iloc[row_idx]
        burkert_id = str(row.iloc[COL_ID]).strip() if pd.notna(row.iloc[COL_ID]) else ""
        part_type = str(row.iloc[COL_TYPE]).strip() if pd.notna(row.iloc[COL_TYPE]) else ""
        description = str(row.iloc[COL_DESC]).strip() if pd.notna(row.iloc[COL_DESC]) else ""

        if not any([burkert_id, part_type, description]):
            continue

        net_price = _parse_net_price(row.iloc[COL_NET_PRICE] if len(row) > COL_NET_PRICE else None)
        factory_lt = row.iloc[COL_LEAD_TIME] if len(row) > COL_LEAD_TIME else None
        customer_lt = customer_lead_time_from_field(factory_lt)

        entry = {
            "burkert_id": burkert_id,
            "type": part_type,
            "description": description,
            "net_price": net_price,
            "factory_lead_time": factory_lt,
            "customer_lead_time": customer_lt,
        }

        for key in {burkert_id, part_type, description}:
            norm = normalize_part(key)
            if len(norm) < 3:
                continue
            existing = lookup.get(norm)
            if existing is None:
                lookup[norm] = entry
            elif existing.get("net_price") is None and net_price is not None:
                lookup[norm] = entry

        loaded_rows += 1

    _LOOKUP_BY_KEY = lookup
    _PRICE_LIST_LOADED = True
    print(f"✅ [BURKERT] Loaded {loaded_rows} rows, {len(lookup)} lookup keys.")
    return bool(lookup)


def _lookup_entry(part_no: str) -> dict[str, Any] | None:
    if not _PRICE_LIST_LOADED:
        load_burkert_price_list()

    norm = normalize_part(part_no)
    if not norm:
        return None

    if norm in _LOOKUP_BY_KEY:
        return _LOOKUP_BY_KEY[norm]

    # Prefix match for catalog types like 0124-A-03,0-AA-PP-GM82-024/DC-08
    best: dict[str, Any] | None = None
    best_len = 0
    for key, entry in _LOOKUP_BY_KEY.items():
        if len(key) < 5:
            continue
        if norm.startswith(key) or key.startswith(norm):
            match_len = min(len(norm), len(key))
            if match_len > best_len:
                best = entry
                best_len = match_len

    return best


def lookup_burkert_quote(part_no: str, qty: int = 1, markup_divisor: float = 0.72) -> dict[str, Any] | None:
    """
    Return a quote dict for a Burkert part from the offline price list.

    Keys: desc, net_price, sell_price, price (formatted), lt, burkert_id, type, source.
    """
    entry = _lookup_entry(part_no)
    if not entry:
        return None

    net_price = entry.get("net_price")
    customer_lt = entry.get("customer_lead_time") or "[TBC]"

    sell_price = None
    price_display = "[TBC]"
    if net_price is not None and markup_divisor > 0:
        sell_price = net_price / markup_divisor
        price_display = f"{sell_price:,.2f}"

    part_type = str(entry.get("type") or "").strip()
    description = str(entry.get("description") or "").strip()
    desc = f"BURKERT {part_type}".strip()
    if description and description.upper() not in desc.upper():
        desc = f"{desc} — {description}"

    return {
        "desc": desc,
        "qty": int(qty),
        "net_price": net_price,
        "sell_price": sell_price,
        "price": price_display,
        "lt": customer_lt,
        "burkert_id": entry.get("burkert_id"),
        "type": part_type,
        "factory_lead_time": entry.get("factory_lead_time"),
        "source": "BURKERT_PRICE_LIST",
    }
