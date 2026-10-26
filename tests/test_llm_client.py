"""The live API path, with the network faked: request shape, JSON parsing, cache."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import llm  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LiveClient(unittest.TestCase):
    def test_request_parse_and_cache(self):
        sent = {}

        def fake_urlopen(req, timeout):
            sent["url"] = req.full_url
            sent["headers"] = dict(req.header_items())
            sent["body"] = json.loads(req.data)
            text = 'Here you go:\n```json\n{"items": [{"number": "2", "category": "say_on_pay"}]}\n```'
            return FakeResp(json.dumps({"content": [{"type": "text", "text": text}]}).encode())

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "CACHE_DIR", Path(tmp)), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            data, err = llm.ask_json("sys", "user prompt")
            self.assertIsNone(err)
            self.assertEqual(data["items"][0]["category"], "say_on_pay")
            self.assertEqual(sent["url"], llm.API_URL)
            self.assertEqual(sent["headers"]["X-api-key"], "test-key")
            self.assertEqual(sent["body"]["messages"][0]["content"], "user prompt")
            self.assertEqual(sent["body"]["temperature"], 0)
            # Second call is served from the cache, no network.
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                again, _ = llm.ask_json("sys", "user prompt")
            self.assertEqual(again, data)

    def test_no_key_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(llm, "CACHE_DIR", Path(tmp)), \
                mock.patch.dict(os.environ, {}, clear=True):
            data, err = llm.ask_json("s", "u")
        self.assertIsNone(data)
        self.assertIn("ANTHROPIC_API_KEY", err)

    def test_mode(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True):
            self.assertEqual(llm.mode_for("starbucks-2026-def14a"), "live")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm.mode_for("starbucks-2026-def14a"), "stub")
            self.assertEqual(llm.mode_for("some-new-filing"), "off")


if __name__ == "__main__":
    unittest.main()
