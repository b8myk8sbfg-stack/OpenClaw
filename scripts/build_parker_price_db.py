#!/usr/bin/env python3
"""Build Parker Hannifin SQLite price database from PDF price lists + discount schedule."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parker_price_list import build_parker_price_database, db_path, pdf_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Parker price list SQLite database")
    parser.add_argument(
        "--pdf-dir",
        default=pdf_dir(),
        help="Directory containing Parker FY*-PL*.pdf and *DS*.pdf files",
    )
    parser.add_argument(
        "--db",
        default=db_path(),
        help="Output SQLite database path",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.pdf_dir):
        print(f"❌ PDF directory not found: {args.pdf_dir}")
        return 1

    stats = build_parker_price_database(pdf_directory=args.pdf_dir, database_path=args.db)
    print("")
    print("Summary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
