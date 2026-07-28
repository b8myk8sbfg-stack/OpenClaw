"""Unit tests for SMC portal lead-time and row-selection logic (no Selenium)."""

import re
import unittest
from typing import Any

PENINSULA_WAREHOUSES = frozenset({"JH", "PG", "SJ"})
LT_EX_STOCK = "1 week"
LT_INDENT = "4-6 weeks"


def compact_part(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part or "").upper())


def parts_equal(a: str, b: str) -> bool:
    return compact_part(a) == compact_part(b)


def parse_qty(value: Any) -> int:
    text = str(value or "").strip()
    if not text or text in ("-", "—"):
        return 0
    match = re.search(r"-?\d+", text.replace(",", ""))
    return max(0, int(match.group(0))) if match else 0


def parse_money(text: str) -> float | None:
    match = re.search(
        r"(?:MYR|RM)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
        str(text or ""),
        re.I,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def compute_lead_time_from_rows(rows: list[dict[str, Any]]) -> str:
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


class SmcLeadTimeTests(unittest.TestCase):
    def test_ex_stock_when_avail_in_jh(self):
        rows = [{"whs": "JH", "avail": "5", "pnt1": "0", "pnt2": "0"}]
        self.assertEqual(compute_lead_time_from_rows(rows), "1 week")

    def test_ex_stock_when_pnt_assembly(self):
        rows = [{"whs": "PG", "avail": "0", "pnt1": "3", "pnt2": "0"}]
        self.assertEqual(compute_lead_time_from_rows(rows), "1 week")

    def test_indent_when_no_peninsula_stock(self):
        rows = [{"whs": "JH", "avail": "0", "pnt1": "0", "pnt2": "0"}]
        self.assertEqual(compute_lead_time_from_rows(rows), "4-6 weeks")

    def test_exact_part_match(self):
        rows = [
            {"pn": "C96SDB40-50C", "whs": "JH"},
            {"pn": "C96SDB40-50C-M9B", "whs": "JH"},
        ]
        self.assertEqual(len(pick_exact_part_rows(rows, "C96SDB40-50C")), 1)

    def test_parse_money_myr(self):
        self.assertEqual(parse_money("MYR 285.28"), 285.28)

    def test_pick_best_price_row_prefers_priced_row(self):
        from smc_portal_lookup import pick_best_price_row

        rows = [
            {"whs": "JH", "avail": "0", "pnt1": "0", "pnt2": "0", "net_price_text": ""},
            {"whs": "JH", "avail": "0", "pnt1": "0", "pnt2": "0", "net_price_text": "MYR 16.32"},
        ]
        best = pick_best_price_row(rows)
        self.assertEqual(best["net_price_text"], "MYR 16.32")

    def test_resolve_hit_prefers_exact_row_at_bottom(self):
        from smc_portal_lookup import resolve_hit_from_rows

        rows = [
            {
                "pn": "AS1002F-04A",
                "whs": "JH",
                "avail": "0",
                "pnt1": "0",
                "pnt2": "0",
                "net_price_text": "",
                "description": "SMC SPEED CONTROLLER VARIANT",
            },
            {
                "pn": "AS1002F-04",
                "whs": "SJ",
                "avail": "2681",
                "pnt1": "0",
                "pnt2": "0",
                "net_price_text": "MYR 20.75",
                "description": "SMC SPEED CONTROLLER",
            },
        ]
        hit = resolve_hit_from_rows(rows, "AS1002F-04")
        self.assertEqual(hit["part_no"], "AS1002F-04")
        self.assertEqual(hit["net_price"], 20.75)
        self.assertEqual(hit["lead_time"], "1 week")

    def test_merge_grid_rows_deduplicates_warehouse_rows(self):
        from smc_portal_lookup import _merge_grid_rows

        first = [{"pn": "AS1002F-04", "whs": "SJ", "net_price_text": "MYR 20.75"}]
        second = [
            {"pn": "AS1002F-04", "whs": "SJ", "net_price_text": "MYR 20.75"},
            {"pn": "AS1002F-04", "whs": "JH", "net_price_text": "MYR 18.00"},
        ]
        merged = _merge_grid_rows(first, second)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
