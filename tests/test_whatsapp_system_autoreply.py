import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from whatsapp_message_classifier import (
    classify_whatsapp_message,
    is_whatsapp_business_hours_message,
)


class WhatsAppSystemAutoreplyTests(unittest.TestCase):
    def test_detects_business_hours_card(self):
        text = (
            "We're currently open\n"
            "Our business hours are:\n"
            "Thursday: 8:30 AM - 6:00 PM\n"
            "Friday: 8:30 AM - 6:00 PM\n"
            "Saturday: Closed\n"
            "Sunday: Closed\n"
            "Monday: 8:30 AM - 6:00 PM\n"
        )
        self.assertTrue(is_whatsapp_business_hours_message(text))

    def test_classifier_skips_business_hours(self):
        text = "We're currently open\nOur business hours are:\nMonday: 8:30 AM - 6:00 PM"
        result = classify_whatsapp_message(text, use_ai=False)
        self.assertEqual(result.handler, "skip")
        self.assertEqual(result.intent, "junk_ad")

    def test_does_not_flag_normal_rfq(self):
        self.assertFalse(is_whatsapp_business_hours_message("Please quote Burkert 00221858 Qty 1 pce"))


if __name__ == "__main__":
    unittest.main()
