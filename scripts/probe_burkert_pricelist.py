#!/usr/bin/env python3
"""
Probe Burkert XLSM price list — run on Mac where the file lives.

Usage:
  uv run python scripts/probe_burkert_pricelist.py
  uv run python scripts/probe_burkert_pricelist.py 6519-H08
  uv run python scripts/probe_burkert_pricelist.py /path/to/pricelist.xlsm 6519-H08
"""

from __future__ import annotations

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.runtime_deps import require_modules

require_modules("pandas", "openpyxl")

import pandas as pd

from burkert_price_list import DEFAULT_PRICE_LIST_PATH, _price_list_path


def _print_usage() -> None:
    default = _price_list_path()
    print("Usage:")
    print("  uv run python scripts/probe_burkert_pricelist.py")
    print("      Dump XLSM structure (default file path)")
    print("  uv run python scripts/probe_burkert_pricelist.py <part-no>")
    print("      Lookup quote for a Burkert type / nameplate (e.g. 6519-H08)")
    print("  uv run python scripts/probe_burkert_pricelist.py <path.xlsm> [part-no]")
    print("")
    print(f"Default price list: {default}")
    print("Override with env: BURKERT_PRICE_LIST_XLSM=/path/to/file.xlsm")


def _parse_args():
    path = _price_list_path()
    part = None

    for arg in sys.argv[1:]:
        token = str(arg or "").strip()
        if not token:
            continue
        if token.lower().endswith((".xlsm", ".xlsx")) or os.path.isfile(token):
            path = token
        else:
            part = token

    return path, part


def _run_part_lookup(path: str, part: str) -> int:
    if not os.path.exists(path):
        print(f"❌ Price list not found: {path}")
        _print_usage()
        return 1

    os.environ["BURKERT_PRICE_LIST_XLSM"] = path

    from burkert_price_list import load_burkert_price_list, lookup_burkert_quote

    print(f"📄 Price list: {path}")
    print(f"🔎 Lookup part: {part}")
    if not load_burkert_price_list(force=True):
        print("❌ Could not load Burkert price list.")
        return 1

    quote = lookup_burkert_quote(part, qty=1)
    if not quote:
        print(f"❌ No price list match for {part!r}")
        return 1

    print("")
    print("Quote dict:")
    print(json.dumps(quote, indent=2, default=str))
    return 0


def _run_structure_probe(path: str) -> int:
    if not os.path.exists(path):
        print(f"❌ Price list not found: {path}")
        _print_usage()
        return 1

    print(f"📄 Reading: {path}")
    xl = pd.ExcelFile(path)
    print(f"Sheets: {xl.sheet_names}")

    df = pd.read_excel(path, sheet_name=0, header=None)
    print(f"Shape: {df.shape[0]} rows × {df.shape[1]} columns")
    print()

    keywords = (
        "article", "order", "material", "description", "price", "net",
        "discount", "type", "code", "unit", "list",
    )
    print("=== Likely header rows (first 200 lines) ===")
    found = 0
    for i in range(min(200, len(df))):
        cells = [str(x).strip() for x in df.iloc[i].tolist() if pd.notna(x) and str(x).strip()]
        if not cells:
            continue
        row_text = " | ".join(cells)
        if any(k in row_text.lower() for k in keywords):
            print(f"Row {i}: {row_text[:240]}")
            found += 1
    if not found:
        print("(no keyword header match in first 200 rows)")

    print()
    print("=== Rows 10–25 (cols 0–7) ===")
    print(df.iloc[10:25, :8].to_string())

    print()
    print("=== First row with a numeric-looking price in cols 0–14 ===")
    for i in range(200, min(500, len(df))):
        row = df.iloc[i]
        non_null = [x for x in row.tolist() if pd.notna(x)]
        if len(non_null) >= 3:
            print(f"Row {i}: {row.tolist()[:10]}")
            break

    print()
    print("=== Sample lookup: 6519 H 8.0 ===")
    try:
        os.environ["BURKERT_PRICE_LIST_XLSM"] = path
        from burkert_price_list import burkert_lookup_keys, lookup_burkert_quote

        keys = burkert_lookup_keys("6519 H 8.0")
        print(f"Keys: {keys}")
        quote = lookup_burkert_quote("6519 H 8.0", search_context="Coil voltage: 24V DC")
        print(f"Quote: {quote}")
    except Exception as exc:
        print(f"(lookup test skipped: {exc})")

    return 0


def main() -> int:
    path, part = _parse_args()
    if part:
        return _run_part_lookup(path, part)
    return _run_structure_probe(path)


if __name__ == "__main__":
    raise SystemExit(main())
