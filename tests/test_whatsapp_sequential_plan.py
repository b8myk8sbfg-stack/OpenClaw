import unittest

from whatsapp_rfq_text import (
    collect_trailing_text_rfqs,
    is_standalone_text_rfq,
    text_has_explicit_part_number,
)


class WhatsAppRfqTextTests(unittest.TestCase):
    def test_text_has_explicit_part_number_smc_hyphen_codes(self):
        self.assertTrue(
            text_has_explicit_part_number(
                "morning pls quote PART OF VACUUM SWITCH-SMC-ZS-46-5F – 4 PCS"
            )
        )
        self.assertTrue(
            text_has_explicit_part_number(
                "ADD QUOTE FILTER ELEMENT-SMC-ZFC-EL-4 (10PCS/CARD) = 20PCS"
            )
        )

    def test_is_standalone_text_rfq_add_quote_line(self):
        self.assertTrue(
            is_standalone_text_rfq(
                "ADD QUOTE FILTER ELEMENT-SMC-ZFC-EL-4 (10PCS/CARD) = 20PCS"
            )
        )

    def test_collect_trailing_text_rfqs_back_to_back(self):
        units = [
            {"kind": "text", "text": "morning pls quote SMC-ZS-46-5F – 4 PCS", "data_id": "a1"},
            {
                "kind": "text",
                "text": "ADD QUOTE FILTER ELEMENT-SMC-ZFC-EL-4 (10PCS/CARD) = 20PCS",
                "data_id": "a2",
            },
        ]
        trailing = collect_trailing_text_rfqs(units)
        self.assertEqual(len(trailing), 2)
        self.assertEqual(trailing[0]["data_id"], "a1")


if __name__ == "__main__":
    unittest.main()
