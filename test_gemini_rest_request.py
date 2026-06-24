import unittest
from pathlib import Path

from src.router.gemini_rest_request import (
    GeminiRestRequestError,
    normalize_gemini_rest_request,
)


class GeminiRestRequestNormalizationTests(unittest.TestCase):
    def test_keeps_direct_gemini_request_body(self):
        body = {
            "contents": [
                {"role": "user", "parts": [{"text": "hello"}]},
            ],
            "generationConfig": {"temperature": 0.2},
        }

        normalized = normalize_gemini_rest_request(body)

        self.assertEqual(normalized["contents"], body["contents"])
        self.assertEqual(normalized["generationConfig"], body["generationConfig"])

    def test_unwraps_generate_content_request_body(self):
        body = {
            "generateContentRequest": {
                "contents": [
                    {"role": "user", "parts": [{"text": "hello"}]},
                ],
                "generationConfig": {"temperature": 0.2},
            }
        }

        normalized = normalize_gemini_rest_request(body)

        self.assertNotIn("generateContentRequest", normalized)
        self.assertEqual(normalized["contents"], body["generateContentRequest"]["contents"])
        self.assertEqual(
            normalized["generationConfig"],
            body["generateContentRequest"]["generationConfig"],
        )

    def test_copies_known_top_level_fields_into_wrapped_body(self):
        body = {
            "generateContentRequest": {
                "contents": [
                    {"role": "user", "parts": [{"text": "hello"}]},
                ],
            },
            "safetySettings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT"}],
            "tools": [{"googleSearch": {}}],
        }

        normalized = normalize_gemini_rest_request(body)

        self.assertEqual(normalized["safetySettings"], body["safetySettings"])
        self.assertEqual(normalized["tools"], body["tools"])

    def test_defaults_missing_content_role_to_user(self):
        body = {
            "contents": [
                {"parts": [{"text": "hello"}]},
            ],
        }

        normalized = normalize_gemini_rest_request(body)

        self.assertEqual(normalized["contents"][0]["role"], "user")
        self.assertEqual(normalized["contents"][0]["parts"], body["contents"][0]["parts"])

    def test_rejects_missing_contents_locally(self):
        with self.assertRaises(GeminiRestRequestError):
            normalize_gemini_rest_request({"generateContentRequest": {}})


class GeminiRestRequestIntegrationSourceTests(unittest.TestCase):
    def test_gemini_native_routers_parse_raw_request_body(self):
        router_files = [
            "src/router/geminicli/gemini.py",
            "src/router/antigravity/gemini.py",
            "src/router/vertex/gemini.py",
        ]

        for router_file in router_files:
            with self.subTest(router_file=router_file):
                source = Path(router_file).read_text(encoding="utf-8")
                self.assertIn("normalize_gemini_rest_request", source)
                self.assertIn("await request.json()", source)


if __name__ == "__main__":
    unittest.main()
