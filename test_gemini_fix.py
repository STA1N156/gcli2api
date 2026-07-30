import unittest

import config
from src.converter.gemini_fix import (
    DEFAULT_SAFETY_SETTINGS,
    _ensure_empty_tool_schema_for_claude,
    normalize_gemini_request,
)


class GeminiThinkingConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_return_thoughts = config.get_return_thoughts_to_frontend

        async def enabled():
            return True

        config.get_return_thoughts_to_frontend = enabled

    async def asyncTearDown(self):
        config.get_return_thoughts_to_frontend = self._old_return_thoughts

    async def test_removes_include_thoughts_when_thinking_is_not_enabled(self):
        request = {
            "model": "gemini-2.5-pro",
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {},
        }

        normalized = await normalize_gemini_request(request, mode="geminicli")

        thinking_config = normalized["generationConfig"].get("thinkingConfig", {})
        self.assertNotEqual(thinking_config.get("includeThoughts"), True)

    async def test_keeps_include_thoughts_when_thinking_budget_is_enabled(self):
        request = {
            "model": "gemini-2.5-pro-low",
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {},
        }

        normalized = await normalize_gemini_request(request, mode="geminicli")

        thinking_config = normalized["generationConfig"]["thinkingConfig"]
        self.assertEqual(thinking_config["thinkingBudget"], 1024)
        self.assertIs(thinking_config["includeThoughts"], True)


class UpstreamConverterRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_antigravity_claude_tools_use_parameters(self):
        tools = [
            {
                "functionDeclarations": [
                    {
                        "name": "test_tool",
                        "parametersJsonSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    }
                ]
            }
        ]

        result = _ensure_empty_tool_schema_for_claude(
            tools, "claude-opus-4-6-thinking", "antigravity"
        )
        declaration = result[0]["functionDeclarations"][0]

        self.assertEqual(declaration["parameters"]["type"], "object")
        self.assertNotIn("parametersJsonSchema", declaration)

    async def test_default_safety_settings_include_image_categories(self):
        old_return_thoughts = config.get_return_thoughts_to_frontend

        async def disabled():
            return False

        config.get_return_thoughts_to_frontend = disabled
        try:
            normalized = await normalize_gemini_request(
                {
                    "model": "gemini-3-flash",
                    "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                },
                mode="geminicli",
            )
        finally:
            config.get_return_thoughts_to_frontend = old_return_thoughts

        categories = {item["category"] for item in normalized["safetySettings"]}
        self.assertEqual(normalized["safetySettings"], DEFAULT_SAFETY_SETTINGS)
        self.assertTrue(
            {
                "HARM_CATEGORY_IMAGE_HATE",
                "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT",
                "HARM_CATEGORY_IMAGE_HARASSMENT",
                "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_JAILBREAK",
            }.issubset(categories)
        )


if __name__ == "__main__":
    unittest.main()
