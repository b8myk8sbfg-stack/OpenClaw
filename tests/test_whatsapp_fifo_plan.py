"""Unit tests for WhatsApp FIFO text-burst planning (no Selenium)."""

import unittest
from unittest.mock import patch

from whatsapp_inbox_watcher import plan_sequential_units


def _text_unit(text, data_id=""):
    return {"kind": "text", "text": text, "data_id": data_id}


class WhatsAppFifoPlanTests(unittest.TestCase):
    @patch("whatsapp_inbox_watcher.filter_processable_units", side_effect=lambda units, _name="": units)
    def test_text_burst_returns_all_messages_fifo(self, _mock_filter):
        units = [
            _text_unit(
                "Good afternoon\nPlease quote 1 unit\nOMRON CPU UNIT\nMODEL: CP2E-N60DT-A",
                "msg1",
            ),
            _text_unit("From zhong zhong electric\nMr chong", "msg2"),
        ]
        plan = plan_sequential_units(units, "Zhong2 Electric")
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["data_id"], "msg1")
        self.assertEqual(plan[1]["data_id"], "msg2")
        self.assertIn("CP2E-N60DT-A", plan[0]["text"])

    @patch("whatsapp_inbox_watcher.filter_processable_units", side_effect=lambda units, _name="": units)
    def test_single_text_still_returns_one(self, _mock_filter):
        units = [_text_unit("Please quote MXQ8-20 qty 2", "only")]
        plan = plan_sequential_units(units, "Customer")
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["data_id"], "only")

    @patch("whatsapp_inbox_watcher.filter_processable_units", side_effect=lambda units, _name="": units)
    def test_image_then_text_pair_unchanged(self, _mock_filter):
        units = [
            {"kind": "image", "text": "", "data_id": "img1"},
            _text_unit("Quote 2 pcs", "cap1"),
        ]
        plan = plan_sequential_units(units, "Customer")
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["kind"], "image")
        self.assertEqual(plan[1]["kind"], "text")


if __name__ == "__main__":
    unittest.main()
