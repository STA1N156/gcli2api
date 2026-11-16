import copy
import json
import uuid
import unittest
from unittest.mock import AsyncMock, patch

from src.api.antigravity import _generate_request_id, fetch_available_models, wrap_cli_request
from src.converter.anti_truncation import apply_anti_truncation, latest_user_text_parts
from src.api.empty_retry import wrap_user_input_in_tool
from src.antigravity_identity import cache_model_enums, complete_execution, ExecutionStream, order_request


class AntigravityIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_completion_retry_and_json_order(self):
        request = {"sessionId": "execution-test", "contents": [{"parts": [{"text": "hi"}], "role": "user"}]}
        first, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "project")
        self.assertNotIn("last_execution_id", first["request"]["labels"])
        terminal = {"response": {"candidates": [{"finishReason": "STOP"}]}}
        observer = ExecutionStream(first["request"])
        line = ("data: " + json.dumps(terminal) + "\n\n").encode()
        observer.observe(line[:12])
        observer.observe(line[12:])
        request["contents"] += [{"role": "model", "parts": [{"text": "OK"}]}, {"role": "user", "parts": [{"text": "next"}]}]
        next_turn, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "project")
        previous = next_turn["request"]["labels"]["last_execution_id"]
        self.assertEqual(str(uuid.UUID(previous)), previous)
        complete_execution(next_turn["request"], {"error": {"code": 429}})
        retry, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "other-project")
        self.assertEqual(retry["request"]["labels"]["last_execution_id"], previous)
        complete_execution(first["request"], terminal)
        retry_again, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "project")
        self.assertEqual(retry_again["request"]["labels"]["last_execution_id"], previous)
        ordered = order_request(next_turn)
        self.assertEqual(ordered, next_turn)
        self.assertEqual(list(ordered), ["project", "requestId", "request", "model", "userAgent"])
        self.assertEqual(list(ordered["request"]), ["contents", "labels", "generationConfig", "sessionId"])
        self.assertEqual(list(ordered["request"]["contents"][0]), ["role", "parts"])
        self.assertEqual(list(ordered["request"]["labels"]), sorted(ordered["request"]["labels"]))
        custom = {"request": {"contents": [{"role": "model", "parts": [{"functionCall": {"args": {"z": 1, "a": 2}}}]}]}}
        self.assertEqual(list(order_request(custom)["request"]["contents"][0]["parts"][0]["functionCall"]["args"]), ["z", "a"])

    async def test_official_output_limits_and_account_retry(self):
        request = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                   "generationConfig": {"maxOutputTokens": 123}}
        for model, expected in (("gemini-3.8-flash-medium", 65536),
                                ("gemini-4-carbon-high", 262144),
                                ("claude-sonnet-4-6", 64000),
                                ("gpt-oss-120b-medium", 32768),
                                ("gemini-3.1-flash-image", 65536),
                                ("unknown-model", 123)):
            payload, _ = await wrap_cli_request(request, model, "project")
            self.assertEqual(payload["request"]["generationConfig"]["maxOutputTokens"], expected)
        model = "gemini-3.8-flash-medium"
        cache_model_enums("limit-account", {model: {"maxOutputTokens": 32768}})
        payload, _ = await wrap_cli_request(request, model, "project", account_id="limit-account")
        self.assertEqual(payload["request"]["generationConfig"]["maxOutputTokens"], 32768)
        _generate_request_id(model, payload["request"], "other-account")
        self.assertEqual(payload["request"]["generationConfig"]["maxOutputTokens"], 65536)
        self.assertEqual(request["generationConfig"]["maxOutputTokens"], 123)

    async def test_history_retry_and_explicit_labels(self):
        first = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
        snapshot = copy.deepcopy(first)
        a, _ = await wrap_cli_request(first, "gemini-3.5-flash-lite", "project-a", session_key="conversation-a")
        next_turn = {"contents": first["contents"] + [{"role": "model", "parts": [{"text": "hi"}]}, {"role": "user", "parts": [{"text": "next"}]}]}
        for request, project, step, request_index, outer in ((first, "project-b", "0", "0", "1"), (next_turn, "project-a", "2", "1", "3")):
            b, _ = await wrap_cli_request(request, "gemini-3.5-flash-lite", project, session_key="conversation-a")
            labels = b["request"]["labels"]
            for key in ("cascade_id", "trajectory_id", "root_cascade_id"):
                self.assertEqual(labels[key], a["request"]["labels"][key])
            parts = b["requestId"].split("/")
            self.assertEqual((parts[1], parts[3]), (labels["cascade_id"], labels["trajectory_id"]))
            self.assertEqual(labels["last_step_index"], step)
            self.assertEqual(labels["request_id"], f"{parts[3]}-{request_index}")
            self.assertEqual(parts[4], outer)
        other, _ = await wrap_cli_request(first, "gemini-3.5-flash-lite", "project-a", session_key="conversation-b")
        self.assertNotEqual(other["request"]["labels"]["cascade_id"], a["request"]["labels"]["cascade_id"])
        explicit = {**first, "labels": {"cascade_id": "existing", "trajectory_id": "trace", "last_step_index": "7", "request_id": "trace-3", "custom": "keep"}}
        b, _ = await wrap_cli_request(explicit, "gemini-3.5-flash-lite", "project-a")
        self.assertIn("/existing/", b["requestId"])
        self.assertEqual(b["request"]["labels"]["request_id"], "trace-3")
        self.assertEqual(b["request"]["labels"]["custom"], "keep")
        self.assertTrue(b["requestId"].endswith("/8"))
        self.assertEqual(first, snapshot)

    async def test_parallel_tools_and_antitruncation_retry_counters(self):
        parallel = {"sessionId": "-tools", "contents": [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "a"}}, {"functionCall": {"name": "b"}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "a"}}, {"functionResponse": {"name": "b"}}]},
        ]}
        a, _ = await wrap_cli_request(parallel, "gemini-3.8-flash-medium", "project")
        self.assertEqual(a["request"]["labels"]["last_step_index"], "3")
        self.assertTrue(a["request"]["labels"]["request_id"].endswith("-1"))
        self.assertTrue(a["requestId"].endswith("/4"))

        payload = apply_anti_truncation({"request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}})
        wrap_user_input_in_tool(payload, latest_user_text_parts(payload["request"]))
        a, _ = await wrap_cli_request(payload["request"], "gemini-3.8-flash-medium", "project", session_key="anti-truncation")
        labels = copy.deepcopy(a["request"]["labels"])
        self.assertEqual(labels["last_step_index"], "2")
        self.assertTrue(labels["request_id"].endswith("-1"))
        self.assertTrue(a["requestId"].endswith("/3"))
        self.assertTrue(_generate_request_id(a["model"], a["request"]).endswith("/3"))
        self.assertEqual(a["request"]["labels"], labels)

    async def test_model_query_uses_selected_account_project(self):
        response = type("Response", (), {"status_code": 200, "json": lambda self: {"models": {}}})()
        data = {"access_token": "fake", "project_id": "account-project", "refresh_token": "fake-refresh"}
        post = AsyncMock(return_value=response)
        with patch("src.api.antigravity.credential_manager.get_valid_credential", AsyncMock(return_value=("account.json", data))), patch("src.api.antigravity.get_antigravity_api_url", AsyncMock(return_value="https://local.invalid")), patch("src.api.antigravity.post_async", post):
            await fetch_available_models()
        self.assertEqual(post.await_args.kwargs["json"], {"project": "account-project"})

    async def test_model_usage_and_credential_catalog_refresh(self):
        request = {"sessionId": "usage-test", "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        await wrap_cli_request(request, "claude-sonnet-5-5-medium", "project")
        for enum in ("MODEL_OLD", "MODEL_NEW"):
            cache_model_enums("account-a", {"gemini-3.8-flash-medium": {"model": enum}})
            payload, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "project", account_id="account-a")
            labels = payload["request"]["labels"]
            self.assertEqual(labels["model_enum"], enum)
            for name in ("used_claude", "used_claude_conservative", "used_non_gemini_model"):
                self.assertEqual(labels[name], "true")
        other, _ = await wrap_cli_request(request, "gemini-3.8-flash-medium", "project", account_id="account-b")
        self.assertNotIn("model_enum", other["request"]["labels"])

    async def test_only_exact_resource_429_skips_failure_state(self):
        from src.api.antigravity import _record_response_error
        import json
        with patch("src.api.antigravity.record_api_call_error", AsyncMock()) as record:
            message = "Resource has been exhausted (e.g. check quota)."
            await _record_response_error("account", "model", 429, json.dumps({"error": {"message": message}}))
            record.assert_not_awaited()
            await _record_response_error("account", "model", 429, json.dumps({"error": {"message": message + " Later."}}))
            await _record_response_error("account", "model", 500, json.dumps({"error": {"message": message}}))
            self.assertEqual(record.await_count, 2)
