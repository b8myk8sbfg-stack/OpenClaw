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
            return_value=[{"part_no": "CPM1A-30CDR-D-V1", "qty": 2, "brand": "OMRON"}],
        ):
            result = openclaw_main.unified_analyze("报价，我要两个", image_path="/tmp/label.png")

        self.assertEqual(result["source"], "copilot")
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


if __name__ == "__main__":
    unittest.main()
