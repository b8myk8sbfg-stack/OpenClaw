import unittest

from product_verification import enrich_item_catalog_links, resolve_burkert_official_links


class ProductVerificationTests(unittest.TestCase):
    def test_burkert_official_links_by_article_id(self):
        links = resolve_burkert_official_links(
            article_id="00132465",
            part_no="6519 H 8.0",
            technical_specs=[
                "Model: 6519 H 8.0",
                "Coil voltage: 24V DC",
                "Power: 2W",
                "Pressure: PN2 - 8bar",
            ],
        )
        self.assertEqual(links["product_page_url"], "https://www.burkert.com/en/item/132465")
        self.assertEqual(
            links["datasheet_url"],
            "https://www.burkert.com/en/Media/plm/DTS/DS/ds6519-standard-eu-en.pdf",
        )
        self.assertEqual(links["match_confidence"], "Exact Match")
        self.assertEqual(links["pdf_status"], "Direct PDF")

    def test_enrich_item_sets_catalog_urls(self):
        item = {
            "brand": "burkert",
            "part_no": "6519 H 8.0",
            "burkert_id": "00132465",
            "technical_specs": ["Coil voltage: 24V DC"],
        }
        row = {"brand": "BURKERT", "burkert_id": "00132465", "pid": "132465"}
        enrich_item_catalog_links(item, row)
        self.assertTrue(str(item.get("product_page_url") or "").startswith("https://www.burkert.com/en/item/"))
        self.assertIn("ds6519-standard-eu-en.pdf", str(item.get("datasheet_url") or ""))


if __name__ == "__main__":
    unittest.main()
