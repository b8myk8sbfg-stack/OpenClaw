"""Unit tests for OBM NEW stock-code fallback on known-brand parts."""

import unittest

from obm_quotation_helper import (
    extract_brand_from_item,
    should_use_new_stock_pid,
    resolve_stock_pid,
    NEW_STOCK_PID,
)


class ObmNewStockTests(unittest.TestCase):
    def test_extract_brand_from_desc(self):
        item = {"desc": "SMC MXY12-150", "qty": 2, "price": "[TBC]"}
        self.assertEqual(extract_brand_from_item(item), "SMC")

    def test_should_use_new_for_smc(self):
        item = {
            "desc": "SMC MXY12-150",
            "pid": "MXY12-150",
            "qty": 2,
            "price": "[TBC]",
        }
        self.assertTrue(should_use_new_stock_pid(item, "MXY12-150"))

    def test_should_not_use_new_for_unknown_brand(self):
        item = {
            "desc": "UNKNOWN BRAND ABC123",
            "pid": "ABC123",
            "qty": 1,
            "price": "[TBC]",
        }
        self.assertFalse(should_use_new_stock_pid(item, "ABC123"))

    def test_resolve_stock_pid_uses_new_when_configured(self):
        import obm_quotation_helper as helper

        original_stock = helper.STOCK
        helper.STOCK = [
            {"pid": "NEW", "n_pid": "NEW", "n_stock": "NEW", "n_model": "NEW"},
        ]
        try:
            item = {"desc": "SMC MXY12-150", "pid": "MXY12-150", "brand": "SMC"}
            self.assertEqual(resolve_stock_pid("MXY12-150", item), NEW_STOCK_PID)
        finally:
            helper.STOCK = original_stock


if __name__ == "__main__":
    unittest.main()
