import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Response

from src.api.antigravity import (
    _rewrite_resource_exhausted_response, non_stream_request, stream_request,
)


class AntigravityErrorRewriteTests(unittest.TestCase):
    def test_generic_resource_exhausted_without_reset_gets_clear_message(self):
        response = Response(
            content=json.dumps({
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                }
            }),
            status_code=429,
        )

        rewritten = _rewrite_resource_exhausted_response(response)
        payload = json.loads(rewritten.body)

        self.assertEqual(rewritten.status_code, 400)
        self.assertEqual(payload["error"]["code"], 400)
        self.assertEqual(payload["error"]["status"], "INVALID_ARGUMENT")
        self.assertEqual(
            payload["error"]["message"],
            "系统提示词中含有被标记内容，请清理后重试。",
        )

    def test_response_with_reset_time_is_unchanged(self):
        response = Response(
            content=json.dumps({
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                    "details": [{
                        "metadata": {"quotaResetTimeStamp": "2026-09-04T00:00:00Z"}
                    }],
                }
            }),
            status_code=429,
        )

        rewritten = _rewrite_resource_exhausted_response(response)
        payload = json.loads(rewritten.body)

        self.assertIs(rewritten, response)
        self.assertEqual(
            payload["error"]["message"],
            "Resource has been exhausted (e.g. check quota).",
        )

    def test_other_errors_are_unchanged(self):
        for status_code, message in ((429, "Quota exceeded"), (503, "unavailable"), (400, "bad request")):
            with self.subTest(status_code=status_code):
                response = Response(content=json.dumps({"error": {
                    "code": status_code, "status": "RESOURCE_EXHAUSTED", "message": message,
                }}), status_code=status_code)
                self.assertIs(_rewrite_resource_exhausted_response(response), response)


class AntigravityContentPolicyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_with_twenty_second_cooldown_and_returns_client_status(self):
        error_text = json.dumps({"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": "Resource has been exhausted (e.g. check quota).",
        }})
        success_body = json.dumps({"candidates": [{
            "content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP",
        }]})
        for transport in ("stream", "nonstream", "stream2nostream"):
            for second_succeeds in (True, False):
                with self.subTest(transport=transport, second_succeeds=second_succeeds):
                    manager = SimpleNamespace(
                        get_valid_credential=AsyncMock(side_effect=[
                            ("a.json", {"access_token": "a", "project_id": "a-project"}),
                            ("b.json", {"access_token": "b", "project_id": "b-project"}),
                        ]),
                        record_api_call_result=AsyncMock(),
                        set_cred_disabled=AsyncMock(),
                    )
                    responses = iter([
                        (429, error_text),
                        (200, success_body) if second_succeeds else (429, error_text),
                    ])

                    async def post(**kwargs):
                        code, body = next(responses)
                        return SimpleNamespace(status_code=code, content=body.encode(), text=body, headers={})

                    async def stream_post(**kwargs):
                        code, body = next(responses)
                        if code == 200:
                            yield f"data: {body}\n\n"
                        else:
                            yield Response(content=body, status_code=code)

                    with patch.multiple(
                        "src.api.antigravity",
                        credential_manager=manager,
                        get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
                        get_antigravity_stream2nostream=AsyncMock(return_value=transport == "stream2nostream"),
                        get_retry_config=AsyncMock(return_value={"max_credentials": 2, "retry_interval": 0}),
                        get_empty_output_error_enabled=AsyncMock(return_value=True),
                        post_async=post,
                        stream_post_async=stream_post,
                    ), patch("src.api.utils.time.time", return_value=1000), patch(
                        "src.api.utils.get_empty_output_error_enabled", AsyncMock(return_value=True)
                    ):
                        request = {"model": "gemini-3-flash", "request": {}}
                        if transport == "stream":
                            chunks = [chunk async for chunk in stream_request(request)]
                            response = chunks[-1]
                        else:
                            response = await non_stream_request(request)

                    self.assertEqual(manager.get_valid_credential.await_count, 2)
                    self.assertEqual(manager.get_valid_credential.await_args.kwargs["exclude_credentials"], {"a.json"})
                    manager.set_cred_disabled.assert_not_awaited()
                    failures = [call for call in manager.record_api_call_result.await_args_list if not call.args[1]]
                    self.assertEqual(len(failures), 1 if second_succeeds else 2)
                    for recorded in failures:
                        self.assertEqual(recorded.args[2], 429)
                        self.assertEqual(recorded.kwargs["cooldown_until"], 1020)
                        self.assertEqual(recorded.kwargs["error_message"], error_text)
                    if second_succeeds:
                        if transport == "stream":
                            self.assertIn('"text": "ok"', response)
                        else:
                            self.assertEqual(response.status_code, 200)
                    else:
                        self.assertEqual(response.status_code, 400)
                        error = json.loads(response.body)["error"]
                        self.assertEqual(error["code"], 400)
                        self.assertEqual(error["status"], "INVALID_ARGUMENT")
                        self.assertEqual(error["message"], "系统提示词中含有被标记内容，请清理后重试。")


if __name__ == "__main__":
    unittest.main()
