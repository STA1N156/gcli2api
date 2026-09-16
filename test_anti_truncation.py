import copy
import importlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from src.api.empty_output import build_empty_model_output_response
from src.converter.anti_truncation import REPLY_TOOL_INSTRUCTION, apply_anti_truncation
from src.router.anti_truncation import anti_truncation_gemini_stream, collect_anti_truncation_response
from src.utils import (
    authenticate_bearer, authenticate_gemini_flexible,
    get_available_models, get_base_model_from_feature_model, is_anti_truncation_model,
)


def event(parts=(), reason=None, usage=None, wrapped=True):
    candidate = {"index": 0, "content": {"role": "model", "parts": list(parts)}}
    if reason:
        candidate["finishReason"] = reason
    response = {"candidates": [candidate]}
    if usage:
        response["usageMetadata"] = usage
    return "data: " + json.dumps({"response": response} if wrapped else response, ensure_ascii=False) + "\n\n"


def reply(text):
    return {"functionCall": {"name": "output_reply", "args": {"content": text}}}


def unpack(chunks):
    return [json.loads(chunk[6:]) for chunk in chunks
            if isinstance(chunk, bytes) and chunk != b"data: [DONE]\n\n"]


class AntiTruncationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.payload = {
            "model": "gemini-3.1-pro-preview", "cache_session_key": "session",
            "request": {
                "systemInstruction": {"parts": [{"text": "保留用户原文"}]},
                "contents": [{"role": "user", "parts": [{"text": "他今年18岁，请写自传"}]}],
                "generationConfig": {"maxOutputTokens": 64000},
            },
        }
        self.calls = []

    async def run_stream(self, attempts, limit=3, payload=None):
        async def upstream(*, body, native):
            self.calls.append(copy.deepcopy(body))
            for chunk in attempts[min(len(self.calls) - 1, len(attempts) - 1)]:
                yield chunk
        return [chunk async for chunk in anti_truncation_gemini_stream(
            payload or self.payload, upstream, limit,
        )]

    def test_request_is_immutable_and_does_not_rewrite_user_content(self):
        original = copy.deepcopy(self.payload)
        result = apply_anti_truncation(self.payload)
        self.assertEqual(self.payload, original)
        self.assertEqual(result["request"]["contents"], original["request"]["contents"])
        self.assertEqual(result["request"]["systemInstruction"]["parts"], [
            *original["request"]["systemInstruction"]["parts"],
            {"text": REPLY_TOOL_INSTRUCTION},
        ])
        self.assertNotIn("role", result["request"]["systemInstruction"])
        self.assertEqual(apply_anti_truncation(result), result)
        self.assertEqual(result["cache_session_key"], "session")
        self.assertEqual(result["request"]["toolConfig"]["functionCallingConfig"], {
            "mode": "ANY", "allowedFunctionNames": ["output_reply"],
        })

    def test_real_tools_and_search_are_preserved(self):
        self.payload["request"]["tools"] = [
            {"googleSearch": {}}, {"functionDeclarations": [{"name": "lookup"}]},
        ]
        result = apply_anti_truncation(self.payload)
        self.assertEqual(result["request"]["tools"][:2], self.payload["request"]["tools"])
        self.assertEqual(result["request"]["toolConfig"]["functionCallingConfig"], {"mode": "ANY"})

    async def test_model_lists_use_new_prefix_and_old_requests_still_resolve(self):
        from src.router.antigravity.model_list import get_antigravity_models_with_features
        with patch("src.router.antigravity.model_list.fetch_available_models", AsyncMock(
            return_value=[{"id": "gemini-3.5-flash"}],
        )):
            antigravity_models = await get_antigravity_models_with_features()
        for models in (get_available_models(), antigravity_models):
            self.assertTrue(any(model.startswith("抗截断/") for model in models))
            self.assertFalse(any(model.startswith("流式抗截断/") for model in models))
        for prefix in ("抗截断/", "流式抗截断/"):
            model = prefix + "gemini-3.1-pro-preview-high-search"
            self.assertTrue(is_anti_truncation_model(model))
            self.assertEqual(get_base_model_from_feature_model(model), "gemini-3.1-pro-preview-high-search")

    async def test_existing_rphub_tool_and_explicit_choices_are_not_consumed(self):
        for mode in ("ANY", "NONE", "AUTO"):
            with self.subTest(mode=mode):
                self.calls.clear()
                payload = copy.deepcopy(self.payload)
                payload["request"]["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
                if mode == "AUTO":
                    payload["request"]["tools"] = [{"functionDeclarations": [{"name": "output_reply"}]}]
                source = event([reply("客户端自己的工具正文")], "STOP")
                chunks = await self.run_stream([[source]], payload=payload)
                self.assertEqual(chunks, [source])
                self.assertEqual(self.calls, [payload])

    async def test_tool_body_becomes_plain_text_without_blank_lines_or_duplicate_fallback(self):
        text = '正文\n"引号"\\路径😀 [done]'
        thought = {"text": "原生思考", "thought": True, "thoughtSignature": "signature"}
        chunks = await self.run_stream([[
            event([thought]), event([{"text": "不应重复显示的正文"}]),
            event([reply(text)], "STOP", {"promptTokenCount": 100, "candidatesTokenCount": 20}),
            "data: [DONE]\n\n",
        ]])
        responses = [data["response"] for data in unpack(chunks)]
        parts = [part for data in responses for candidate in data["candidates"]
                 for part in candidate["content"]["parts"]]
        self.assertEqual(parts, [thought, {"text": text}])
        self.assertEqual(responses[-1]["usageMetadata"]["promptTokenCount"], 100)
        self.assertEqual(responses[-1]["candidates"][0]["finishReason"], "STOP")
        self.assertEqual(chunks.count(b"data: [DONE]\n\n"), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_plain_text_fallback_and_token_limit_never_continue(self):
        for reason in ("STOP", "MAX_TOKENS"):
            with self.subTest(reason=reason):
                self.calls.clear()
                chunks = await self.run_stream([[event([{"text": "正文"}], reason, wrapped=False)]])
                final = unpack(chunks)[-1]["candidates"][0]
                self.assertEqual(final["content"]["parts"], [{"text": "正文"}])
                self.assertEqual(final["finishReason"], reason)
                self.assertEqual(len(self.calls), 1)

    async def test_real_tool_call_keeps_id_signature_and_finish_reason(self):
        tool = {"functionCall": {"name": "lookup", "id": "call_123", "args": {"q": "query"}},
                "thoughtSignature": "signature"}
        chunks = await self.run_stream([[event([tool]), event(reason="STOP")]])
        final = unpack(chunks)[-1]["response"]["candidates"][0]
        self.assertEqual(final["content"]["parts"], [tool])
        self.assertEqual(final["finishReason"], "STOP")
        self.assertEqual(len(self.calls), 1)

    async def test_only_truly_empty_response_is_retried_and_usage_is_summed(self):
        chunks = await self.run_stream([
            [event(reason="STOP", usage={"promptTokenCount": 100})],
            [event([reply("成功")], "STOP", {"promptTokenCount": 110, "candidatesTokenCount": 3})],
        ])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], self.calls[1])
        usage = unpack(chunks)[-1]["response"]["usageMetadata"]
        self.assertEqual(usage, {"promptTokenCount": 210, "candidatesTokenCount": 3})

    async def test_empty_http_response_obeys_total_attempt_limit(self):
        chunks = await self.run_stream([[build_empty_model_output_response()]], limit=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].status_code, 461)

    async def test_thinking_only_empty_tool_and_errors_do_not_retry(self):
        cases = [
            [event([{"text": "思考", "thought": True}], "STOP")],
            [event([reply("")], "STOP")],
            [event([{"functionCall": {"name": "output_reply", "args": {"content": 1}}}], "STOP")],
            [event([reply("a"), reply("b")], "STOP")],
            [event([reply("a"), {"functionCall": {"name": "lookup", "args": {}}}], "STOP")],
            [event(reason="SAFETY")],
            [JSONResponse({"error": {"code": 400, "message": "bad request"}}, status_code=400)],
            [JSONResponse({"error": {"code": 429, "message": "quota"}}, status_code=429)],
            ['data: {"error":{"code":500,"message":"failure"}}\n\n'],
            ['data: {invalid json}\n\n'],
        ]
        for source in cases:
            with self.subTest(source=source):
                self.calls.clear()
                chunks = await self.run_stream([source])
                self.assertEqual(len(self.calls), 1)
                self.assertIsInstance(chunks[-1], Response)
                self.assertGreaterEqual(chunks[-1].status_code, 400)

    async def test_nonstream_uses_same_body_reasoning_and_usage(self):
        async def upstream(**kwargs):
            yield event([{"text": "思考", "thought": True}])
            yield event([reply("完整正文")], "STOP", {"promptTokenCount": 8, "candidatesTokenCount": 5})
        with patch("src.api.utils.get_empty_output_error_enabled", AsyncMock(return_value=True)):
            response = await collect_anti_truncation_response(self.payload, upstream, 3)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["candidates"][0]["content"]["parts"], [
            {"text": "思考", "thought": True}, {"text": "完整正文"},
        ])
        self.assertEqual(data["usageMetadata"]["promptTokenCount"], 8)

    async def test_cancellation_closes_upstream_without_retry(self):
        closed = []
        async def upstream(**kwargs):
            try:
                yield event([{"text": "思考", "thought": True}])
                self.fail("Cancelled stream should not request another chunk")
            finally:
                closed.append(True)
        stream = anti_truncation_gemini_stream(self.payload, upstream, 3)
        await anext(stream)
        await stream.aclose()
        self.assertEqual(closed, [True])

    async def test_upstream_is_closed_before_returning_an_http_error(self):
        closed = []
        async def upstream(**kwargs):
            try:
                yield JSONResponse({"error": "bad request"}, status_code=400)
            finally:
                closed.append(True)
        stream = anti_truncation_gemini_stream(self.payload, upstream, 3)
        response = await anext(stream)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(closed, [True])
        await stream.aclose()


class AntiTruncationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def request_route(self, backend, protocol, streaming, source):
        module = importlib.import_module(f"src.router.{backend}.{protocol}")
        app = FastAPI()
        app.include_router(module.router)
        app.dependency_overrides[authenticate_bearer] = lambda: "test"
        app.dependency_overrides[authenticate_gemini_flexible] = lambda: "test"
        calls = []
        async def upstream(*, body, native):
            calls.append(body)
            for chunk in source:
                yield chunk
        prefix = "/antigravity" if backend == "antigravity" else ""
        model = "抗截断/gemini-3.1-pro-preview"
        body = {"model": model, "messages": [{"role": "user", "content": "请回答一个问题"}],
                "stream": streaming, "max_tokens": 64000}
        if protocol == "gemini":
            action = "streamGenerateContent" if streaming else "generateContent"
            url = f"{prefix}/v1/models/{model}:{action}"
            body = {"contents": [{"role": "user", "parts": [{"text": "请回答一个问题"}]}]}
        else:
            url = f"{prefix}/v1/" + ("chat/completions" if protocol == "openai" else "messages")
        with ExitStack() as stack:
            stack.enter_context(patch(f"src.api.{backend}.stream_request", upstream))
            stack.enter_context(patch.object(module, "get_anti_truncation_max_attempts", AsyncMock(return_value=3)))
            stack.enter_context(patch("config.get_config_value", AsyncMock(side_effect=lambda key, default=None: default)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(url, json=body)
        return response, calls

    async def test_all_backends_protocols_and_stream_modes_restore_reply(self):
        for backend in ("geminicli", "antigravity"):
            for protocol in ("openai", "anthropic", "gemini"):
                for streaming in (False, True):
                    with self.subTest(backend=backend, protocol=protocol, streaming=streaming):
                        response, calls = await self.request_route(backend, protocol, streaming, [
                            event([reply("完整正文")], "STOP", {"promptTokenCount": 8, "candidatesTokenCount": 5}),
                        ])
                        self.assertEqual(response.status_code, 200, response.text)
                        if streaming:
                            data = [json.loads(line[6:]) for line in response.text.splitlines()
                                    if line.startswith("data: ") and line != "data: [DONE]"]
                            self.assertIn("完整正文", json.dumps(data, ensure_ascii=False))
                            if protocol == "openai":
                                self.assertIsNone(data[0]["choices"][0]["finish_reason"])
                                self.assertEqual(data[-1]["choices"][0]["finish_reason"], "stop")
                        else:
                            self.assertIn("完整正文", response.text)
                        self.assertNotIn("output_reply", response.text)
                        self.assertNotIn("tool_calls", response.text)
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0]["request"]["toolConfig"]["functionCallingConfig"]["mode"], "ANY")

    async def test_http_error_is_preserved_before_stream_starts(self):
        error = JSONResponse({"error": {"code": 400, "message": "bad request"}}, status_code=400)
        for backend in ("geminicli", "antigravity"):
            for protocol in ("openai", "anthropic", "gemini"):
                with self.subTest(backend=backend, protocol=protocol):
                    response, calls = await self.request_route(backend, protocol, True, [error])
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(len(calls), 1)

    async def test_error_after_thinking_is_not_hidden_as_success(self):
        for protocol in ("openai", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                response, calls = await self.request_route("geminicli", protocol, True, [
                    event([{"text": "思考", "thought": True}]),
                    event([{"functionCall": {"name": "output_reply", "args": {"content": 1}}}], "STOP"),
                ])
                self.assertIn("error", response.text)
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
