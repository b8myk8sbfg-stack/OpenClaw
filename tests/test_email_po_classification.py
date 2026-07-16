"""Tests: email PO must not enter RFQ inquiry extraction."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from email_message_classifier import (
    classify_email,
    is_email_rfq_inquiry,
    _looks_like_purchase_order,
)


class EmailPoClassificationTests(unittest.TestCase):
    def test_amended_po_subject_detected(self):
        self.assertTrue(
            _looks_like_purchase_order(
                "RE: AMENDED PO: M177514 - from GG Circuits ( to ROBOMATICS (JOHOR) )",
                "",
            )
        )

    @patch("email_message_classifier.record_classification_example")
    def test_gg_circuits_amended_po_not_rfq(self, _record):
        subject = "RE: AMENDED PO: M177514 - from GG Circuits ( to ROBOMATICS (JOHOR) )"
        body = (
            "AMENDED PO TO: ROBOMATICS (JOHOR) SDN BHD **IMPORTANT NOTICE** "
            "Please acknowledge receiving of this PO, and REPLY US the status of this PO "
            "via email. If delivery of goods above cannot be made as per our request date, "
            "please advise ETA ASAP. Thanks Best Regards, Alan Purchasing Department"
        )
        result = classify_email(
            "purchasing@ggcircuits.com",
            subject,
            body,
            use_ai=False,
        )
        self.assertEqual(result.intent, "purchase_order")
        self.assertEqual(result.handler, "purchase_order")
        self.assertFalse(is_email_rfq_inquiry(result))

    @patch("email_message_classifier.record_classification_example")
    def test_real_rfq_still_inquiry(self, _record):
        result = classify_email(
            "buyer@customer.com",
            "RFQ - SMC parts",
            "Please quote SMC AS1002F-04 Qty : 2 pcs Thank you.",
            use_ai=False,
        )
        self.assertEqual(result.intent, "rfq_inquiry")
        self.assertTrue(is_email_rfq_inquiry(result))


if __name__ == "__main__":
    unittest.main()
