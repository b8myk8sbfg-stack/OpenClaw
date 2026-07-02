"""Tests for OpenAI-compatible audio transcription endpoint."""

import io
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api import app


class AudioTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_rejects_empty_upload(self):
        response = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("voice.ogg", io.BytesIO(b""), "audio/ogg")},
            data={"model": "whisper-1"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("server.api.transcribe_audio_bytes", return_value="quote me two pieces")
    def test_returns_openai_shape(self, transcribe):
        response = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("voice.ogg", io.BytesIO(b"fake-audio"), "audio/ogg")},
            data={"model": "whisper-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "quote me two pieces"})
        transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
