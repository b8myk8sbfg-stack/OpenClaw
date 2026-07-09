"""Unit tests for WhatsApp incoming vs outgoing bubble detection helpers."""

import re
import unittest


def is_outgoing_pre_plain(pre_plain_text):
    ppt = str(pre_plain_text or "").strip()
    return bool(re.search(r"\]\s*You:\s*$", ppt, re.I))


def is_outgoing_data_id(data_id: str) -> bool:
    return bool(re.match(r"^3EB", str(data_id or "").strip(), re.I))


class OutgoingDetectionTests(unittest.TestCase):
    def test_outgoing_pre_plain(self):
        self.assertTrue(is_outgoing_pre_plain("[10:30 AM, 1/1/2026] You:"))
        self.assertFalse(is_outgoing_pre_plain("[10:30 AM, 1/1/2026] Stephen:"))

    def test_outgoing_data_id_prefix(self):
        self.assertTrue(is_outgoing_data_id("3EB0ABCDEF"))
        self.assertFalse(is_outgoing_data_id("AABBCC"))


if __name__ == "__main__":
    unittest.main()
