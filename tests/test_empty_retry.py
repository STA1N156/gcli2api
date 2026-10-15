import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Response
from fastapi.responses import JSONResponse

from src.api import antigravity
from src.api.empty_retry import empty_output_stream, prepare_empty_retry, retry_empty_response
from src.converter.anti_truncation import (
    REPLY_TOOL_INSTRUCTION, apply_anti_truncation, latest_user_text_parts, move_system_to_first_user,
)
from src.router.anti_truncation import anti_truncation_gemini_stream


def payload(model="gemini-4-argon-high"):
    return {"model": model, "request": {
        "systemInstruction": {"parts": [{"text": "System instruction"}]},
        "contents": [{"role": "user", "parts": [{"text": "你好"}]}],
    }}


def reply(parts):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}


def sse(data):
    return f"data: {json.dumps(data)}\n\n".encode()


class EmptyRetryTests(unittest.IsolatedAsyncioTestCase):
    def test_existing_unicode_spans_are_preserved(self):
        cases = {
            "⟦U:4F60 597D⟧": "⟦U:4F60 597D⟧",
            "前⟦U:4F60 597D⟧后": "⟦U:524D⟧⟦U:4F60 597D⟧⟦U:540E⟧",
            "⟦U:4f60⟧ ⟦U:1F600 10FFFF⟧": "⟦U:4f60⟧⟦U:0020⟧⟦U:1F600 10FFFF⟧",
            "⟦U:000A\t597D⟧": "⟦U:000A\t597D⟧",
            "": "",
        }
        for invalid in ("⟦U:⟧", "⟦U:XYZ⟧", "⟦U:110000⟧", "⟦U:D800⟧", "⟦U:4F60 110000⟧"):
            cases[invalid] = "⟦U:" + " ".join(f"{ord(char):04X}" for char in invalid) + "⟧"
        for text, expected in cases.items():
            with self.subTest(text=text):
                body = payload()
                body["request"]["contents"][0]["parts"][0]["text"] = text
                for _ in range(2):
                    prepare_empty_retry(body, latest_user_text_parts(body["request"]))
                    self.assertEqual(body["request"]["contents"][0]["parts"][0]["text"], expected)

    def test_encoding_preserves_history_images_tools_and_moved_system(self):
        body = payload()
        contents = body["request"]["contents"]
        contents.insert(0, {"role": "user", "parts": [{"text": "History"}]})
        contents[-1]["parts"].extend([
            {"inlineData": {"mimeType": "image/png", "data": "test"}},
            {"text": "\n😀"},
        ])
        contents.append({"role": "user", "parts": [{"functionResponse": {"name": "lookup"}}]})
        original = copy.deepcopy(body)
        body = apply_anti_truncation(body)
        parts = latest_user_text_parts(body["request"])
        body["request"] = move_system_to_first_user(body["request"])
        prepare_empty_retry(body, parts)
        result = body["request"]["contents"]
        self.assertEqual(result[0]["parts"][0]["text"], "System instruction")
        self.assertEqual(result[0]["parts"][-1]["text"], "History")
        self.assertEqual(result[1]["parts"][0]["text"], REPLY_TOOL_INSTRUCTION + "\n")
        self.assertEqual(result[1]["parts"][1]["text"], "⟦U:4F60 597D⟧")
        self.assertEqual(result[1]["parts"][2], original["request"]["contents"][1]["parts"][1])
        self.assertEqual(result[1]["parts"][3]["text"], "⟦U:000A 1F600⟧")
        self.assertEqual(result[-1], original["request"]["contents"][-1])
        self.assertNotIn("systemInstruction", body["request"])
        self.assertEqual(original["request"]["contents"][1]["parts"][0]["text"], "你好")

    async def test_streams_try_only_twice_and_leave_other_errors_alone(self):
        for run in (empty_output_stream, anti_truncation_gemini_stream):
            for last in (reply([]), reply([{"text": "OK"}]), Response(status_code=400)):
                with self.subTest(run=run.__name__, last=last):
                    seen = []

                    async def upstream(body, **kwargs):
                        seen.append(copy.deepcopy(body["request"]))
                        item = reply([]) if len(seen) == 1 else last
                        yield item if isinstance(item, Response) else sse(item)

                    output = [item async for item in run(payload(), upstream)]
                    self.assertEqual(len(seen), 2)
                    self.assertEqual(seen[0]["contents"][-1]["parts"][-1]["text"], "你好")
                    self.assertEqual(seen[1]["contents"][-1]["parts"][-1]["text"], "⟦U:4F60 597D⟧")
                    if isinstance(last, Response) or not last["candidates"][0]["content"]["parts"]:
                        self.assertEqual(output[-1].status_code, 400)
                    else:
                        self.assertTrue(any(b"OK" in item for item in output))

    async def test_nonstream_tries_only_twice(self):
        seen = []

        @retry_empty_response
        async def upstream(body, **kwargs):
            seen.append(copy.deepcopy(body["request"]))
            return JSONResponse(reply([]))

        result = await upstream(payload())
        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["contents"][-1]["parts"][0]["text"], "⟦U:4F60 597D⟧")

    async def test_thoughts_are_forwarded_before_retry(self):
        calls = []

        async def upstream(body, **kwargs):
            calls.append(body)
            yield sse(reply([{"text": "Thought", "thought": True}]))

        stream = empty_output_stream(payload(), upstream)
        self.assertIn(b"Thought", await anext(stream))
        self.assertEqual(len(calls), 1)
        output = [item async for item in stream]
        self.assertEqual(len(calls), 2)
        self.assertEqual(output[-1].status_code, 400)

    def test_flagged_prompt_model_scope(self):
        for model in ("claude-opus-5-5", "claude-sonnet-5.5-high", "gemini-4-argon-max"):
            self.assertTrue(antigravity._uses_system_prompt_retry(payload(model)))
        for model in ("claude-opus-4-6", "claude-opus-5-50", "gemini-3.8-flash-high"):
            body = payload(model)
            self.assertFalse(antigravity._uses_system_prompt_retry(body))
            self.assertTrue(antigravity._uses_system_prompt_retry({**body, "_anti_truncation": True}))

    async def test_flagged_then_empty_preserves_move_and_encodes_only_user(self):
        for model in ("gemini-4-argon-high", "claude-opus-5-5-high"):
            for mode in ("stream", "nonstream", "anti", "passthrough"):
                with self.subTest(model=model, mode=mode):
                    seen = []

                    async def select(model, session, attempted):
                        name = f"test-{len(attempted)}"
                        attempted.add(name)
                        return name, "test-token", "test-project"

                    def response(request):
                        seen.append(copy.deepcopy(request))
                        if len(seen) == 1:
                            return 429, {"error": {
                                "code": 429, "status": "RESOURCE_EXHAUSTED",
                                "message": "Resource has been exhausted (e.g. check quota).",
                            }}
                        return 200, reply([] if len(seen) == 2 else [{"text": "OK"}])

                    async def stream_post(body, **kwargs):
                        code, data = response(body["request"])
                        yield JSONResponse(data, status_code=code) if code != 200 else sse(data)

                    async def post(json, **kwargs):
                        code, data = response(json["request"])
                        return httpx.Response(code, json=data)

                    with ExitStack() as stack:
                        if hasattr(antigravity, "record_api_call_success_later"):
                            stack.enter_context(patch.object(antigravity, "record_api_call_success_later", return_value=None))
                        for name, mock in {
                            "_get_valid_request_credential": select,
                            "get_retry_config": AsyncMock(return_value={"max_credentials": 3, "retry_interval": 0}),
                            "get_empty_output_error_enabled": AsyncMock(return_value=True),
                            "get_antigravity_stream2nostream": AsyncMock(return_value=False),
                            "get_antigravity_api_url": AsyncMock(return_value="https://example.invalid"),
                            "_record_response_error": AsyncMock(), "record_api_call_error": AsyncMock(),
                            "record_api_call_success": AsyncMock(),
                            "stream_post_async": stream_post, "post_async": post,
                        }.items():
                            stack.enter_context(patch.object(antigravity, name, mock))
                        body = payload(model)
                        if mode == "nonstream":
                            result = await antigravity.non_stream_request(body)
                            self.assertEqual(result.status_code, 200)
                        else:
                            if mode == "passthrough":
                                body["request"]["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
                            run = antigravity.stream_request(body) if mode == "stream" else (
                                anti_truncation_gemini_stream(body, antigravity.stream_request)
                            )
                            result = [item async for item in run]
                            self.assertTrue(any(b"OK" in item for item in result))
                    self.assertEqual(len(seen), 3)
                    self.assertIn("systemInstruction", seen[0])
                    self.assertNotIn("systemInstruction", seen[1])
                    self.assertNotIn("systemInstruction", seen[2])
                    self.assertEqual(seen[1]["contents"][0]["parts"][0]["text"], "System instruction")
                    self.assertEqual(seen[2]["contents"][0]["parts"][0]["text"], "System instruction")
                    self.assertEqual(seen[2]["contents"][0]["parts"][-1]["text"], "⟦U:4F60 597D⟧")


if __name__ == "__main__":
    unittest.main()
