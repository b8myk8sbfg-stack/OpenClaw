import unittest

from product_verification import (
    enrich_item_catalog_links,
    guess_smc_series_list_slug,
    resolve_burkert_official_links,
    resolve_smc_official_links,
)


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

    def test_smc_official_detail_link(self):
        links = resolve_smc_official_links("C96SDB40-50C")
        self.assertEqual(
            links["product_page_url"],
            "https://www.smcworld.com/webcatalog/s3s/en-my/detail/?partNumber=C96SDB40-50C",
        )
        self.assertEqual(links["match_confidence"], "Exact Match")
        self.assertNotIn("/search/?q=", links["product_page_url"])

    def test_smc_series_list_slug(self):
        self.assertEqual(guess_smc_series_list_slug("C96SDB40-50C"), "C96-C96SD-2-E")

    def test_enrich_smc_item_sets_detail_url(self):
        item = {"brand": "SMC", "part_no": "C96SDB40-50C", "technical_specs": ["Bore: 40mm"]}
        enrich_item_catalog_links(item, {})
        self.assertIn(
            "/webcatalog/s3s/en-my/detail/?partNumber=C96SDB40-50C",
            str(item.get("product_page_url") or ""),
        )
        self.assertIn("C96-C96SD-2-E", str(item.get("type_page_url") or ""))


if __name__ == "__main__":
    unittest.main()
