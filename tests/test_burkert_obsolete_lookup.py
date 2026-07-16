import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import burkert_obsolete_lookup as obs


class BurkertObsoleteLookupTests(unittest.TestCase):
    def test_extract_article_ids(self):
        text = "replacement Bürkert 00221858 for obsolete 00134328 (134328)"
        ids = obs.extract_burkert_article_ids(text)
        self.assertIn("00221858", ids)
        self.assertIn("00134328", ids)

    def test_format_full_spec_comparison(self):
        summary = obs.format_obsolete_research_summary(
            {
                "is_obsolete": True,
                "original_part": "00134328",
                "original_specifications": {
                    "type": "5281",
                    "title": "Type 5281 servo-assisted solenoid valve",
                    "specs": [
                        {"label": "Valve type", "value": "2/2-way NC pilot-operated"},
                        {"label": "Connection size", "value": "DN25 (G1\")"},
                        {"label": "Voltage", "value": "230 VAC 50/60 Hz"},
                    ],
                },
                "replacement_parts": [
                    {
                        "article_id": "00221858",
                        "type": "6281",
                        "title": "Type 6281 EV",
                        "specifications": {
                            "specs": [
                                {"label": "Valve type", "value": "2/2-way NC pilot-operated"},
                                {"label": "Connection size", "value": "DN25 (G1\")"},
                                {"label": "Voltage", "value": "230 VAC 50/60 Hz"},
                            ]
                        },
                        "comparison_notes": "Direct functional replacement.",
                    }
                ],
                "comparison_summary": "Same DN25 connection and 230VAC coil class.",
                "sources": ["aimfluid.nl"],
            },
            quoted_replacement={"article_id": "00221858", "type": "6281"},
        )
        self.assertIn("REQUESTED PART (OBSOLETE): 00134328", summary)
        self.assertIn("RECOMMENDED REPLACEMENT (QUOTED): 00221858", summary)
        self.assertIn("Valve type", summary)
        self.assertIn("Connection size", summary)
        self.assertIn("COMPARISON SUMMARY", summary)
        self.assertIn("Direct functional replacement", summary)

    @patch.object(obs, "copilot_burkert_obsolete_lookup")
    def test_replacement_quote_from_price_list(self, mock_copilot):
        mock_copilot.return_value = {
            "is_obsolete": True,
            "original_part": "00134328",
            "original_specifications": {
                "title": "Type 5281",
                "specs": [{"label": "Connection size", "value": "DN25"}],
            },
            "replacement_parts": [{"article_id": "00221858", "type": "6281"}],
            "sources": ["aimfluid.nl"],
            "confidence": "high",
            "raw_text": "",
        }
        row, info = obs.try_burkert_replacement_quote("00134328", qty=1)
        self.assertIsNotNone(row)
        self.assertEqual(row.get("replacement_part"), "00221858")
        self.assertNotEqual(row.get("price"), "[TBC]")
        self.assertIn("REQUESTED PART", row.get("obsolete_research", ""))
        self.assertIn("RECOMMENDED REPLACEMENT", row.get("obsolete_research", ""))


if __name__ == "__main__":
    unittest.main()
