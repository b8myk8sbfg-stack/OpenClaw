import unittest

from burkert_price_list import (
    apply_moq_to_qty,
    burkert_lookup_keys,
    burkert_type_family_key,
    burkert_id_lookup_keys,
    customer_lead_time_from_field,
    factory_days_to_customer_lead_time,
    normalize_burkert_id,
    parse_factory_days,
)


class BurkertLeadTimeMappingTests(unittest.TestCase):
    def test_parse_single_day(self):
        self.assertEqual(parse_factory_days(7), (7, 7))
        self.assertEqual(parse_factory_days("10 days"), (10, 10))

    def test_parse_range(self):
        self.assertEqual(parse_factory_days("3 to 5 days"), (3, 5))
        self.assertEqual(parse_factory_days("6-10"), (6, 10))

    def test_factory_bucket_mapping(self):
        self.assertEqual(factory_days_to_customer_lead_time(3, 5), "4-5 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(6, 10), "5-6 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(11, 20), "6-8 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(21, 50), "8-10 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(51, 100), "10-14 weeks")

    def test_single_value_uses_bucket(self):
        self.assertEqual(factory_days_to_customer_lead_time(4), "4-5 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(8), "5-6 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(15), "6-8 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(30), "8-10 weeks")
        self.assertEqual(factory_days_to_customer_lead_time(75), "10-14 weeks")

    def test_conservative_cross_bucket_range(self):
        self.assertEqual(factory_days_to_customer_lead_time(8, 12), "6-8 weeks")

    def test_below_minimum_uses_shortest_customer_lead_time(self):
        self.assertEqual(factory_days_to_customer_lead_time(1, 2), "4-5 weeks")

    def test_above_maximum_uses_longest_customer_lead_time(self):
        self.assertEqual(factory_days_to_customer_lead_time(120), "10-14 weeks")

    def test_customer_lead_time_from_field_examples(self):
        self.assertEqual(customer_lead_time_from_field("3 to 5 days"), "4-5 weeks")
        self.assertEqual(customer_lead_time_from_field("6 to 10 days"), "5-6 weeks")
        self.assertEqual(customer_lead_time_from_field("11 to 20 days"), "6-8 weeks")
        self.assertEqual(customer_lead_time_from_field("21 to 50 days"), "8-10 weeks")
        self.assertEqual(customer_lead_time_from_field("51 to 100 days"), "10-14 weeks")

    def test_nameplate_maps_to_catalog_family(self):
        keys = burkert_lookup_keys("6519 H 8.0")
        self.assertIn("6519H80", keys)
        self.assertIn("6519H08", keys)

    def test_type_family_key(self):
        self.assertEqual(
            burkert_type_family_key("6519-H08,0-GM82-B5-024/DC-02"),
            "6519H08",
        )

    def test_burkert_id_normalization(self):
        self.assertEqual(normalize_burkert_id("00132465"), "132465")
        self.assertEqual(normalize_burkert_id("132465"), "132465")
        self.assertIn("132465", burkert_id_lookup_keys("00132465"))

    def test_moq_bumps_requested_qty(self):
        quoted, applied = apply_moq_to_qty(1, 5)
        self.assertEqual(quoted, 5)
        self.assertTrue(applied)
        quoted, applied = apply_moq_to_qty(6, 5)
        self.assertEqual(quoted, 6)
        self.assertFalse(applied)
        quoted, applied = apply_moq_to_qty(2, 0)
        self.assertEqual(quoted, 2)
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main()
