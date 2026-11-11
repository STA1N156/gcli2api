import ast
import asyncio
import __future__
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import Response
from src.api.empty_output import stream_chunk_finishes_response, stream_chunk_has_visible_output


ROOT = Path(__file__).resolve().parents[1]


def event(*, text=None, finish=False, usage=False):
    response = {}
    if text is not None or finish:
        candidate = {"index": 0, "content": {"parts": [{"text": text or ""}]}}
        if finish:
            candidate["finishReason"] = "STOP"
        response["candidates"] = [candidate]
    if usage:
        response["usageMetadata"] = {"totalTokenCount": 4}
    return "data: " + json.dumps({"response": response})


class StreamCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self, chunks, *, hang_upstream=False, slow_save=False):
        closed = asyncio.Event()
        saved = asyncio.Event()
        save_tasks = []

        async def upstream(**kwargs):
            try:
                for chunk in chunks:
                    yield chunk
                if hang_upstream:
                    await asyncio.Event().wait()
            finally:
                closed.set()

        async def persist():
            if slow_save:
                await saved.wait()

        def record(*args, **kwargs):
            task = asyncio.create_task(persist())
            save_tasks.append(task)
            return task

        payload = {"request": {"generationConfig": {}}}
        namespace = {
            "asyncio": asyncio, "json": json, "Response": Response,
            "log": SimpleNamespace(warning=lambda *args: None),
            "extract_cache_session_key": lambda *args: None,
            "_get_valid_request_credential": AsyncMock(return_value=("test.json", "token", "project")),
            "get_antigravity_api_url": AsyncMock(return_value="https://example.invalid"),
            "build_antigravity_headers": lambda token: {},
            "wrap_cli_request": AsyncMock(return_value=(payload, "test-request")),
            "get_retry_config": AsyncMock(return_value={"retry_interval": 0}),
            "get_empty_output_error_enabled": AsyncMock(return_value=True),
            "stream_post_async": upstream,
            "stream_chunk_finishes_response": stream_chunk_finishes_response,
            "stream_chunk_has_visible_output": stream_chunk_has_visible_output,
            "record_api_call_success_later": record,
            "credential_manager": object(),
        }
        # Execute the actual generator without importing startup/network services.
        source = ast.parse((ROOT / "src/api/antigravity.py").read_text(encoding="utf-8"))
        function = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_request")
        function.decorator_list = []
        compiled = compile(ast.Module(body=[function], type_ignores=[]), "antigravity.py", "exec", flags=__future__.annotations.compiler_flag)
        exec(compiled, namespace)

        async def collect():
            return [chunk async for chunk in namespace["stream_request"]({"model": "test", "request": {}})]

        try:
            actual = await asyncio.wait_for(collect(), timeout=0.5)
            self.assertEqual(actual, chunks)
            self.assertTrue(closed.is_set())
            self.assertEqual(len(save_tasks), 1)
            if slow_save:
                self.assertFalse(save_tasks[0].done())
        finally:
            saved.set()
            await asyncio.gather(*save_tasks)

    async def test_final_result_closes_an_upstream_that_never_sends_eof(self):
        await self.run_stream([event(text="OK", finish=True, usage=True)], hang_upstream=True)

    async def test_slow_success_save_does_not_hold_response_open(self):
        await self.run_stream([event(text="OK")], slow_save=True)

    async def test_waits_for_final_usage_instead_of_using_interim_usage(self):
        await self.run_stream([
            event(text="O", usage=True),
            event(text="K", finish=True),
            event(usage=True),
        ], hang_upstream=True)

    def test_waits_for_all_requested_candidates(self):
        state = {"finished": set()}
        self.assertFalse(stream_chunk_finishes_response(event(finish=True, usage=True), state, 2))
        second = json.dumps({"candidates": [{"index": 1, "finishReason": "STOP"}], "usageMetadata": {"totalTokenCount": 8}})
        self.assertTrue(stream_chunk_finishes_response("data: " + second, state, 2))


if __name__ == "__main__":
    unittest.main()
