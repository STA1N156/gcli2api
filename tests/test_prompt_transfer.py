import ast
import asyncio
import __future__
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import Response
from src.converter.anti_truncation import move_system_to_first_user


class PromptTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "src/api/antigravity.py"
        names = {"_RESOURCE_EXHAUSTED_MESSAGE", "_ANTIGRAVITY_CONTENT_POLICY_MESSAGE"}
        methods = {"_rewrite_resource_exhausted_response", "_move_system_prompt_for_retry"}
        nodes = [n for n in ast.parse(path.read_text(encoding="utf-8")).body if
                 isinstance(n, ast.FunctionDef) and n.name in methods or
                 isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
        cls.scope = {"json": json, "Response": Response, "move_system_to_first_user": move_system_to_first_user,
                     "log": SimpleNamespace(info=lambda *args: None)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag), cls.scope)

    def response(self, *, reset=False):
        error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": self.scope["_RESOURCE_EXHAUSTED_MESSAGE"]}
        if reset:
            error["details"] = [{"metadata": {"quotaResetTimeStamp": "2099-01-01T00:00:00Z"}}]
        return Response(json.dumps({"error": error}), 429)

    def test_matching_429_moves_prompt_for_this_retry(self):
        original = self.response()
        rewritten = self.scope["_rewrite_resource_exhausted_response"](original)
        self.assertIsNot(rewritten, original)
        request = {"systemInstruction": {"parts": [{"text": "system"}]},
                   "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        untouched = copy.deepcopy(request)
        body, payload = {"request": request}, {"request": request}
        self.assertTrue(self.scope["_move_system_prompt_for_retry"](body, payload))
        self.assertNotIn("systemInstruction", body["request"])
        self.assertEqual(payload["request"]["contents"][0]["parts"][0], {"text": "system"})
        self.assertEqual(request, untouched)
        self.assertFalse(self.scope["_move_system_prompt_for_retry"](body, payload))

    def test_quota_reset_429_keeps_normal_quota_handling(self):
        original = self.response(reset=True)
        self.assertIs(self.scope["_rewrite_resource_exhausted_response"](original), original)


class PromptRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_retry_keeps_account_and_skips_failure_writes(self):
        PromptTransferTests.setUpClass()
        path = Path(__file__).resolve().parents[1] / "src/api/antigravity.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for streaming in (False, True):
            for second_failure in (False, True):
                with self.subTest(streaming=streaming, second_failure=second_failure):
                    error = Response(json.dumps({"error": {"status": "RESOURCE_EXHAUSTED", "message": PromptTransferTests.scope["_RESOURCE_EXHAUSTED_MESSAGE"]}}), 429)
                    request = {"systemInstruction": {"parts": [{"text": "system"}]}, "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
                    payload = {"request": request}
                    select = AsyncMock(return_value=("account.json", "token", "project"))
                    save = AsyncMock()
                    sent = []

                    async def post(**kwargs):
                        sent.append(copy.deepcopy(kwargs.get("json", kwargs.get("body"))))
                        if len(sent) == 1 or second_failure:
                            return error
                        return SimpleNamespace(status_code=200, content=b'{}', headers={})

                    async def stream(**kwargs):
                        response = await post(**kwargs)
                        yield response if response is error else "data: OK"

                    scope = dict(PromptTransferTests.scope)
                    scope.update(asyncio=asyncio, log=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
                                 extract_cache_session_key=lambda *a: "session", _get_valid_request_credential=select,
                                 get_antigravity_api_url=AsyncMock(return_value="https://example.invalid"),
                                 build_antigravity_headers=lambda *a: {}, wrap_cli_request=AsyncMock(return_value=(payload, "id")),
                                 get_retry_config=AsyncMock(return_value={"retry_interval": 0}),
                                 get_empty_output_error_enabled=AsyncMock(return_value=False),
                                 get_antigravity_stream2nostream=AsyncMock(return_value=False),
                                 _record_response_error=save, credential_manager=object(),
                                 record_api_call_success=AsyncMock(), record_api_call_success_later=lambda *a, **k: None,
                                 rewrite_input_token_limit_response=lambda r: r, is_retryable_status=lambda s: s == 429,
                                 _generate_request_id=lambda *a: "retry-id", stream_chunk_finishes_response=lambda *a: False,
                                 _response_from_httpx=lambda r: r if isinstance(r, Response) else Response(r.content, r.status_code),
                                 post_async=post, stream_post_async=stream)
                    name = "stream_request" if streaming else "non_stream_request"
                    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
                    node.decorator_list = []
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag), scope)
                    body = {"model": "test", "request": request}
                    if streaming:
                        result = [v async for v in scope[name](body)]
                        if second_failure:
                            self.assertEqual(result[-1].status_code, 400)
                    else:
                        result = await scope[name](body)
                        self.assertEqual(result.status_code, 400 if second_failure else 200)
                    self.assertEqual(len(sent), 2)
                    self.assertIn("systemInstruction", sent[0]["request"])
                    self.assertNotIn("systemInstruction", sent[1]["request"])
                    select.assert_awaited_once()
                    save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
