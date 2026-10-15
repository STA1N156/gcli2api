import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Response
from fastapi.responses import JSONResponse

from src.api import antigravity
from src.api.empty_retry import empty_output_stream, has_current_input_tool_result, wrap_user_input_in_tool, retry_empty_response
from src.converter.anti_truncation import (
    REPLY_TOOL_INSTRUCTION, apply_anti_truncation, latest_user_text_parts, move_system_to_first_user,
)
from src.converter.anthropic2gemini import convert_messages_to_contents
from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.router.anti_truncation import anti_truncation_gemini_stream, collect_anti_truncation_response


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
    def assert_input_tool(self, request, index, text):
        contents = request["contents"]
        call = contents[index + 1]["parts"][0]["functionCall"]
        result = contents[index + 2]["parts"][0]["functionResponse"]
        self.assertEqual(contents[index + 1]["role"], "model")
        self.assertEqual(contents[index + 2]["role"], "user")
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["id"], result["id"])
        self.assertEqual(call["name"], "read_user_input")
        self.assertEqual(result["name"], call["name"])
        self.assertEqual(call["args"], {})
        self.assertEqual(result["response"], {"source": "user_input", "text": text})
        return call["id"]

    def test_tool_result_keeps_original_text_and_unique_ids(self):
        ids = set()
        for text in ("走吧", "前⟦U:4F60 597D⟧后", '"引号"\n😀', ""):
            with self.subTest(text=text):
                body = payload()
                body["request"]["contents"][0]["parts"][0]["text"] = text
                source_parts = latest_user_text_parts(body["request"])
                wrap_user_input_in_tool(body, source_parts)
                call_id = self.assert_input_tool(body["request"], 0, text)
                self.assertNotIn(call_id, ids)
                ids.add(call_id)
                self.assertEqual(source_parts[0]["text"], text)
                prepared = copy.deepcopy(body)
                wrap_user_input_in_tool(body, source_parts)
                self.assertEqual(body, prepared)

    def test_only_current_complete_input_tool_pair_is_recognized(self):
        body = payload()
        wrap_user_input_in_tool(body, latest_user_text_parts(body["request"]))
        self.assertTrue(has_current_input_tool_result(body["request"]))
        for kind in ("new_user", "id", "source", "name", "text", "missing_call"):
            request = copy.deepcopy(body["request"])
            with self.subTest(kind=kind):
                if kind == "new_user":
                    request["contents"].append({"role": "user", "parts": [{"text": "new input"}]})
                elif kind == "missing_call":
                    request["contents"].pop(1)
                elif kind == "name":
                    request["contents"][1]["parts"][0]["functionCall"]["name"] = "another_tool"
                else:
                    response = request["contents"][2]["parts"][0]["functionResponse"]
                    if kind == "id":
                        response["id"] = "unmatched"
                    elif kind == "source":
                        response["response"]["source"] = "external_data"
                    else:
                        response["response"].pop("text")
                self.assertFalse(has_current_input_tool_result(request))
        body["request"]["contents"].extend([
            {"role": "model", "parts": [{"functionCall": {"name": "search", "id": "search-id"}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "search", "id": "search-id", "response": {}}}]},
        ])
        self.assertTrue(has_current_input_tool_result(body["request"]))

    async def test_already_wrapped_input_is_not_retried(self):
        for mode in ("ordinary", "anti", "passthrough", "nonstream"):
            with self.subTest(mode=mode):
                body = payload()
                wrap_user_input_in_tool(body, latest_user_text_parts(body["request"]))
                calls = []

                async def upstream(body, **kwargs):
                    calls.append(body)
                    yield sse(reply([]))

                @retry_empty_response
                async def nonstream(body, **kwargs):
                    calls.append(body)
                    return JSONResponse(reply([]))

                if mode == "nonstream":
                    result = await nonstream(body)
                else:
                    run = empty_output_stream if mode == "ordinary" else anti_truncation_gemini_stream
                    if mode == "passthrough":
                        body["request"]["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
                    result = [item async for item in run(body, upstream)]
                    result = result[-1]
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(sum(
                    part.get("functionCall", {}).get("name") == "read_user_input"
                    for content in calls[0]["request"]["contents"] for part in content["parts"]
                ), 1)

    async def test_anti_wraps_first_request_and_does_not_retry_empty(self):
        for mode in ("stream", "nonstream", "passthrough"):
            for empty in (False, True):
                with self.subTest(mode=mode, empty=empty):
                    body = payload()
                    image = {"inlineData": {"mimeType": "image/png", "data": "test"}}
                    body["request"]["contents"][0]["parts"].append(image)
                    if mode == "passthrough":
                        body["request"]["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
                    original = copy.deepcopy(body)
                    seen = []

                    async def upstream(body, **kwargs):
                        seen.append(copy.deepcopy(body["request"]))
                        parts = [] if empty else ([{"text": "OK"}] if mode == "passthrough" else [{
                            "functionCall": {"name": "output_reply", "args": {"content": "OK"}},
                        }])
                        yield sse(reply(parts))

                    if mode == "nonstream":
                        result = await collect_anti_truncation_response(body, upstream)
                        self.assertEqual(result.status_code, 400 if empty else 200)
                    else:
                        result = [item async for item in anti_truncation_gemini_stream(body, upstream)]
                        if empty:
                            self.assertEqual(result[-1].status_code, 400)
                        else:
                            self.assertTrue(any(b"OK" in item for item in result))
                    self.assertEqual(len(seen), 1)
                    self.assert_input_tool(seen[0], 0, "你好")
                    self.assertIn(image, seen[0]["contents"][0]["parts"])
                    self.assertEqual(seen[0]["systemInstruction"]["parts"][0]["text"], "System instruction")
                    if mode != "passthrough":
                        self.assertEqual(seen[0]["contents"][0]["parts"][0]["text"], REPLY_TOOL_INSTRUCTION + "\n")
                    self.assertNotIn("read_user_input", json.dumps(seen[0].get("tools", [])))
                    self.assertEqual(body, original)

    async def test_client_openai_and_anthropic_input_tool_formats(self):
        text = json.dumps({"source": "user_input", "text": "走吧"}, ensure_ascii=False)
        request = {"model": "gemini-4-argon-high", "messages": [
            {"role": "user", "content": "Read the tool result"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_test", "type": "function",
                "function": {"name": "read_user_input", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_test", "content": text},
        ]}
        with patch("src.converter.openai2gemini.merge_system_messages", AsyncMock(return_value=request)):
            converted = await convert_openai_to_gemini_request(request)
        self.assertTrue(has_current_input_tool_result(converted))
        converted = {"contents": convert_messages_to_contents([
            {"role": "user", "content": "Read the tool result"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_test", "name": "read_user_input", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_test", "content": text}]},
        ])}
        self.assertTrue(has_current_input_tool_result(converted))

    async def test_success_does_not_check_retry_structure(self):
        async def upstream(body, **kwargs):
            yield sse(reply([{"text": "OK"}]))

        with patch("src.api.empty_retry.has_current_input_tool_result", side_effect=AssertionError("unexpected check")):
            result = [item async for item in empty_output_stream(payload(), upstream)]
        self.assertTrue(any(b"OK" in item for item in result))

    def test_wrapping_preserves_history_images_tools_and_moved_system(self):
        body = payload()
        contents = body["request"]["contents"]
        contents.insert(0, {"role": "user", "parts": [{"text": "History"}]})
        contents[-1]["parts"].extend([
            {"inlineData": {"mimeType": "image/png", "data": "test"}},
            {"text": "\n😀"},
        ])
        contents.extend([
            {"role": "model", "parts": [{"functionCall": {"id": "real-id", "name": "lookup", "args": {}}}]},
            {"role": "user", "parts": [{"functionResponse": {"id": "real-id", "name": "lookup", "response": {}}}]},
        ])
        original = copy.deepcopy(body)
        body = apply_anti_truncation(body)
        tools = copy.deepcopy(body["request"]["tools"])
        parts = latest_user_text_parts(body["request"])
        body["request"] = move_system_to_first_user(body["request"])
        wrap_user_input_in_tool(body, parts)
        result = body["request"]["contents"]
        self.assertEqual(result[0]["parts"][0]["text"], "System instruction")
        self.assertEqual(result[0]["parts"][-1]["text"], "History")
        self.assertEqual(result[1]["parts"][0]["text"], REPLY_TOOL_INSTRUCTION + "\n")
        self.assertIn("read_user_input", result[1]["parts"][1]["text"])
        self.assertEqual(result[1]["parts"][2], original["request"]["contents"][1]["parts"][1])
        self.assert_input_tool(body["request"], 1, "你好\n😀")
        self.assertEqual(result[-2], original["request"]["contents"][-2])
        self.assertEqual(result[-1], original["request"]["contents"][-1])
        self.assertEqual(body["request"]["tools"], tools)
        self.assertNotIn("systemInstruction", body["request"])
        self.assertEqual(original["request"]["contents"][1]["parts"][0]["text"], "你好")

    async def test_streams_try_only_twice_and_leave_other_errors_alone(self):
        for last in (reply([]), reply([{"text": "OK"}]), Response(status_code=400)):
            with self.subTest(last=last):
                seen = []

                async def upstream(body, **kwargs):
                    seen.append(copy.deepcopy(body["request"]))
                    item = reply([]) if len(seen) == 1 else last
                    yield item if isinstance(item, Response) else sse(item)

                output = [item async for item in empty_output_stream(payload(), upstream)]
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0]["contents"][-1]["parts"][-1]["text"], "你好")
                self.assert_input_tool(seen[1], 0, "你好")
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
        self.assert_input_tool(seen[1], 0, "你好")

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

    def test_flagged_prompt_moves_only_once_and_preserves_body(self):
        body = payload()
        final_payload = copy.deepcopy(body)
        self.assertTrue(antigravity._move_flagged_system_prompt(body, final_payload))
        self.assertNotIn("systemInstruction", body["request"])
        self.assertEqual(body["request"], final_payload["request"])
        self.assertEqual(body["request"]["contents"][0]["parts"][-1]["text"], "你好")
        moved = copy.deepcopy(body)
        self.assertFalse(antigravity._move_flagged_system_prompt(body, final_payload))
        self.assertEqual(body, moved)

    async def test_flagged_then_empty_preserves_move_and_wraps_only_user(self):
        for model in (
            "gemini-4-argon-high", "claude-opus-5-5-high", "gemini-3.8-flash-high",
            "gemini-3.1-pro-preview", "claude-opus-4-6",
        ):
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
                            "relocate_known_prompt": AsyncMock(), "remember_marked_prompt": AsyncMock(),
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
                            if mode in ("anti", "passthrough"):
                                self.assertEqual(result[-1].status_code, 400)
                            else:
                                self.assertTrue(any(b"OK" in item for item in result))
                    if mode in ("anti", "passthrough"):
                        self.assertEqual(len(seen), 2)
                        self.assert_input_tool(seen[0], 0, "你好")
                        self.assert_input_tool(seen[1], 0, "你好")
                        self.assertIn("systemInstruction", seen[0])
                        self.assertNotIn("systemInstruction", seen[1])
                        self.assertEqual(seen[1]["contents"][0]["parts"][0]["text"], "System instruction")
                        continue
                    self.assertEqual(len(seen), 3)
                    self.assertIn("systemInstruction", seen[0])
                    self.assertNotIn("systemInstruction", seen[1])
                    self.assertNotIn("systemInstruction", seen[2])
                    self.assertEqual(seen[1]["contents"][0]["parts"][0]["text"], "System instruction")
                    self.assertEqual(seen[2]["contents"][0]["parts"][0]["text"], "System instruction")
                    self.assert_input_tool(seen[2], 0, "你好")


if __name__ == "__main__":
    unittest.main()
