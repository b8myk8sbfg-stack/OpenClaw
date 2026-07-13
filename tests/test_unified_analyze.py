import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from openai import APIStatusError

import openclaw_main


class UnifiedAnalyzeTests(unittest.TestCase):
    def test_copilot_success_returns_copilot_source(self):
        with patch.object(
            openclaw_main,
            "_extract_rfq_with_copilot_only",
            return_value={
                "items": [{"part_no": "CPM1A-30CDR-D-V1", "qty": 2, "brand": "OMRON"}],
                "route": "ocr_copilot",
                "ocr_used": True,
            },
        ):
            result = openclaw_main.unified_analyze("报价，我要两个", image_path="/tmp/label.png")

        self.assertEqual(result["source"], "copilot")
        self.assertEqual(result["route"], "ocr_copilot")
        self.assertTrue(result["ocr_used"])
        self.assertFalse(result["copilot_failed"])
        self.assertEqual(len(result["items"]), 1)

    def test_copilot_503_falls_back_to_openai(self):
        err = APIStatusError(
            "Error code: 503",
            response=MagicMock(status_code=503),
            body={"error": {"type": "clearance_required", "message": "Cloudflare clearance expired"}},
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENCLAW_AI_FALLBACK": "openai"}):
            with patch.object(openclaw_main, "_extract_rfq_with_copilot_only", side_effect=err):
                with patch.object(
                    openclaw_main,
                    "_extract_rfq_with_openai",
                    return_value=[{"part_no": "CPM1A-30CDR-D-V1", "qty": 2, "brand": "OMRON"}],
                ) as openai_mock:
                    result = openclaw_main.unified_analyze(
                        "报价，我要两个",
                        image_path="/tmp/label.png",
                    )

        openai_mock.assert_called_once()
        self.assertTrue(result["copilot_failed"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["source"], "openai")
        self.assertEqual(result["route"], "openai_vision")
        self.assertEqual(result["error"]["status"], 503)
        self.assertEqual(result["error"]["type"], "clearance_required")

    def test_copilot_503_without_openai_key_does_not_fallback(self):
        err = APIStatusError(
            "Error code: 503",
            response=MagicMock(status_code=503),
            body={"error": {"type": "clearance_required", "message": "Cloudflare clearance expired"}},
        )
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(openclaw_main, "_extract_rfq_with_copilot_only", side_effect=err):
                with patch.object(openclaw_main, "_extract_rfq_with_openai") as openai_mock:
                    result = openclaw_main.unified_analyze("quote 2 pcs", image_path="/tmp/label.png")

        openai_mock.assert_not_called()
        self.assertTrue(result["copilot_failed"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["items"], [])

    def test_malfunction_alert_format(self):
        alert = openclaw_main.build_copilot_malfunction_alert(
            operation="unified_analyze",
            customer_name="Robomatics Stephen",
            error={"status": 503, "type": "clearance_required", "message": "Cloudflare clearance expired"},
            caption="Hi, thank you for your message.",
            original_message="报价，我要两个",
        )
        self.assertIn("[OpenClaw Copilot Malfunction]", alert)
        self.assertIn("Operation: unified_analyze", alert)
        self.assertIn("Customer: Robomatics Stephen", alert)
        self.assertIn("HTTP status: 503", alert)
        self.assertIn("报价，我要两个", alert)

    def test_image_routes_through_ocr_to_copilot_text(self):
        ocr_payload = {
            "engine": "tesseract",
            "full_text": "CPM1A-30CDR-D-V1\nOMRON",
            "lines": [{"text": "CPM1A-30CDR-D-V1", "confidence": 95.0}],
            "error": None,
        }
        with patch.dict(os.environ, {"OPENCLAW_OCR_ENABLED": "1"}):
            with patch("local_ocr.extract_text_from_image", return_value=ocr_payload):
                with patch.object(
                    openclaw_main,
                    "_call_copilot_rfq",
                    return_value=[{"part_no": "CPM1A-30CDR-D-V1", "qty": 2, "brand": "OMRON"}],
                ) as copilot_mock:
                    result = openclaw_main._extract_rfq_with_copilot_only(
                        "quote 2 pcs",
                        image_path="/tmp/label.png",
                    )

        self.assertEqual(result["route"], "ocr_copilot")
        self.assertTrue(result["ocr_used"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(copilot_mock.call_count, 1)
        user_content = copilot_mock.call_args[0][2]
        self.assertIn("OCR result (JSON)", user_content)
        self.assertIn("CPM1A-30CDR-D-V1", user_content)

    def test_copilot_ocr_fail_tries_openai_text_before_vision(self):
        ocr_payload = {
            "engine": "tesseract",
            "full_text": "CPM1A-30CDR-D-V1\nOMRON",
            "lines": [{"text": "CPM1A-30CDR-D-V1", "confidence": 95.0}],
            "error": None,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENCLAW_OCR_ENABLED": "1"}):
            with patch("local_ocr.extract_text_from_image", return_value=ocr_payload):
                with patch.object(openclaw_main, "_call_copilot_rfq", return_value=[]):
                    with patch.object(
                        openclaw_main,
                        "_extract_rfq_with_openai_from_ocr",
                        return_value=[{"part_no": "CPM1A-30CDR-D-V1", "qty": 1, "brand": "OMRON"}],
                    ) as openai_text_mock:
                        with patch.object(openclaw_main, "_extract_rfq_with_openai_vision") as vision_mock:
                            result = openclaw_main.unified_analyze(
                                "Quote me 1 unit",
                                image_path="/tmp/label.png",
                            )

        openai_text_mock.assert_called_once()
        vision_mock.assert_not_called()
        self.assertEqual(result["route"], "openai_text_ocr_fallback")
        self.assertEqual(len(result["items"]), 1)

    def test_empty_ocr_goes_to_openai_vision(self):
        ocr_payload = {
            "engine": "tesseract",
            "full_text": "",
            "lines": [],
            "error": "tesseract_not_installed",
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENCLAW_OCR_ENABLED": "1"}):
            with patch("local_ocr.extract_text_from_image", return_value=ocr_payload):
                with patch.object(
                    openclaw_main,
                    "_extract_rfq_with_openai_vision",
                    return_value=[{"part_no": "CPM1A-30CDR-D-V1", "qty": 1, "brand": "OMRON"}],
                ) as openai_mock:
                    result = openclaw_main.unified_analyze(
                        "quote 1 pc",
                        image_path="/tmp/label.png",
                    )

        openai_mock.assert_called_once()
        self.assertEqual(result["source"], "openai")
        self.assertEqual(result["route"], "openai_vision_ocr_fallback")
        self.assertFalse(result["copilot_failed"])
        self.assertTrue(result["fallback_used"])

    def test_empty_ocr_without_openai_key_returns_empty(self):
        ocr_payload = {
            "engine": "tesseract",
            "full_text": "",
            "lines": [],
            "error": "tesseract_not_installed",
        }
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, {**env, "OPENCLAW_OCR_ENABLED": "1"}, clear=True):
            with patch("local_ocr.extract_text_from_image", return_value=ocr_payload):
                with patch.object(openclaw_main, "_extract_rfq_with_openai") as openai_mock:
                    result = openclaw_main.unified_analyze(
                        "quote 1 pc",
                        image_path="/tmp/label.png",
                    )

        openai_mock.assert_not_called()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["route"], "ocr_openai_skipped")


if __name__ == "__main__":
    unittest.main()
