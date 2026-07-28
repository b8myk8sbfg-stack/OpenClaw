"""Parker Hannifin (incl. Legris, Rectus, Parflex) offline price list via SQLite."""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

DEFAULT_PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Parker Price List")
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parker_price_list.db")
DEFAULT_LEAD_TIME = "12-14 weeks"
MARKUP_DIVISOR = float(os.getenv("OPENCLAW_MARKUP_DIVISOR", "0.72"))
BRAND_NAME = "PARKER"

PART_LINE_RE = re.compile(
    r"^(?P<part>[A-Z0-9][A-Z0-9./\-]*)\s+(?P<rest>.+?)\s+"
    r"(?P<uom>EA|MT|KG|M|PC|SET|FT|MR)\s+"
    r"(?P<code>8C-[A-Z0-9]+)\s+"
    r"(?P<price>[0-9,]+(?:\.[0-9]+)?)\s*$",
    re.I,
)
DISCOUNT_LINE_RE = re.compile(
    r"^(?P<code>8C-[A-Z0-9]+)\s+(?P<desc>.+?)\s+(?P<pct>\d+(?:\.\d+)?)\%\s+",
    re.I,
)

PARKER_FAMILY_BRANDS = frozenset({
    "PARKER",
    "LEGRIS",
    "RECTUS",
    "PARFLEX",
})

PROD_CODE_FAMILY = {
    "8C-REC1": "RECTUS",
    "8C-REC2": "LEGRIS",
    "8C-PF01": "PARFLEX",
    "8C-PF02": "PARFLEX",
    "8C-PF03": "PARFLEX",
}

_DB_READY = False


def pdf_dir() -> str:
    return os.getenv("PARKER_PRICE_LIST_DIR", DEFAULT_PDF_DIR).strip()


def db_path() -> str:
    return os.getenv("PARKER_PRICE_LIST_DB", DEFAULT_DB_PATH).strip()


def default_lead_time() -> str:
    return os.getenv("OPENCLAW_PARKER_DEFAULT_LT", DEFAULT_LEAD_TIME).strip() or DEFAULT_LEAD_TIME


def normalize_part_number(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def compact_part_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_part_number(value))


