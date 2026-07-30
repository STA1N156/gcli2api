import unittest

from src.converter.openai2gemini import convert_openai_to_gemini_request


class OpenAIToolConversionTests(unittest.IsolatedAsyncioTestCase):
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
