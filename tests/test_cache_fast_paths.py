import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.agy_transport import NativeBridge
from src.antigravity_identity import derive_conversation_anchor
from src.api.antigravity import wrap_cli_request
from src.httpx_client import HttpxClientManager
from src.session_affinity import extract_cache_session_key


class CacheFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_client_and_bridge_skip_creation_locks(self):
        manager = HttpxClientManager()
        manager.get_client_kwargs = AsyncMock(return_value={"account_id": "account"})
        cached = SimpleNamespace(is_closed=False)
        manager._clients["account" + manager._client_cache_key({})] = cached
        async with manager._lock:
            self.assertIs(await asyncio.wait_for(manager._get_or_create_client(), .2), cached)

        bridge = NativeBridge()
        bridge.process = SimpleNamespace(returncode=None)
        bridge.address = {"url": "http://127.0.0.1:1", "key": "test"}
        async with bridge.lock:
            self.assertIs(await asyncio.wait_for(bridge.start(), .2), bridge.address)

    def test_normalized_anchor_distinguishes_sessions_models_and_systems(self):
        request = {"model": "model-a", "systemInstruction": {"parts": [{"text": "same system"}]},
                   "contents": [{"role": "user", "parts": [{"text": "first"}]}]}
        original = copy.deepcopy(request)
        key = extract_cache_session_key({"messages": []}, gemini_request=request)
        self.assertEqual(key, "auto:" + derive_conversation_anchor("model-a", request).hex()[:32])
        self.assertEqual(key, extract_cache_session_key({"system": "raw"}, gemini_request=request))
        variants = []
        for field in ("model", "system", "message"):
            changed = copy.deepcopy(request)
            if field == "model":
                changed["model"] = "model-b"
            elif field == "system":
                changed["systemInstruction"]["parts"][0]["text"] = "other system"
            else:
                changed["contents"][0]["parts"][0]["text"] = "second"
            variants.append(extract_cache_session_key({}, gemini_request=changed))
        self.assertEqual(len({key, *variants}), 4)
        self.assertEqual(request, original)

    async def test_appended_round_keeps_anchor_and_upstream_ids(self):
        request = {"model": "model-a", "systemInstruction": {"parts": [{"text": "system"}]},
                   "contents": [{"role": "user", "parts": [{"text": "first"}]},
                                {"role": "model", "parts": [{"text": "reply"}]}]}
        continued = copy.deepcopy(request)
        continued["contents"] += [{"role": "user", "parts": [{"text": "next"}]},
                                  {"role": "model", "parts": [{"text": "next reply"}]}]
        self.assertEqual(extract_cache_session_key(request), extract_cache_session_key(continued))
        first, _ = await wrap_cli_request(request, "model-a", "project")
        second, _ = await wrap_cli_request(continued, "model-a", "project")
        self.assertEqual(first["request"]["sessionId"], second["request"]["sessionId"])
        self.assertEqual(first["request"]["labels"]["trajectory_id"], second["request"]["labels"]["trajectory_id"])
        self.assertEqual(first["request"]["labels"]["last_step_index"], "2")
        self.assertEqual(second["request"]["labels"]["last_step_index"], "3")

    def test_explicit_session_ids_keep_priority(self):
        request = {"model": "model-a", "contents": [{"role": "user", "parts": [{"text": "first"}]}]}
        self.assertEqual(extract_cache_session_key({"cache_session_key": "explicit"}, gemini_request=request), "explicit")
        self.assertEqual(extract_cache_session_key({}, {"x-session-id": "session"}, gemini_request=request), "header:session")
        self.assertEqual(extract_cache_session_key({}, gemini_request={**request, "sessionId": "client"}), "gemini-session:client")
