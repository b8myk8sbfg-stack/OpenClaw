import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inquiry_extraction_helper import (
    extract_clean_items_from_text,
    format_inquiry_description,
    is_plausible_part_no,
    normalize_inquiry_item,
    parse_brand_prefixed_part,
)
from auto_claw import extract_structured_rfq_items


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

    def test_parse_brand_prefixed_part_smc(self):
        brand, part = parse_brand_prefixed_part("SMC-AS2201F-01-04SA")
        self.assertEqual(brand, "SMC")
        self.assertEqual(part, "AS2201F-01-04SA")

    def test_normalize_strips_duplicate_brand_prefix(self):
        brand, part = normalize_inquiry_item("SMC", "SMC-AS2201F-01-04SA")
        self.assertEqual(brand, "SMC")
        self.assertEqual(part, "AS2201F-01-04SA")
        self.assertEqual(format_inquiry_description(brand, part), "SMC AS2201F-01-04SA")

    def test_normalize_keyence_and_cpc_prefixes(self):
        cases = [
            ("UNKNOWN", "KEYENCE-FU-35TZ", "KEYENCE", "FU-35TZ"),
            ("UNKNOWN", "CPC-MR12WNSS", "CPC", "MR12WNSS"),
            ("UNKNOWN", "KOGANEI-JDADS32X10", "KOGANEI", "JDADS32X10"),
        ]
        for in_brand, in_part, out_brand, out_part in cases:
            brand, part = normalize_inquiry_item(in_brand, in_part)
            self.assertEqual((brand, part), (out_brand, out_part))

    def test_numbered_brand_part_qty_email_format(self):
        body = """
1 ). CPC-MR12WNSS : MR12WNSS LINEAR SLIDE LENGTH 220MM   (Qty :  1 pc.)
2 ). SMC-AS2201F-01-04SA   (Qty : 2 pcs.)
13 ). KEYENCE-FU-35TZ   (Qty : 2 pcs.)
"""
        items = extract_structured_rfq_items(body.upper())
        by_part = {i["part_no"]: i for i in items}
        self.assertEqual(by_part["MR12WNSS"]["brand"], "CPC")
        self.assertEqual(by_part["AS2201F-01-04SA"]["brand"], "SMC")
        self.assertEqual(by_part["AS2201F-01-04SA"]["qty"], 2)
        self.assertEqual(by_part["FU-35TZ"]["brand"], "KEYENCE")
        self.assertNotIn("SMC-AS2201F-01-04SA", by_part)
        self.assertEqual(
            format_inquiry_description("SMC", "AS2201F-01-04SA"),
            "SMC AS2201F-01-04SA",
        )

    def test_caption_pas_typo_qty_hint(self):
        from inquiry_extraction_helper import (
            apply_caption_qty_to_items,
            extract_qty_from_caption,
            normalize_qty_caption_text,
        )

        self.assertEqual(normalize_qty_caption_text("Quote me 2 PAS"), "Quote me 2 PCS")
        self.assertEqual(extract_qty_from_caption("Quote me 2 PAS"), 2)
        items = apply_caption_qty_to_items(
            [{"part_no": "001372465", "qty": 1, "brand": "UNKNOWN"}],
            "Quote me 2 PAS",
        )
        self.assertEqual(items[0]["qty"], 2)

    def test_burkert_ocr_s_to_5(self):
        from inquiry_extraction_helper import (
            burkert_id_ocr_variants,
            looks_like_burkert_article_id,
            normalize_burkert_part_from_ocr,
        )

        self.assertEqual(normalize_burkert_part_from_ocr("00137246S"), "001372465")
        self.assertTrue(looks_like_burkert_article_id("00137246S"))
        self.assertIn("001372465", burkert_id_ocr_variants("00137246S"))


if __name__ == "__main__":
    unittest.main()
