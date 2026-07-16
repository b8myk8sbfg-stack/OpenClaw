import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import channel_router as router
import supplier_rfq_pricing as pricing


class SupplierRfqTemplateTests(unittest.TestCase):
    def test_external_rfq_hides_customer(self):
        text = router.build_supplier_rfq_text(
            "PIAB",
            [{"desc": "PIAB 31.16.671", "qty": 1}],
            "WA-20260716-PIAB-UL92",
            customer_name="+60 13-288 9210",
            customer_contact="+60 13-288 9210",
            include_customer_info=False,
        )
        self.assertNotIn("Customer:", text)
        self.assertNotIn("Customer Contact:", text)
        self.assertIn("Ref: WA-20260716-PIAB-UL92", text)
        self.assertIn("[REPLY FORMAT - PLEASE COPY & FILL]", text)
        self.assertIn("SGD", text)

    def test_internal_rfq_includes_customer(self):
        text = router.build_supplier_rfq_text(
            "PIAB",
            [{"desc": "PIAB 31.16.671", "qty": 1}],
            "WA-20260716-PIAB-UL92",
            customer_name="+60 13-288 9210",
            customer_contact="+60 13-288 9210",
            include_customer_info=True,
        )
        self.assertIn("Customer: +60 13-288 9210", text)
        self.assertIn("Customer Contact: +60 13-288 9210", text)

    def test_reply_format_includes_ref(self):
        text = router.build_supplier_rfq_text(
            "FESTO",
            [{"desc": "FESTO 530031", "qty": 3}],
            "WA-20260716-FESTO-AB12",
            include_customer_info=False,
        )
        reply_start = text.index("[REPLY FORMAT - PLEASE COPY & FILL]")
        self.assertIn("Ref: WA-20260716-FESTO-AB12", text[reply_start:])


class PiabPricingTests(unittest.TestCase):
    def test_piab_sgd_pricing_example(self):
        unit = pricing.calc_piab_unit_sell_rm(78.44, exchange_rate=3.16)
        self.assertAlmostEqual(unit, 378.69, places=1)

    def test_piab_reply_parser(self):
        section = """
Ref: WA-20260716-PIAB-UL92
1) Piab vacuum filter (31.16.671)
Qty: 1
Price: SGD78.44net/unit
Lead Time: 1 weeks
"""
        with mock.patch.object(pricing, "get_sgd_to_rm_rate", return_value=3.16):
            items = pricing.parse_supplier_reply_items(section, brand="PIAB")
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["customer_unit_price"], 378.69, places=1)

    def test_customer_update_adds_piab_courier(self):
        with mock.patch.object(pricing, "get_sgd_to_rm_rate", return_value=3.16):
            items = pricing.parse_supplier_reply_items(
                "1) Piab filter\nQty: 1\nPrice: SGD78.44\nLead Time: 1 week",
                brand="PIAB",
            )
        msg = pricing.build_customer_update_from_supplier("WA-1", "PIAB", items)
        self.assertIn("Transport & Courier: RM 300.00", msg)


class ExternalSupplierRoutingTests(unittest.TestCase):
    def test_piab_and_festo_are_external(self):
        self.assertTrue(router.is_external_supplier("PIAB"))
        self.assertTrue(router.is_external_supplier("FESTO"))
        self.assertTrue(router.is_external_supplier("OMRON"))

    def test_smc_is_internal(self):
        self.assertFalse(router.is_external_supplier("SMC"))


if __name__ == "__main__":
    unittest.main()
