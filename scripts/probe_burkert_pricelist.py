#!/usr/bin/env python3
"""Probe Burkert XLSM price list structure — run on Mac where the file lives."""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Price List 220015_MYR_15.01.2026-3 (Level 3) Robomatics.xlsm",
)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print(f"❌ File not found: {path}")
        print("Usage: uv run python scripts/probe_burkert_pricelist.py [path-to-xlsm]")
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
    print("=== Sample lookup keys for nameplate '6519 H 8.0' ===")
    try:
        from burkert_price_list import burkert_lookup_keys, lookup_burkert_quote

        keys = burkert_lookup_keys("6519 H 8.0")
        print(f"Keys: {keys}")
        quote = lookup_burkert_quote(
            "6519 H 8.0",
            search_context="Coil voltage: 24V DC",
            burkert_id="00132465",
        )
        print(f"Quote by ID: {quote}")
        quote2 = lookup_burkert_quote(
            "6519 H 8.0",
            search_context="Coil voltage: 24V DC",
        )
        print(f"Quote by type: {quote2}")
    except Exception as exc:
        print(f"(lookup test skipped: {exc})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
