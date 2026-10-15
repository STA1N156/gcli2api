import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from src.api.utils import collect_streaming_response
from src.converter.anthropic2gemini import (
    gemini_stream_to_anthropic_stream,
    gemini_to_anthropic_response,
)
from src.converter.openai2gemini import (
    convert_gemini_to_openai_response,
    convert_gemini_to_openai_stream,
)
from src.router.anti_truncation import collect_anti_truncation_response


REQUEST_MODEL = "gemini-3.8-flash-high"
UPSTREAM_MODEL = "gemini-3.8-flash-actual-version"


def sse(data):
    return f"data: {json.dumps(data)}\n\n".encode()


def response():
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]},
                            "finishReason": "STOP"}]}


class ResponseModelTests(unittest.IsolatedAsyncioTestCase):
    def test_converters_prefer_upstream_version_and_fall_back(self):
        for version in (UPSTREAM_MODEL, None, ""):
            for wrapped in (False, True):
                with self.subTest(version=version, wrapped=wrapped):
                    data = {**response(), "modelVersion": version}
                    if wrapped:
                        data = {"response": data}
                    original = copy.deepcopy(data)
                    expected = version or REQUEST_MODEL
                    self.assertEqual(convert_gemini_to_openai_response(data, REQUEST_MODEL)["model"], expected)
                    self.assertEqual(gemini_to_anthropic_response(data, REQUEST_MODEL)["model"], expected)
                    stream = convert_gemini_to_openai_stream(sse(data), REQUEST_MODEL, "test")
                    self.assertEqual(json.loads(stream[6:])["model"], expected)
                    self.assertEqual(data, original)

    def test_openai_stream_keeps_version_without_sharing_between_requests(self):
        state = {}
        for version, expected in ((None, REQUEST_MODEL), (UPSTREAM_MODEL, UPSTREAM_MODEL),
                                  (None, UPSTREAM_MODEL), ("", UPSTREAM_MODEL)):
            data = {**response(), "modelVersion": version}
            result = convert_gemini_to_openai_stream(sse(data), REQUEST_MODEL, "test", model_state=state)
            self.assertEqual(json.loads(result[6:])["model"], expected)
        result = convert_gemini_to_openai_stream(sse(response()), REQUEST_MODEL, "new", model_state={})
        self.assertEqual(json.loads(result[6:])["model"], REQUEST_MODEL)

    async def test_collectors_keep_metadata_only_chunks(self):
        for anti in (False, True):
            for wrapped in (False, True):
                for version in (UPSTREAM_MODEL, None):
                    with self.subTest(anti=anti, wrapped=wrapped, version=version):
                        data = response()
                        if anti:
                            data["candidates"][0]["content"]["parts"] = [{
                                "functionCall": {"name": "output_reply", "args": {"content": "OK"}},
                            }]

                        async def upstream(**kwargs):
                            chunks = [
                                {"modelVersion": version}, data,
                                {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2,
                                                   "totalTokenCount": 7}},
                            ]
                            for chunk in chunks:
                                yield sse({"response": chunk} if wrapped else chunk)
                            yield b"data: [DONE]\n\n"

                        with patch("src.api.utils.get_empty_output_error_enabled", new=AsyncMock(return_value=True)):
                            if anti:
                                result = await collect_anti_truncation_response({
                                    "model": REQUEST_MODEL,
                                    "request": {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
                                }, upstream)
                            else:
                                result = await collect_streaming_response(upstream())
                        self.assertEqual(result.status_code, 200)
                        collected = json.loads(result.body)
                        self.assertEqual(collected.get("modelVersion"), version)
                        self.assertEqual(collected["usageMetadata"]["totalTokenCount"], 7)
                        self.assertEqual(collected["candidates"][0]["content"]["parts"], [{"text": "OK"}])
                        self.assertEqual(convert_gemini_to_openai_response(collected, REQUEST_MODEL)["model"],
                                         version or REQUEST_MODEL)

    async def test_anthropic_stream_uses_upstream_version(self):
        for version in (UPSTREAM_MODEL, None):
            with self.subTest(version=version):
                async def upstream():
                    yield sse({"response": {"modelVersion": version}})
                    yield sse({"response": response()})
                    yield b"data: [DONE]\n\n"

                events = [chunk async for chunk in gemini_stream_to_anthropic_stream(upstream(), REQUEST_MODEL)]
                start = next(chunk for chunk in events if chunk.startswith(b"event: message_start\n"))
                message = json.loads(start.split(b"data: ", 1)[1])["message"]
                self.assertEqual(message["model"], version or REQUEST_MODEL)

    def test_error_responses_are_unchanged(self):
        error = {"error": {"message": "upstream error", "code": 400}}
        self.assertIs(convert_gemini_to_openai_response(error, REQUEST_MODEL, 400), error)
        self.assertIs(gemini_to_anthropic_response(error, REQUEST_MODEL, 400), error)


if __name__ == "__main__":
    unittest.main()
