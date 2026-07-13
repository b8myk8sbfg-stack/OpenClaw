import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import local_ocr


class LocalOcrTests(unittest.TestCase):
    def test_ocr_payload_to_json_is_compact(self):
        payload = {
            "engine": "tesseract",
            "lang": "eng",
            "full_text": "CPM1A-30CDR-D-V1",
            "lines": [{"text": "CPM1A-30CDR-D-V1", "confidence": 91.2}],
            "error": None,
        }
        rendered = local_ocr.ocr_payload_to_json(payload)
        parsed = json.loads(rendered)
        self.assertEqual(parsed["full_text"], "CPM1A-30CDR-D-V1")
        self.assertEqual(parsed["lines"][0]["text"], "CPM1A-30CDR-D-V1")

    def test_has_usable_ocr_text(self):
        self.assertTrue(local_ocr.has_usable_ocr_text({"full_text": "ABC", "error": None}))
        self.assertFalse(local_ocr.has_usable_ocr_text({"full_text": "", "error": "no_text_detected"}))

    def test_missing_image_returns_error(self):
        result = local_ocr.extract_text_from_image("/tmp/does-not-exist.png")
        self.assertEqual(result["error"], "image_not_found")
        self.assertEqual(result["full_text"], "")

    def test_tesseract_missing_reports_error(self):
        with patch.object(local_ocr, "_resolve_tesseract_cmd", return_value=None):
            result = local_ocr.extract_text_from_image(__file__)
        self.assertEqual(result["error"], "tesseract_not_installed")


if __name__ == "__main__":
    unittest.main()
