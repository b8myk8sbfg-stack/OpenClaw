import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from markem_imaje_price_list import (
    is_markem_imaje_brand,
    load_markem_imaje_price_list,
    lookup_markem_imaje_quote,
    looks_like_markem_imaje_material,
    material_lookup_keys,
)


class MarkemImajePriceListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_markem_imaje_price_list(force=True)

    def test_loads_price_list(self):
        self.assertTrue(self.loaded)

    def test_enm_part_lookup(self):
        quote = lookup_markem_imaje_quote("ENM10053306", qty=1)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["price"], "2,837.00")
        self.assertEqual(quote["material_key"], "ENM10053306")
        self.assertNotIn("0.72", quote["price"])

    def test_material_key_variants(self):
        keys = material_lookup_keys("ENM10053306")
        self.assertIn("ENM10053306", keys)

    def test_brand_and_part_detection(self):
        self.assertTrue(is_markem_imaje_brand("MARKEM-IMAJE"))
        self.assertTrue(is_markem_imaje_brand("Markem Imaje"))
        self.assertTrue(looks_like_markem_imaje_material("ENM10053306"))

    def test_unknown_part_returns_none(self):
        quote = lookup_markem_imaje_quote("ENM99999999", qty=1)
        self.assertIsNone(quote)


if __name__ == "__main__":
    unittest.main()
