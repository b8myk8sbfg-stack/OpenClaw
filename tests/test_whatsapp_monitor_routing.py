import os
import unittest
from unittest.mock import patch

import whatsapp_inbox_watcher as wa


class TestMonitorWhatsAppPhones(unittest.TestCase):
    def test_defaults_include_stephen_and_annie(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONES", None)
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONE", None)
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONE_2", None)
            phones = wa.get_monitor_whatsapp_phones()
        self.assertEqual(
            [wa.normalize_phone(p) for p in phones],
            ["60167222208", "60167108883"],
        )

    def test_comma_list_env(self):
        with patch.dict(
            os.environ,
            {"OPENCLAW_MONITOR_WHATSAPP_PHONES": "+60167222208,+60167108883"},
            clear=False,
        ):
            phones = wa.get_monitor_whatsapp_phones()
        self.assertEqual(len(phones), 2)

    def test_is_monitor_phone_matches_both_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONES", None)
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONE", None)
            os.environ.pop("OPENCLAW_MONITOR_WHATSAPP_PHONE_2", None)
            self.assertTrue(wa.is_monitor_phone("+60167222208"))
            self.assertTrue(wa.is_monitor_phone("+60167108883"))
            self.assertFalse(wa.is_monitor_phone("+60123456789"))


if __name__ == "__main__":
    unittest.main()
