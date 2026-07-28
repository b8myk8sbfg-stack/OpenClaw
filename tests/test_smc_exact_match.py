"""Tests for SMC exact-part warehouse matching (no variant remap)."""

import unittest
from unittest.mock import patch

from openclaw_inquiry_engine import (
    EXACT_LOOKUP,
    WAREHOUSE_ROWS,
    load_warehouse_map,
    normalize_part,
    resolve_warehouse_match,
)


class SmcExactMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_warehouse_map()

    def test_mxy12_150_does_not_match_m9bl_variant(self):
        match = resolve_warehouse_match("MXY12-150", declared_brand="SMC", qty=2)
        self.assertIsNone(match)

    def test_mxy12_150_m9bl_exact_match_when_in_lookup(self):
        norm = normalize_part("MXY12-150-M9BL")
        if norm not in EXACT_LOOKUP:
            self.skipTest("warehouse CSV not loaded in CI")
        match = resolve_warehouse_match("MXY12-150-M9BL", declared_brand="SMC", qty=2)
        self.assertIsNotNone(match)
        self.assertEqual(normalize_part(match.get("stock_name") or ""), norm)

    def test_non_smc_still_allows_fuzzy_match(self):
        if not WAREHOUSE_ROWS:
            self.skipTest("warehouse CSV not loaded in CI")
        with patch(
            "openclaw_inquiry_engine.find_best_warehouse_match",
            return_value={"api_id": "TEST123", "stock_name": "TEST-PART-2M"},
        ) as mock_fuzzy:
            match = resolve_warehouse_match("TEST-PART", declared_brand="OMRON", qty=1)
            self.assertIsNotNone(match)
            mock_fuzzy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
