import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import whatsapp_inbox_watcher as wa


class TestInquiryImageMonitor(unittest.TestCase):
    def test_image_capture_dir_defaults_to_wa_image(self):
        self.assertTrue(wa.IMAGE_CAPTURE_DIR.endswith("WA_Image"))

    def test_cleanup_deletes_after_successful_send(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = handle.name
        wa.cleanup_inquiry_image(path, sent=True)
        self.assertFalse(os.path.exists(path))

    def test_cleanup_keeps_file_when_send_failed(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = handle.name
        wa.cleanup_inquiry_image(path, sent=False)
        self.assertTrue(os.path.exists(path))
        os.remove(path)

    @patch("whatsapp_inbox_watcher.send_image_in_current_chat", return_value=True)
    @patch("whatsapp_inbox_watcher.open_whatsapp_chat_by_phone", return_value=True)
    @patch("whatsapp_inbox_watcher.get_monitor_whatsapp_phones", return_value=["+60167222208", "+60167108883"])
    def test_send_inquiry_image_to_all_monitors(self, _phones, _open_chat, _send_image):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            path = handle.name
        try:
            sent = wa.send_inquiry_image_to_monitors(driver=object(), image_path=path)
            self.assertTrue(sent)
            self.assertEqual(_open_chat.call_count, 2)
            self.assertEqual(_send_image.call_count, 2)
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
