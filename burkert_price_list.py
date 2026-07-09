"""Burkert offline price list lookup and ex-factory → customer lead time mapping."""

from __future__ import annotations

import os
import pickle
import re
import time
from typing import Any

import pandas as pd

# Column indices in the XLSM (header row 18, data from row 19).
COL_ID = 0
COL_TYPE = 1
COL_DESC = 2
COL_MOQ = 6
COL_NET_PRICE = 10
COL_LEAD_TIME = 11
DATA_START_ROW = 19
CACHE_VERSION = 2

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
_FAMILY_INDEX: dict[str, list[dict[str, Any]]] = {}
_ID_INDEX: dict[str, dict[str, Any]] = {}


def normalize_part(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def normalize_burkert_id(value: str) -> str:
    """Strip non-digits and leading zeros: 00132465 → 132465."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return ""
    return digits.lstrip("0") or "0"


def burkert_id_lookup_keys(value: str) -> list[str]:
    """All ID forms to try: with/without leading zeros."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return []

    stripped = digits.lstrip("0") or "0"
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    add(stripped)
    add(digits)
    if len(digits) < 8:
        add(digits.zfill(6))
        add(digits.zfill(8))
    if stripped != digits:
        add(stripped.zfill(6))
        add(stripped.zfill(8))
    return keys


def extract_burkert_id_from_text(text: str) -> str:
    """Pull a Burkert article ID from label text or Copilot specs."""
    blob = str(text or "").strip()
    if not blob:
        return ""

    patterns = (
        r"\b(?:ID|ARTICLE|ART\.?|ARTICLE\s+NO\.?)\s*[:#]?\s*(0*\d{5,9})\b",
        r"\b(0\d{5,8})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return match.group(1)
    return ""


def format_burkert_id_display(value: str) -> str:
    """Format Burkert article ID for customer-facing quotes (e.g. 132465 → 00132465)."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return ""
    if len(digits) <= 8:
        return digits.zfill(8)
    return digits


def resolve_burkert_id(
    burkert_id: str = "",
    technical_specs: list | None = None,
    search_context: str = "",
) -> str:
    """Prefer explicit burkert_id, else scan specs/context for label ID."""
    explicit = str(burkert_id or "").strip()
    if explicit:
        return explicit

    specs = technical_specs or []
    if isinstance(specs, str):
        specs = [specs]
    for spec in specs:
        found = extract_burkert_id_from_text(spec)
        if found:
            return found

    return extract_burkert_id_from_text(search_context)


def burkert_type_family_key(part_type: str) -> str:
    """6519-H08,0-GM82-B5-024/DC-02 → 6519H08"""
    head = str(part_type or "").split(",")[0].strip()
    return normalize_part(head)


def burkert_lookup_keys(part_no: str) -> list[str]:
    """
    Build normalized lookup keys from a customer part / nameplate.

    Nameplates often read '6519 H 8.0' while the catalog type is '6519-H08,...'.
    """
    text = str(part_no or "").upper().strip()
    keys: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        norm = normalize_part(value)
        if norm and norm not in seen:
            seen.add(norm)
            keys.append(norm)

    add(text)

    for letter in ("H", "W"):
        match = re.search(rf"(\d{{4}})\s*{letter}\s*(\d+)\s*[.,]\s*(\d+)", text)
        if match:
            series, major, minor = match.groups()
            add(f"{series}-{letter}{int(major):02d}")
            add(f"{series}{letter}{int(major):02d}")
            add(f"{series}-{letter}{major}{minor}")
            add(f"{series}{letter}{major}{minor}")

        short = re.search(rf"(\d{{4}})\s*{letter}\s*(\d+)", text)
        if short:
            series, major = short.groups()
            add(f"{series}-{letter}{int(major):02d}")
            add(f"{series}{letter}{int(major):02d}")

    return keys


def _voltage_tokens_from_context(search_context: str) -> list[str]:
    text = re.sub(r"\s+", "", str(search_context or "").upper())
    tokens: list[str] = []

    if re.search(r"24VDC|DC24|024/DC|024DC", text):
        tokens.extend(["024DC", "024/DC", "024"])
    if re.search(r"110VAC|AC110|110/50|110/56|110/60", text):
        tokens.extend(["110/50", "110/56", "110/60", "110"])
    if re.search(r"120VAC|AC120|120/60", text):
        tokens.extend(["120/60", "120"])
    if re.search(r"230VAC|AC230|230/50|230/56|230/60", text):
        tokens.extend(["230/50", "230/56", "230/60", "230"])
    if re.search(r"240VAC|AC240|240/50", text):
        tokens.extend(["240/50", "240"])

    return tokens


def _score_entry_for_context(entry: dict[str, Any], search_context: str) -> int:
    part_type_norm = normalize_part(entry.get("type") or "")
    if not part_type_norm:
        return 0

    score = 0
    voltage_tokens = _voltage_tokens_from_context(search_context)
    for token in voltage_tokens:
        token_norm = normalize_part(token)
        if token_norm and token_norm in part_type_norm:
            score += 100

    if entry.get("net_price") is not None:
        score += 10

    return score


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


def _cache_path(path: str) -> str:
    return f"{path}.openclaw_burkert_cache.pkl"


def _parse_net_price(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        price = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _parse_moq(value: Any) -> int:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    try:
        moq = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, moq)


def apply_moq_to_qty(requested_qty: int, moq: int) -> tuple[int, bool]:
    """Return (quoted_qty, moq_applied). Bump qty up to MOQ when customer qty is lower."""
    requested = max(1, int(requested_qty or 1))
    minimum = max(0, int(moq or 0))
    if minimum > 1 and requested < minimum:
        return minimum, True
    return requested, False


def _register_entry(
    lookup: dict[str, dict[str, Any]],
    family_index: dict[str, list],
    id_index: dict[str, dict[str, Any]],
    entry: dict[str, Any],
) -> None:
    part_type = str(entry.get("type") or "").strip()
    family = burkert_type_family_key(part_type)
    if family:
        family_index.setdefault(family, []).append(entry)

    burkert_id = str(entry.get("burkert_id") or "").strip()
    for id_key in burkert_id_lookup_keys(burkert_id):
        existing = id_index.get(id_key)
        if existing is None:
            id_index[id_key] = entry
        elif existing.get("net_price") is None and entry.get("net_price") is not None:
            id_index[id_key] = entry
        existing_lookup = lookup.get(id_key)
        if existing_lookup is None:
            lookup[id_key] = entry
        elif existing_lookup.get("net_price") is None and entry.get("net_price") is not None:
            lookup[id_key] = entry

    for key in {burkert_id, part_type, entry.get("description")}:
        norm = normalize_part(str(key or ""))
        if len(norm) < 3:
            continue
        existing = lookup.get(norm)
        if existing is None:
            lookup[norm] = entry
        elif existing.get("net_price") is None and entry.get("net_price") is not None:
            lookup[norm] = entry


def _load_from_cache(path: str) -> tuple[dict[str, dict[str, Any]], dict[str, list], dict[str, dict[str, Any]]] | None:
    cache_file = _cache_path(path)
    if not os.path.exists(cache_file):
        return None
    try:
        if os.path.getmtime(cache_file) < os.path.getmtime(path):
            return None
        with open(cache_file, "rb") as handle:
            payload = pickle.load(handle)
        lookup = payload.get("lookup") or {}
        family_index = payload.get("family_index") or {}
        id_index = payload.get("id_index") or {}
        if payload.get("version") != CACHE_VERSION:
            return None
        if lookup and id_index:
            return lookup, family_index, id_index
    except Exception as exc:
        print(f"⚠️ [BURKERT] Cache read failed: {exc}")
    return None


def _save_cache(
    path: str,
    lookup: dict[str, dict[str, Any]],
    family_index: dict[str, list],
    id_index: dict[str, dict[str, Any]],
) -> None:
    cache_file = _cache_path(path)
    try:
        with open(cache_file, "wb") as handle:
            pickle.dump(
                {
                    "version": CACHE_VERSION,
                    "lookup": lookup,
                    "family_index": family_index,
                    "id_index": id_index,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        print(f"💾 [BURKERT] Saved lookup cache: {cache_file}")
    except Exception as exc:
        print(f"⚠️ [BURKERT] Cache write failed: {exc}")


def load_burkert_price_list(force: bool = False) -> bool:
    """Load the Burkert XLSM into memory. Returns True when the index is ready."""
    global _PRICE_LIST_LOADED, _LOOKUP_BY_KEY, _FAMILY_INDEX, _ID_INDEX

    if _PRICE_LIST_LOADED and not force:
        return bool(_LOOKUP_BY_KEY)

    path = _price_list_path()
    if not os.path.exists(path):
        print(f"⚠️ [BURKERT] Price list not found: {path}")
        _PRICE_LIST_LOADED = True
        _LOOKUP_BY_KEY = {}
        _FAMILY_INDEX = {}
        _ID_INDEX = {}
        return False

    cached = None if force else _load_from_cache(path)
    if cached:
        _LOOKUP_BY_KEY, _FAMILY_INDEX, _ID_INDEX = cached
        _PRICE_LIST_LOADED = True
        print(
            f"✅ [BURKERT] Loaded cached index: {len(_LOOKUP_BY_KEY)} keys, "
            f"{len(_FAMILY_INDEX)} families, {len(_ID_INDEX)} IDs."
        )
        return True

    print(f"📦 [BURKERT] Loading price list: {path}")
    started = time.time()
    try:
        df = pd.read_excel(
            path,
            sheet_name=0,
            header=None,
            usecols=[COL_ID, COL_TYPE, COL_DESC, COL_MOQ, COL_NET_PRICE, COL_LEAD_TIME],
            skiprows=DATA_START_ROW,
            engine="openpyxl",
        )
    except Exception as exc:
        print(f"❌ [BURKERT] Failed to read price list: {exc}")
        _PRICE_LIST_LOADED = True
        _LOOKUP_BY_KEY = {}
        _FAMILY_INDEX = {}
        _ID_INDEX = {}
        return False

    lookup: dict[str, dict[str, Any]] = {}
    family_index: dict[str, list[dict[str, Any]]] = {}
    id_index: dict[str, dict[str, Any]] = {}
    loaded_rows = 0

    for _, row in df.iterrows():
        burkert_id = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        part_type = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        description = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""

        if not any([burkert_id, part_type, description]):
            continue

        net_price = _parse_net_price(row.iloc[4] if len(row) > 4 else None)
        factory_lt = row.iloc[5] if len(row) > 5 else None
        customer_lt = customer_lead_time_from_field(factory_lt)
        moq = _parse_moq(row.iloc[3] if len(row) > 3 else None)

        entry = {
            "burkert_id": burkert_id,
            "type": part_type,
            "description": description,
            "net_price": net_price,
            "moq": moq,
            "factory_lead_time": factory_lt,
            "customer_lead_time": customer_lt,
        }
        _register_entry(lookup, family_index, id_index, entry)
        loaded_rows += 1

    _LOOKUP_BY_KEY = lookup
    _FAMILY_INDEX = family_index
    _ID_INDEX = id_index
    _PRICE_LIST_LOADED = True
    elapsed = time.time() - started
    print(
        f"✅ [BURKERT] Loaded {loaded_rows} rows, {len(lookup)} lookup keys, "
        f"{len(family_index)} families, {len(id_index)} IDs in {elapsed:.1f}s."
    )
    _save_cache(path, lookup, family_index, id_index)
    return bool(lookup)


def _collect_candidate_entries(part_no: str) -> list[dict[str, Any]]:
    keys = burkert_lookup_keys(part_no)
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_entry(entry: dict[str, Any] | None) -> None:
        if not entry:
            return
        entry_id = str(entry.get("burkert_id") or entry.get("type") or "")
        if entry_id in seen_ids:
            return
        seen_ids.add(entry_id)
        candidates.append(entry)

    for key in keys:
        add_entry(_LOOKUP_BY_KEY.get(key))
        for entry in _FAMILY_INDEX.get(key, []):
            add_entry(entry)
        if len(key) >= 5:
            for family_key, entries in _FAMILY_INDEX.items():
                if family_key.startswith(key):
                    for entry in entries:
                        add_entry(entry)

    if candidates:
        return candidates

    norm = normalize_part(part_no)
    if len(norm) >= 4:
        for family_key, entries in _FAMILY_INDEX.items():
            if family_key.startswith(norm[:4]):
                for entry in entries:
                    add_entry(entry)

    return candidates


def _pick_best_entry(candidates: list[dict[str, Any]], search_context: str = "") -> dict[str, Any] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    scored = sorted(
        candidates,
        key=lambda entry: (
            _score_entry_for_context(entry, search_context),
            len(normalize_part(entry.get("type") or "")),
        ),
        reverse=True,
    )
    best_score = _score_entry_for_context(scored[0], search_context)
    if best_score > 0:
        return scored[0]

    priced = [entry for entry in scored if entry.get("net_price") is not None]
    return priced[0] if priced else scored[0]


def _lookup_entry_by_id(burkert_id: str) -> dict[str, Any] | None:
    if not burkert_id:
        return None
    for key in burkert_id_lookup_keys(burkert_id):
        entry = _ID_INDEX.get(key) or _LOOKUP_BY_KEY.get(key)
        if entry:
            return entry
    return None


def _lookup_entry(
    part_no: str,
    search_context: str = "",
    burkert_id: str = "",
    technical_specs: list | None = None,
) -> dict[str, Any] | None:
    if not _PRICE_LIST_LOADED:
        load_burkert_price_list()

    resolved_id = resolve_burkert_id(
        burkert_id=burkert_id,
        technical_specs=technical_specs,
        search_context=search_context,
    )
    if resolved_id:
        entry = _lookup_entry_by_id(resolved_id)
        if entry:
            print(f"   ✅ [BURKERT] Matched by ID {resolved_id} → type {entry.get('type')}")
            return entry

    candidates = _collect_candidate_entries(part_no)
    entry = _pick_best_entry(candidates, search_context=search_context)
    if entry:
        return entry

    tried_id = normalize_burkert_id(resolved_id) if resolved_id else ""
    tried_keys = ", ".join(burkert_lookup_keys(part_no)[:4])
    print(
        f"   ⚠️ [BURKERT] No price list match for {part_no!r}"
        f"{f' / ID {tried_id}' if tried_id else ''} (tried: {tried_keys})"
    )
    return None


def lookup_burkert_quote(
    part_no: str,
    qty: int = 1,
    markup_divisor: float = 0.72,
    search_context: str = "",
    burkert_id: str = "",
    technical_specs: list | None = None,
) -> dict[str, Any] | None:
    """
    Return a quote dict for a Burkert part from the offline price list.

    Keys: desc, net_price, sell_price, price (formatted), lt, moq, burkert_id, type, source.
    """
    entry = _lookup_entry(
        part_no,
        search_context=search_context,
        burkert_id=burkert_id,
        technical_specs=technical_specs,
    )
    if not entry:
        return None

    net_price = entry.get("net_price")
    customer_lt = entry.get("customer_lead_time") or "[TBC]"
    moq = _parse_moq(entry.get("moq"))
    requested_qty = max(1, int(qty))
    quoted_qty, moq_applied = apply_moq_to_qty(requested_qty, moq)

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
        "qty": quoted_qty,
        "requested_qty": requested_qty,
        "moq": moq,
        "moq_applied": moq_applied,
        "net_price": net_price,
        "sell_price": sell_price,
        "price": price_display,
        "lt": customer_lt,
        "burkert_id": entry.get("burkert_id"),
        "burkert_id_display": format_burkert_id_display(str(entry.get("burkert_id") or "")),
        "type": part_type,
        "factory_lead_time": entry.get("factory_lead_time"),
        "source": "BURKERT_PRICE_LIST",
    }
