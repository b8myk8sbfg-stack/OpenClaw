import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inquiry_extraction_helper import extract_clean_items_from_text, is_plausible_part_no


class InquiryExtractionTests(unittest.TestCase):
    def test_rejects_thank_as_part(self):
        self.assertFalse(is_plausible_part_no("THANK"))
        self.assertFalse(is_plausible_part_no("THANKS"))

    def test_accepts_smc_cylinder_part(self):
        self.assertTrue(is_plausible_part_no("MXY12-150"))

    def test_email_part_brand_qty_format(self):
        body = (
            "Please quote the new item Cylinder MXY12-150 "
            "Brand : SMC Qty : 2pcs Thank you."
        )
        items = extract_clean_items_from_text(body)
        parts = [i["part_no"] for i in items]
        self.assertIn("MXY12-150", parts)
        self.assertNotIn("THANK", parts)
        match = next(i for i in items if i["part_no"] == "MXY12-150")
        self.assertEqual(match["brand"], "SMC")
        self.assertEqual(match["qty"], 2)


if __name__ == "__main__":
    unittest.main()
