"""OBM SMC portal price enrichment before CreateQuotation."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from obm_quotation_helper import enrich_smc_item_from_portal


class ObmSmcEnrichTests(unittest.TestCase):
    def test_skips_when_price_already_set(self):
        item = {
            "brand": "SMC",
            "customer_part": "AS2201F-01-04SA",
            "qty": 2,
            "price": 12.5,
            "desc": "SMC AS2201F-01-04SA",
        }
        with patch("smc_portal_lookup.lookup_smc_quote") as lookup:
            result = enrich_smc_item_from_portal(dict(item))
            lookup.assert_not_called()
        self.assertEqual(result["price"], 12.5)

    def test_enriches_zero_price_from_portal(self):
        item = {
            "brand": "SMC",
            "customer_part": "AS2201F-01-04SA",
            "qty": 2,
            "price": 0.0,
            "desc": "SMC AS2201F-01-04SA",
        }
        with patch(
            "smc_portal_lookup.lookup_smc_quote",
            return_value={
                "price": "31.50",
                "desc": "SMC AS2201F-01-04SA (SMC SPEED CONTROLLER)",
                "lt": "4-6 weeks",
            },
        ):
            result = enrich_smc_item_from_portal(dict(item))
        self.assertEqual(result["price"], 31.5)
        self.assertIn("SPEED CONTROLLER", result["desc"])


if __name__ == "__main__":
    unittest.main()
