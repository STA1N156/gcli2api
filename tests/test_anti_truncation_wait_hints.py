import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Response

from src.converter.openai2gemini import convert_gemini_to_openai_stream
from src.router import anti_truncation as anti


def request():
    return {"model": "gemini-4-argon-high", "request": {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
    }}


def event(parts):
    return anti._sse({"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})


def text(chunk):
    return json.loads(chunk[6:])["candidates"][0]["content"]["parts"][0]["text"]


class WaitingHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_thought_gets_first_notice_immediately_and_resets_interval(self):
        answer_ready = asyncio.Event()

        async def upstream(**kwargs):
            yield event([{"text": "First thought", "thought": True}])
            yield event([{"text": "Next thought", "thought": True}])
            await answer_ready.wait()
            yield event([{"text": "Answer"}])

        with patch.object(anti, "FIRST_WAIT_HINT_SECONDS", 60), patch.object(anti, "WAIT_HINT_INTERVAL_SECONDS", 0.01):
            stream = anti.anti_truncation_gemini_stream(request(), upstream)
            self.assertEqual(text(await anext(stream)), anti.FIRST_WAIT_HINT + "\n\n")
            self.assertEqual(text(await anext(stream)), "First thought")
            self.assertEqual(text(await anext(stream)), "Next thought")
            self.assertEqual(text(await anext(stream)), "\n\n" + anti.WAIT_HINT + "\n\n")
            answer_ready.set()
            self.assertEqual(text(await anext(stream)), "Answer")
            await stream.aclose()

    async def test_fast_body_without_thoughts_has_no_notice(self):
        async def upstream(**kwargs):
            yield event([{"text": "Answer"}])

        result = [chunk async for chunk in anti.anti_truncation_gemini_stream(request(), upstream)]
        self.assertEqual(text(result[0]), "Answer")
        self.assertNotIn(anti.FIRST_WAIT_HINT.encode(), b"".join(result))

    async def test_hints_repeat_between_real_thoughts_without_cancelling_read(self):
        thought_ready, answer_ready = asyncio.Event(), asyncio.Event()
        starts = []

        async def upstream(**kwargs):
            starts.append(1)
            await thought_ready.wait()
            yield event([{"text": "Real thought", "thought": True}])
            await answer_ready.wait()
            yield event([{"functionCall": {"name": "output_reply", "args": {"content": "Answer"}}}])

        self.assertEqual((anti.FIRST_WAIT_HINT_SECONDS, anti.WAIT_HINT_INTERVAL_SECONDS), (20, 30))
        with patch.object(anti, "FIRST_WAIT_HINT_SECONDS", 0.01), patch.object(anti, "WAIT_HINT_INTERVAL_SECONDS", 0.01):
            stream = anti.anti_truncation_gemini_stream(request(), upstream)
            first = await anext(stream)
            self.assertEqual(text(first).strip(), anti.FIRST_WAIT_HINT)
            converted = json.loads(convert_gemini_to_openai_stream(first, "test", "test")[6:])
            delta = converted["choices"][0]["delta"]
            self.assertNotIn("content", delta)
            self.assertEqual(delta["reasoning_content"].strip(), anti.FIRST_WAIT_HINT)
            thought_ready.set()
            self.assertEqual(text(await anext(stream)), "Real thought")
            self.assertEqual(text(await anext(stream)).strip(), anti.WAIT_HINT)
            self.assertEqual(text(await anext(stream)).strip(), anti.WAIT_HINT)
            answer_ready.set()
            self.assertEqual(text(await anext(stream)), "Answer")
            self.assertEqual(await anext(stream), b"data: [DONE]\n\n")
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertEqual(starts, [1])

    async def test_disconnect_closes_pending_upstream(self):
        closed = asyncio.Event()

        async def upstream(**kwargs):
            try:
                await asyncio.Event().wait()
                yield b""
            finally:
                closed.set()

        with patch.object(anti, "FIRST_WAIT_HINT_SECONDS", 0.01):
            stream = anti.anti_truncation_gemini_stream(request(), upstream)
            await anext(stream)
            await stream.aclose()
        self.assertTrue(closed.is_set())

    async def test_nonstream_has_no_notices_and_keeps_usage(self):
        async def upstream(**kwargs):
            await asyncio.sleep(0.02)
            yield event([{"functionCall": {"name": "output_reply", "args": {"content": "Answer"}}}])
            yield anti._sse({"usageMetadata": {"totalTokenCount": 7}})

        with patch.object(anti, "FIRST_WAIT_HINT_SECONDS", 0.001), patch(
            "src.api.utils.get_empty_output_error_enabled", new=AsyncMock(return_value=True),
        ):
            response = await anti.collect_anti_truncation_response(request(), upstream)
        data = json.loads(response.body)
        self.assertEqual(data["candidates"][0]["content"]["parts"], [{"text": "Answer"}])
        self.assertEqual(data["usageMetadata"]["totalTokenCount"], 7)

    async def test_fast_error_is_unchanged_and_error_after_hint_is_not_swallowed(self):
        for delayed in (False, True):
            ready = asyncio.Event()
            error = Response(status_code=400)

            async def upstream(**kwargs):
                if delayed:
                    await ready.wait()
                yield error

            with patch.object(anti, "FIRST_WAIT_HINT_SECONDS", 0.01):
                stream = anti.anti_truncation_gemini_stream(request(), upstream)
                if delayed:
                    self.assertEqual(text(await anext(stream)).strip(), anti.FIRST_WAIT_HINT)
                    ready.set()
                self.assertIs(await anext(stream), error)
                await stream.aclose()


if __name__ == "__main__":
    unittest.main()
