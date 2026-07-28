import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import non_standard_inquiry_handler as nsh


class LinkVerificationTests(unittest.TestCase):
    def setUp(self):
        nsh._link_verify_cache.clear()

    def test_rejects_invalid_url_shape(self):
        result = nsh.verify_supplier_link("not a url")
        self.assertFalse(result["ok"])

    @patch("non_standard_inquiry_handler.requests.head")
    def test_accepts_ok_head_response(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.almarc.com.my/"
        mock_head.return_value = mock_resp

        result = nsh.verify_supplier_link("https://www.almarc.com.my")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)

    @patch("non_standard_inquiry_handler.requests.get")
    @patch("non_standard_inquiry_handler.requests.head")
    def test_falls_back_to_get_when_head_not_allowed(self, mock_head, mock_get):
        mock_head.return_value = MagicMock(status_code=405, url="https://example.com")
        mock_get.return_value = MagicMock(status_code=200, url="https://example.com/page")

        result = nsh.verify_supplier_link("https://example.com")
        self.assertTrue(result["ok"])

    @patch("non_standard_inquiry_handler.requests.head")
    def test_enrich_filters_failed_links(self, mock_head):
        ok = MagicMock(status_code=200, url="https://good.example")
        bad = MagicMock(status_code=404, url="https://bad.example")
        mock_head.side_effect = [ok, bad]

        rows = nsh.enrich_suggestions_with_link_checks([
            {"title": "Good Co", "url": "https://good.example", "priority": "COPILOT_LOCAL"},
            {"title": "Bad Co", "url": "https://bad.example", "priority": "COPILOT_LOCAL"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Good Co")


class TechnicalEmailBodyTests(unittest.TestCase):
    def test_email_contains_summary_and_supplier_table(self):
        items = [{
            "brand": "UNKNOWN",
            "part_no": "CUSTOM HOSE",
            "desc": "STAINLESS STEEL TEFLON HOSE 3000mm",
            "qty": 8,
            "reason": "Unknown brand / not found in warehouse",
        }]
        all_suggestions = {
            "CUSTOM HOSE": [
                {
                    "title": "Almarc Engineering",
                    "url": "https://www.almarc.com.my",
                    "priority": "COPILOT_LOCAL",
                    "source": "copilot",
                    "snippet": "Hose supplier",
                    "link_ok": True,
                    "link_check": {"ok": True, "status_code": 200, "final_url": "https://www.almarc.com.my"},
                }
            ],
        }
        html_body = nsh.build_technical_email_body(
            "I-PEX",
            "ipg.purchase03@i-pex.com",
            "EMAIL",
            items,
            "Please quote hose",
            all_suggestions,
            verified_suggestions=all_suggestions,
        )
        self.assertIn("Inquiry Summary", html_body)
        self.assertIn("Items Requiring Technical Review", html_body)
        self.assertIn("Supplier Research (link-tested)", html_body)
        self.assertIn("Verified Website", html_body)
        self.assertIn("https://www.almarc.com.my", html_body)
        self.assertIn("Almarc Engineering", html_body)
        self.assertIn("CUSTOM HOSE", html_body)


if __name__ == "__main__":
    unittest.main()
