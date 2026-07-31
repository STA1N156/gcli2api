import unittest

import config
from src.api.antigravity import wrap_cli_request
from src.converter.gemini_fix import normalize_gemini_request
from src.converter.openai2gemini import convert_openai_to_gemini_request


class OpenAIToolConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rp_hub_style_gemini_payload_matches_cliproxy_final_rules(self):
        request = {
            "model": "gemini-3.1-pro-preview",
            "temperature": 1,
            "messages": [
                {"role": "system", "content": "character and world info " * 500},
                {"role": "user", "name": "user", "content": "first message"},
                {"role": "assistant", "name": "character", "content": "reply"},
                {"role": "system", "content": "instructions for next message"},
                {"role": "user", "name": "user", "content": "continue"},
            ],
        }
        old_return_thoughts = config.get_return_thoughts_to_frontend

        async def disabled():
            return False

        config.get_return_thoughts_to_frontend = disabled
        try:
            converted = await convert_openai_to_gemini_request(request)
            converted["model"] = request["model"]
            normalized = await normalize_gemini_request(
                converted, mode="antigravity"
            )
        finally:
            config.get_return_thoughts_to_frontend = old_return_thoughts

        model = normalized.pop("model")
        final_payload, _ = await wrap_cli_request(normalized, model, "project")

        self.assertNotIn("enabledCreditTypes", final_payload)
        self.assertEqual(
            final_payload["request"]["generationConfig"]["maxOutputTokens"],
            64000,
        )
        self.assertNotIn("toolConfig", final_payload["request"])
        self.assertNotIn("safetySettings", final_payload["request"])
        self.assertNotIn(
            "role", final_payload["request"]["systemInstruction"]
        )

    async def test_all_system_messages_become_one_system_instruction(self):
        converted = await convert_openai_to_gemini_request(
            {
                "messages": [
                    {"role": "system", "content": "first"},
                    {"role": "user", "content": "hello"},
                    {"role": "system", "content": "later"},
                    {"role": "developer", "content": "developer"},
                ]
            }
        )

        self.assertEqual(
            converted["systemInstruction"]["parts"],
            [{"text": "first"}, {"text": "later"}, {"text": "developer"}],
        )
        self.assertEqual(
            converted["contents"],
            [{"role": "user", "parts": [{"text": "hello"}]}],
        )

    async def test_tool_call_text_blocks_remain_plain_strings(self):
        converted = await convert_openai_to_gemini_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hello"}],
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "test_tool", "arguments": "{}"},
                            }
                        ],
                    }
                ]
            }
        )

        text_part = converted["contents"][0]["parts"][0]
        self.assertEqual(text_part, {"text": "hello"})
        self.assertIsInstance(text_part["text"], str)


if __name__ == "__main__":
    unittest.main()
