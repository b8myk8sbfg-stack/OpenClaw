import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import supplier_whatsapp_config as cfg
import channel_router as router


class SupplierWhatsappConfigTests(unittest.TestCase):
    def test_purchasing_sender_default(self):
        with patch.dict(os.environ, {}, clear=False):
            phone = cfg.get_purchasing_sender_phone()
            self.assertTrue(phone.endswith("27683") or len(phone) >= 10)

    def test_omron_uses_purchasing_whatsapp(self):
        self.assertTrue(cfg.uses_purchasing_whatsapp("OMRON"))
        self.assertFalse(cfg.uses_purchasing_whatsapp("FESTO"))
        self.assertFalse(cfg.uses_purchasing_whatsapp("SMC"))

    def test_external_supplier_from_env(self):
        with patch.dict(os.environ, {"OPENCLAW_OMRON_SUPPLIER_WHATSAPP": "+60111222333"}):
            dest = cfg.get_supplier_destination("OMRON")
            self.assertIsNotNone(dest)
            self.assertEqual(dest.kind, "phone")
            self.assertEqual(dest.value, "60111222333")

    def test_omron_group_from_env(self):
        with patch.dict(os.environ, {"OPENCLAW_OMRON_SUPPLIER_WHATSAPP_GROUP": "RoboJ + SKU"}, clear=False):
            for key in list(os.environ):
                if key == "OPENCLAW_OMRON_SUPPLIER_WHATSAPP":
                    del os.environ[key]
            dest = cfg.get_supplier_destination("OMRON")
            self.assertIsNotNone(dest)
            self.assertEqual(dest.kind, "group")
            self.assertEqual(dest.value, "RoboJ + SKU")

    def test_festo_phone_from_json(self):
        with patch.object(cfg, "CONFIG_PATH", "/Users/evon/OpenClaw/supplier_whatsapp_numbers.json"):
            dest = cfg.get_supplier_destination("FESTO")
            self.assertIsNone(dest)


class ChannelRouterPurchasingTests(unittest.TestCase):
    def test_omron_channel_is_purchasing_whatsapp(self):
        self.assertEqual(router.get_supplier_channel("OMRON"), "PURCHASING_WHATSAPP")

    def test_festo_channel_is_email(self):
        self.assertEqual(router.get_supplier_channel("FESTO"), "EMAIL")
        self.assertEqual(router.get_supplier_email("FESTO"), "siuw@jsautomation.com.my")

    def test_piab_channel_is_email(self):
        self.assertEqual(router.get_supplier_channel("PIAB"), "EMAIL")
        self.assertEqual(router.get_supplier_email("PIAB"), "Ang.SengGuan@piabgroup.com")

    def test_smc_stays_email(self):
        self.assertEqual(router.get_supplier_channel("SMC"), "EMAIL")


if __name__ == "__main__":
    unittest.main()
