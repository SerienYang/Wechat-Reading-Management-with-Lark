import io
import http.client
import json
import os
import urllib.error
import unittest
from unittest.mock import patch

from reading_manager.llm import (
    LLMConfig,
    LLMFatalError,
    LLMRetryableError,
    OpenAICompatibleClient,
)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _ReadErrorResponse:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        raise self.error


class LLMTests(unittest.TestCase):
    def test_config_loads_defaults_and_environment(self):
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "test-secret",
                "LLM_BASE_URL": "https://llm.example.test/v1/",
                "LLM_MODEL": "test-model",
                "LLM_TIMEOUT_SECONDS": "30",
                "LLM_MAX_ATTEMPTS": "2",
            },
            clear=True,
        ):
            config = LLMConfig.from_env()

        self.assertEqual(config.api_key, "test-secret")
        self.assertEqual(config.base_url, "https://llm.example.test/v1")
        self.assertEqual(config.model, "test-model")
        self.assertEqual(config.timeout_seconds, 30)
        self.assertEqual(config.max_attempts, 2)

    def test_config_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LLMFatalError, "LLM_API_KEY"):
                LLMConfig.from_env()

    def test_config_rejects_non_https_base_url(self):
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "test-secret",
                "LLM_BASE_URL": "http://llm.example.test",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(LLMFatalError, "HTTPS"):
                LLMConfig.from_env()

    def test_config_rejects_invalid_numeric_values(self):
        cases = [
            ("LLM_TIMEOUT_SECONDS", "0"),
            ("LLM_TIMEOUT_SECONDS", "abc"),
            ("LLM_MAX_ATTEMPTS", "0"),
            ("LLM_MAX_ATTEMPTS", "4"),
            ("LLM_MAX_ATTEMPTS", "abc"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with patch.dict(
                    os.environ,
                    {"LLM_API_KEY": "test-secret", name: value},
                    clear=True,
                ):
                    with self.assertRaises(LLMFatalError):
                        LLMConfig.from_env()

    def test_complete_json_sends_deepseek_request(self):
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": '{"book_tags":[],"note_tags":[]}',
                            },
                        }
                    ]
                }
            )

        client = OpenAICompatibleClient(
            LLMConfig(
                api_key="test-secret",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                timeout_seconds=45,
                max_attempts=3,
            ),
            opener=fake_open,
        )
        content = client.complete_json("tag these notes")

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(content, '{"book_tags":[],"note_tags":[]}')
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(captured["timeout"], 45)
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": "tag these notes"},
            ],
        )
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["max_tokens"], 4096)
        self.assertFalse(body["stream"])

    def test_complete_json_classifies_retryable_http_errors(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    "https://api.deepseek.com/chat/completions",
                    status,
                    "failed",
                    {},
                    io.BytesIO(b'{"secret":"must not leak"}'),
                )
                client = self._client(opener=lambda request, timeout: (_ for _ in ()).throw(error))
                with self.assertRaisesRegex(LLMRetryableError, str(status)) as raised:
                    client.complete_json("private note")
                self.assertNotIn("private note", str(raised.exception))
                self.assertNotIn("must not leak", str(raised.exception))

    def test_complete_json_does_not_retry_authentication_errors(self):
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            401,
            "unauthorized",
            {},
            io.BytesIO(b'{"api_key":"must not leak"}'),
        )
        client = self._client(opener=lambda request, timeout: (_ for _ in ()).throw(error))

        with self.assertRaisesRegex(LLMFatalError, "401") as raised:
            client.complete_json("private note")

        self.assertNotIn("private note", str(raised.exception))
        self.assertNotIn("must not leak", str(raised.exception))

    def test_complete_json_classifies_network_errors_as_retryable(self):
        error = urllib.error.URLError("connection reset")
        client = self._client(opener=lambda request, timeout: (_ for _ in ()).throw(error))

        with self.assertRaises(LLMRetryableError):
            client.complete_json("private note")

    def test_complete_json_classifies_response_read_errors_as_retryable(self):
        errors = [
            ConnectionResetError("connection reset"),
            http.client.IncompleteRead(b"partial"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client = self._client(
                    opener=lambda request, timeout: _ReadErrorResponse(error)
                )
                with self.assertRaises(LLMRetryableError):
                    client.complete_json("private note")

    def test_complete_json_rejects_empty_or_truncated_content(self):
        payloads = [
            {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"book_tags":'},
                    }
                ]
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = self._client(opener=lambda request, timeout: _FakeResponse(payload))
                with self.assertRaises(LLMRetryableError):
                    client.complete_json("private note")

    def test_complete_json_retries_malformed_envelope(self):
        client = self._client(
            opener=lambda request, timeout: _FakeResponse({"choices": []})
        )

        with self.assertRaises(LLMRetryableError):
            client.complete_json("private note")

    @staticmethod
    def _client(opener):
        return OpenAICompatibleClient(
            LLMConfig(
                api_key="test-secret",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                timeout_seconds=45,
                max_attempts=3,
            ),
            opener=opener,
        )


if __name__ == "__main__":
    unittest.main()