def part_lookup_keys(part_no: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    add(normalize_part_number(part_no))
    add(compact_part_number(part_no))
    return keys


def is_parker_family_brand(brand: str) -> bool:
    brand_u = str(brand or "").upper().replace("_", " ").replace("-", " ").strip()
    if brand_u in PARKER_FAMILY_BRANDS:
        return True
    return brand_u in {"LEGRIS", "RECTUS", "PARFLEX"}


def family_from_prod_code(prod_code: str) -> str:
    return PROD_CODE_FAMILY.get(str(prod_code or "").upper().strip(), BRAND_NAME)


def _parse_price(raw: str) -> float | None:
    text = str(raw or "").replace(",", "").strip()
    try:
        value = float(text)
        return value if value > 0 else None
    except ValueError:
        return None


def _connect(db_file: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file or db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discount_codes (
            prod_code TEXT PRIMARY KEY,
            description TEXT,
            discount_pct REAL NOT NULL,
            effective_date TEXT,
            source_file TEXT
        );
        CREATE TABLE IF NOT EXISTS parts (
            part_number TEXT PRIMARY KEY,
            part_number_norm TEXT NOT NULL,
            part_number_compact TEXT NOT NULL,
            description TEXT,
            uom TEXT,
            prod_code TEXT NOT NULL,
            list_price_myr REAL NOT NULL,
            source_pdf TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_parts_norm ON parts(part_number_norm);
        CREATE INDEX IF NOT EXISTS idx_parts_compact ON parts(part_number_compact);
        CREATE INDEX IF NOT EXISTS idx_parts_prod_code ON parts(prod_code);
        """
    )


def parse_discount_pdf(pdf_path: str) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    rows: list[dict[str, Any]] = []
    reader = PdfReader(pdf_path)
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            line = line.strip()
            match = DISCOUNT_LINE_RE.match(line)
            if not match:
                continue
            rows.append(
                {
                    "prod_code": match.group("code").upper(),
                    "description": match.group("desc").strip(),
                    "discount_pct": float(match.group("pct")),
                    "effective_date": "",
                    "source_file": os.path.basename(pdf_path),
                }
            )
    return rows


def parse_price_list_pdf(pdf_path: str) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    rows: list[dict[str, Any]] = []
    reader = PdfReader(pdf_path)
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            line = line.strip()
            match = PART_LINE_RE.match(line)
            if not match:
                continue
            list_price = _parse_price(match.group("price"))
            if list_price is None:
                continue
            part_number = normalize_part_number(match.group("part"))
            rows.append(
                {
                    "part_number": part_number,
                    "part_number_norm": part_number,
                    "part_number_compact": compact_part_number(part_number),
                    "description": match.group("rest").strip(),
                    "uom": match.group("uom").upper(),
                    "prod_code": match.group("code").upper(),
                    "list_price_myr": list_price,
                    "source_pdf": os.path.basename(pdf_path),
                }
            )
    return rows


def build_parker_price_database(
    pdf_directory: str | None = None,
    database_path: str | None = None,
) -> dict[str, int]:
    """Parse Parker PDFs and rebuild the SQLite lookup database."""
    pdf_directory = pdf_directory or pdf_dir()
    database_path = database_path or db_path()
    started = time.time()

    discount_files = sorted(glob.glob(os.path.join(pdf_directory, "*DS*.pdf")))
    price_list_files = sorted(
        path
        for path in glob.glob(os.path.join(pdf_directory, "*.pdf"))
        if re.search(r"FY\d{2}-PL", os.path.basename(path), re.I)
    )

    if not price_list_files:
        raise FileNotFoundError(f"No Parker FY*-PL*.pdf files found in {pdf_directory}")

    os.makedirs(os.path.dirname(os.path.abspath(database_path)), exist_ok=True)
    if os.path.exists(database_path):
        os.remove(database_path)

    conn = _connect(database_path)
    _ensure_schema(conn)

    discount_rows: dict[str, dict[str, Any]] = {}
    for pdf_file in discount_files:
        for row in parse_discount_pdf(pdf_file):
            discount_rows[row["prod_code"]] = row

    for row in discount_rows.values():
        conn.execute(
            """
            INSERT INTO discount_codes (prod_code, description, discount_pct, effective_date, source_file)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["prod_code"],
                row["description"],
                row["discount_pct"],
                row.get("effective_date") or "",
                row.get("source_file") or "",
            ),
        )

    inserted = 0
    duplicates = 0
    for pdf_file in price_list_files:
        pdf_name = os.path.basename(pdf_file)
        print(f"📄 [PARKER] Parsing {pdf_name}...")
        for row in parse_price_list_pdf(pdf_file):
            try:
                conn.execute(
                    """
                    INSERT INTO parts (
                        part_number, part_number_norm, part_number_compact,
                        description, uom, prod_code, list_price_myr, source_pdf
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["part_number"],
                        row["part_number_norm"],
                        row["part_number_compact"],
                        row["description"],
                        row["uom"],
                        row["prod_code"],
                        row["list_price_myr"],
                        row["source_pdf"],
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                duplicates += 1

    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("pdf_dir", pdf_directory),
    )
    conn.commit()
    conn.close()

    elapsed = time.time() - started
    stats = {
        "discount_codes": len(discount_rows),
        "parts": inserted,
        "duplicates_skipped": duplicates,
        "price_list_files": len(price_list_files),
        "elapsed_sec": round(elapsed, 1),
    }
    print(
        f"✅ [PARKER] Database built: {inserted} parts, {len(discount_rows)} discount codes "
        f"({duplicates} duplicate parts skipped) in {elapsed:.1f}s"
    )
    print(f"   DB: {database_path}")
    global _DB_READY
    _DB_READY = False
    return stats


def _database_exists() -> bool:
    path = db_path()
    return os.path.isfile(path) and os.path.getsize(path) > 0


def ensure_parker_database() -> bool:
    global _DB_READY
    if _DB_READY and _database_exists():
        return True
    if not _database_exists():
        print(f"⚠️ [PARKER] Database not found: {db_path()}")
        print("   Run: uv run python scripts/build_parker_price_db.py")
        return False
    _DB_READY = True
    return True


def lookup_parker_entry(part_no: str, *, log_miss: bool = True) -> dict[str, Any] | None:
    if not ensure_parker_database():
        return None

    conn = _connect()
    try:
        for key in part_lookup_keys(part_no):
            row = conn.execute(
                """
                SELECT p.*, d.discount_pct, d.description AS discount_desc
                FROM parts p
                LEFT JOIN discount_codes d ON d.prod_code = p.prod_code
                WHERE p.part_number_norm = ? OR p.part_number_compact = ?
                LIMIT 1
                """,
                (key, key),
            ).fetchone()
            if row:
                print(f"   ✅ [PARKER] Matched {part_no!r} → {row['part_number']} ({row['prod_code']})")
                return dict(row)
    finally:
        conn.close()

    if log_miss:
        tried = ", ".join(part_lookup_keys(part_no)[:4])
        print(f"   ⚠️ [PARKER] No price list match for {part_no!r} (tried: {tried})")
    return None


def calc_parker_sell_price(list_price: float, discount_pct: float) -> tuple[float, float]:
    """Return (nett_price, sell_price) where sell = nett / markup divisor."""
    nett = float(list_price) * (1.0 - (float(discount_pct) / 100.0))
    sell = nett / MARKUP_DIVISOR if MARKUP_DIVISOR > 0 else nett
    return nett, sell


def lookup_parker_quote(
    part_no: str,
    qty: int = 1,
    brand: str = "",
    search_context: str = "",
) -> dict[str, Any] | None:
    _ = search_context
    entry = lookup_parker_entry(part_no)
    if not entry:
        return None

    discount_pct = entry.get("discount_pct")
    list_price = entry.get("list_price_myr")
    if list_price is None:
        return None

    if discount_pct is None:
        print(
            f"   ⚠️ [PARKER] No discount for prod code {entry.get('prod_code')} — using 0% discount"
        )
        discount_pct = 0.0

    nett, sell = calc_parker_sell_price(float(list_price), float(discount_pct))
    quoted_qty = max(1, int(qty))
    family = family_from_prod_code(entry.get("prod_code") or "")
    material = str(entry.get("part_number") or part_no).upper()
    description = str(entry.get("description") or "").strip()
    label_brand = str(brand or "").upper().strip()
    if label_brand not in PARKER_FAMILY_BRANDS:
        label_brand = family if family != BRAND_NAME else BRAND_NAME

    desc = f"{label_brand} {material}"
    if description and description.upper() not in desc.upper():
        desc = f"{desc} — {description}"

    return {
        "desc": desc,
        "qty": quoted_qty,
        "requested_qty": quoted_qty,
        "list_price": float(list_price),
        "discount_pct": float(discount_pct),
        "nett_price": nett,
        "sell_price": sell,
        "price": f"{sell:,.2f}",
        "lt": default_lead_time(),
        "prod_code": entry.get("prod_code"),
        "family_brand": family,
        "brand": BRAND_NAME,
        "source": "PARKER_PRICE_LIST",
        "needs_supplier": False,
    }
