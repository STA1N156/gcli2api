import unittest

import config
from src.converter.gemini_fix import normalize_gemini_request


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


if __name__ == "__main__":
    unittest.main()
