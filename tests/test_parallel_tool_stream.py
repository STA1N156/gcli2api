import json
import unittest

from src.converter.openai2gemini import convert_gemini_to_openai_stream
from src.router.anti_truncation import anti_truncation_gemini_stream


def event(parts=(), finish=None):
    candidate = {"index": 0, "content": {"role": "model", "parts": list(parts)}}
    if finish:
        candidate["finishReason"] = finish
    return "data: " + json.dumps({"response": {"candidates": [candidate]}}) + "\n\n"


CALLS = [
    {"functionCall": {"id": "call_weather", "name": "weather", "args": {"city": "Beijing"}}},
    {"functionCall": {"id": "call_clock", "name": "clock", "args": {"city": "Shanghai"}}},
]


def collect_client_calls(chunks):
    state, calls, finish = {}, {}, None
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.strip() == "data: [DONE]":
            continue
        converted = convert_gemini_to_openai_stream(text, "test", "response", model_state=state)
        for choice in json.loads(converted[6:])["choices"]:
            if choice["finish_reason"]:
                finish = choice["finish_reason"]
            for delta in choice["delta"].get("tool_calls", []):
                call = calls.setdefault(delta["index"], {"id": "", "name": "", "arguments": ""})
                call["id"] += delta.get("id", "")
                call["name"] += delta["function"].get("name", "")
                call["arguments"] += delta["function"].get("arguments", "")
    return calls, finish


class ParallelToolStreamTests(unittest.IsolatedAsyncioTestCase):
    def assert_two_calls(self, chunks):
        calls, finish = collect_client_calls(chunks)
        self.assertEqual(list(calls), [0, 1])
        self.assertEqual([call["name"] for call in calls.values()], ["weather", "clock"])
        self.assertEqual([json.loads(call["arguments"]) for call in calls.values()],
                         [{"city": "Beijing"}, {"city": "Shanghai"}])
        self.assertEqual(finish, "tool_calls")

    def test_separate_events_preserve_both_tools(self):
        self.assert_two_calls([event([CALLS[0]]), event([CALLS[1]]), event(finish="STOP")])

    def test_text_parts_do_not_create_gaps_in_tool_indexes(self):
        self.assert_two_calls([event([{"text": "Checking both."}, *CALLS], "STOP")])

    def test_truncation_keeps_length_finish_reason(self):
        calls, finish = collect_client_calls([event(CALLS), event(finish="MAX_TOKENS")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(finish, "length")

    async def test_anti_truncation_preserves_parallel_real_tools(self):
        async def upstream(**kwargs):
            for chunk in [event([CALLS[0]]), event([CALLS[1]]), event(finish="STOP")]:
                yield chunk

        payload = {"model": "gemini-3.8-flash-medium", "request": {
            "contents": [{"role": "user", "parts": [{"text": "Check weather and time."}]}],
            "tools": [{"functionDeclarations": [{"name": "weather"}, {"name": "clock"}]}],
        }}
        chunks = [chunk async for chunk in anti_truncation_gemini_stream(payload, upstream)]
        self.assert_two_calls(chunks)
