"""Tests for OpenAI-compatible image input forwarding."""

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.api import chat_completions
from server.prompt import messages_to_prompt_and_image
from server.schemas import ChatCompletionRequest, ChatMessage


PNG = b"\x89PNG\r\n\x1a\n" + b"test-payload"


class ImageInputTests(unittest.TestCase):
    def _messages(self, url):
        return [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Read the part number"},
                    {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                ],
            )
        ]

    def test_decodes_data_url_and_preserves_text(self):
        encoded = base64.b64encode(PNG).decode("ascii")
        prompt, image = messages_to_prompt_and_image(
            self._messages(f"data:image/png;base64,{encoded}")
        )
        self.assertEqual(prompt, "Read the part number")
        self.assertEqual(image, PNG)

    def test_rejects_remote_url(self):
        with self.assertRaisesRegex(ValueError, "base64 PNG/JPEG"):
            messages_to_prompt_and_image(self._messages("https://example.com/image.png"))

    def test_rejects_multiple_images(self):
        encoded = base64.b64encode(PNG).decode("ascii")
        url = f"data:image/png;base64,{encoded}"
        message = self._messages(url)[0]
        message.content.append({"type": "image_url", "image_url": {"url": url}})
        with self.assertRaisesRegex(ValueError, "one input image"):
            messages_to_prompt_and_image([message])

    @patch("server.api.client.chat")
    def test_api_forwards_image_bytes_to_copilot_client(self, chat):
        chat.return_value = SimpleNamespace(
            text='[{"part_no":"MXQ12L-75","qty":1}]',
            conversation_id="conversation-1",
        )
        encoded = base64.b64encode(PNG).decode("ascii")
        request = ChatCompletionRequest(
            model="copilot",
            messages=self._messages(f"data:image/png;base64,{encoded}"),
        )

        response = chat_completions(request)

        self.assertEqual(response["choices"][0]["message"]["content"], chat.return_value.text)
        chat.assert_called_once_with(
            "Read the part number",
            conversation_id=None,
            image=PNG,
        )


if __name__ == "__main__":
    unittest.main()
