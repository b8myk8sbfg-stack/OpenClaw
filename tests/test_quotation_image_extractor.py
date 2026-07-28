import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quotation_image_extractor as qie


class QuotationDetectionTests(unittest.TestCase):
    def test_detects_quotation_from_ocr_signals(self):
        ocr = "Quotation\nOur Ref : Q001300\nUnit Price\nTotal Price\n2 PCE"
        self.assertTrue(qie.is_quotation_document(ocr))

    def test_rejects_product_label_ocr(self):
        ocr = "OMRON\nE2E-X5E1\nPROXIMITY SENSOR\nMADE IN JAPAN"
        self.assertFalse(qie.is_quotation_document(ocr))

    def test_rejects_burkert_nameplate_as_quotation(self):
        ocr = (
            "5281 A 25.0 NBR MS\n00134328\nG1 PN0.2-16bar\n"
            "230V 50-60Hz 8W\nUS $255\nCondition:\nQuantity:"
        )
        self.assertTrue(qie.is_product_label_photo(ocr))
        self.assertFalse(qie.is_quotation_document(ocr))

    def test_rejects_known_hallucination_without_ocr_support(self):
        ocr = "Quotation\nOur Ref : Q001300\nUnit Price\nTotal Price\n2 PCE"
        ok, reason = qie.validate_vision_against_ocr(
            {"item_code": "KQ2L06-01A", "qty": 1, "unit_price": 9.8, "total_price": 9.8},
            ocr,
        )
        self.assertFalse(ok)
        self.assertIn("hallucination", reason.lower())

    def test_accepts_vision_when_ocr_contains_item_code(self):
        ocr = "Quotation\n89PR10KLF TRIMMER RESISTOR\nUnit Price\n2 PCE"
        ok, reason = qie.validate_vision_against_ocr(
            {"item_code": "89PR10KLF", "qty": 2, "unit_price": 60, "total_price": 120},
            ocr,
        )
        self.assertTrue(ok, reason)

    def test_our_ref_token(self):
        self.assertTrue(qie.is_our_ref_token("Q001300"))
        self.assertFalse(qie.is_our_ref_token("89PR10KLF"))


class QuotationValidationTests(unittest.TestCase):
    def test_valid_ramatex_fields(self):
        data = {
            "item_code": "89PR10KLF",
            "description": "TRIMMER RESISTOR, 10K RESISTANCE VALUE",
            "qty": 2,
            "unit_price": 60.0,
            "total_price": 120.0,
            "delivery": "2-3 WEEKS",
            "total_amount": 120.0,
        }
        ok, reason = qie.validate_quotation_fields(data)
        self.assertTrue(ok, reason)

    def test_rejects_our_ref_as_item_code(self):
        data = {"item_code": "Q001300", "qty": 2, "unit_price": 60, "total_price": 120}
        ok, reason = qie.validate_quotation_fields(data)
        self.assertFalse(ok)
        self.assertIn("Our Ref", reason)

    def test_rejects_arithmetic_mismatch(self):
        data = {
            "item_code": "89PR10KLF",
            "qty": 10,
            "unit_price": 60.0,
            "total_price": 120.0,
            "description": "TRIMMER RESISTOR",
        }
        ok, reason = qie.validate_quotation_fields(data)
        self.assertFalse(ok)

    def test_rejects_qty_from_10k(self):
        data = {
            "item_code": "89PR10KLF",
            "qty": 10,
            "unit_price": 60,
            "total_price": 600,
            "description": "10K RESISTANCE VALUE",
        }
        ok, reason = qie.validate_quotation_fields(data)
        self.assertFalse(ok)
        self.assertIn("10K", reason)


class QuotationConversionTests(unittest.TestCase):
    def test_caption_qty_overrides_document_qty(self):
        data = {
            "item_code": "89PR10KLF",
            "description": "TRIMMER RESISTOR",
            "qty": 2,
            "unit_price": 60.0,
            "total_price": 120.0,
            "delivery": "2-3 WEEKS",
            "total_amount": 120.0,
        }
        items = qie.quotation_to_rfq_items(data, "boleh quote lagi tak barang ini? 3pcs.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["part_no"], "89PR10KLF")
        self.assertEqual(items[0]["qty"], 3)
        self.assertEqual(items[0]["quotation_meta"]["document_qty"], 2)

    def test_normalize_alternate_json_keys(self):
        raw = {
            "Item Code": "89PR10KLF",
            "Description": "TRIMMER RESISTOR",
            "Quantity": 2,
            "Unit Price": "60.00",
            "Total Price": "120.00",
            "Delivery": "2-3 WEEKS",
            "Total Amount": "120.00",
        }
        norm = qie.normalize_quotation_payload(raw)
        self.assertEqual(norm["item_code"], "89PR10KLF")
        self.assertEqual(norm["qty"], 2)
        self.assertEqual(norm["unit_price"], 60.0)


class QuotationRouteTests(unittest.TestCase):
    def test_try_extract_returns_none_for_label(self):
        ocr = {"full_text": "OMRON E2E-X5E1 PROXIMITY SENSOR", "lines": [], "error": None}
        with patch("local_ocr.extract_text_from_image", return_value=ocr):
            result = qie.try_extract_quotation_image("/tmp/label.png", "quote 1")
        self.assertIsNone(result)

    def test_try_extract_wires_through_on_success(self):
        import tempfile
        ocr = {
            "full_text": "Quotation\nOur Ref : Q001300\nUnit Price\nTotal Price\n2 PCE",
            "lines": [{"text": "Quotation"}],
            "error": None,
        }
        validated = {
            "item_code": "89PR10KLF",
            "description": "TRIMMER RESISTOR",
            "qty": 2,
            "unit_price": 60.0,
            "total_price": 120.0,
            "delivery": "2-3 WEEKS",
            "total_amount": 120.0,
        }
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\xff\xd8\xff\xe0")
            image_path = tmp.name
        try:
            with patch("local_ocr.extract_text_from_image", return_value=ocr):
                with patch("local_ocr.has_usable_ocr_text", return_value=True):
                    with patch.object(qie, "_copilot_vision_extract", return_value=validated):
                        result = qie.try_extract_quotation_image(image_path, "3pcs")
        finally:
            os.unlink(image_path)

        self.assertIsNotNone(result)
        self.assertEqual(result["route"], "copilot_quotation_vision")
        self.assertEqual(result["items"][0]["part_no"], "89PR10KLF")
        self.assertEqual(result["items"][0]["qty"], 3)


if __name__ == "__main__":
    unittest.main()
