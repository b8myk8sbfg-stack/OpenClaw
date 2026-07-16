import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import parker_price_list as parker


class ParkerPriceListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_exists = parker.ensure_parker_database()

    def test_database_ready(self):
        self.assertTrue(self.db_exists, "Run scripts/build_parker_price_db.py first")

    def test_tube_fitting_quote(self):
        quote = parker.lookup_parker_quote("APB14571X", qty=1, brand="LEGRIS")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["list_price"], 108.6)
        self.assertEqual(quote["discount_pct"], 75.0)
        self.assertAlmostEqual(quote["nett_price"], 27.15, places=2)
        self.assertEqual(quote["price"], "37.71")
        self.assertEqual(quote["lt"], "12-14 weeks")

    def test_parflex_quote(self):
        quote = parker.lookup_parker_quote("E-43-0500", qty=1)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["prod_code"], "8C-PF01")
        self.assertEqual(quote["family_brand"], "PARFLEX")
        self.assertEqual(quote["price"], "5.00")

    def test_parker_family_brands(self):
        self.assertTrue(parker.is_parker_family_brand("LEGRIS"))
        self.assertTrue(parker.is_parker_family_brand("PARKER"))
        self.assertFalse(parker.is_parker_family_brand("SMC"))

    def test_calc_pricing(self):
        nett, sell = parker.calc_parker_sell_price(100.0, 75.0)
        self.assertAlmostEqual(nett, 25.0, places=2)
        self.assertAlmostEqual(sell, 25.0 / 0.72, places=2)


if __name__ == "__main__":
    unittest.main()
