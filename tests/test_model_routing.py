import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Response
import httpx

from src.api import antigravity
from src.credential_manager import CredentialManager
from src.model_support import supports_model
from src.storage.sqlite_manager import SQLiteManager


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.backend = SQLiteManager()
        self.backend._db_path = str(Path(self.directory.name) / "credentials.db")
        self.backend._initialized = True
        async with self.backend._connection(write=True) as db:
            await self.backend._create_tables(db)
            await self.backend._ensure_schema_compatibility(db)
            await db.commit()
        for name in ("a.json", "b.json", "z.json"):
            await self.backend.store_credential(name, {"access_token": name, "project_id": "test-project"})
        self.adapter = SimpleNamespace(**{name: getattr(self.backend, name) for name in (
            "get_credential", "get_credential_state", "get_credential_with_state",
            "get_all_credential_states", "update_credential_state")})

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    def manager(self):
        manager = CredentialManager()
        manager._initialized = True
        manager._storage_adapter = self.adapter
        manager._should_refresh_token = AsyncMock(return_value=False)
        return manager

    async def test_supported_models_persist_and_prefer_matches_then_unknown(self):
        await self.backend.update_credential_state("b.json", {"supported_models": ["flash"]})
        await self.backend.update_credential_state("z.json", {"supported_models": ["pro"]})
        manager = self.manager()
        with patch("src.credential_manager.get_session_affinity_enabled", AsyncMock(return_value=False)):
            selection = {}
            first = await manager.get_valid_credential(model_name="pro", selection_state=selection)
            second = await manager.get_valid_credential(model_name="pro", exclude_credentials={"z.json"}, selection_state=selection)
        self.assertEqual((first[0], second[0]), ("z.json", "a.json"))
        self.assertEqual(first[1]["access_token"], "z.json")
        self.assertIsNone(await manager._get_bound_credential_if_available("b.json", mode="antigravity", model_name="pro", exclude_credentials=set()))
        await self.backend.close()
        async with self.backend._connection() as db:
            row = await (await db.execute("SELECT supported_models FROM antigravity_credentials WHERE filename='z.json'")).fetchone()
        self.assertEqual(json.loads(row[0]), ["pro"])
        self.assertIsNone(supports_model([], "pro"))
        self.assertTrue(supports_model(["claude-sonnet-4-6"], "claude-sonnet-4-6-thinking"))

    async def test_model_refresh_is_coalesced_and_public_list_uses_one_account(self):
        await self.backend.update_credential_state("z.json", {"disabled": True})
        await self.backend.update_credential_state("b.json", {"supported_models": ["pro"]})
        post = AsyncMock(return_value=httpx.Response(200, json={"models": {"flash": {}}}))
        manager = self.manager()
        manager.get_valid_credential = AsyncMock(return_value=("a.json", await self.backend.get_credential("a.json")))
        with patch.object(antigravity, "get_storage_adapter", AsyncMock(return_value=self.adapter)), \
             patch.object(antigravity, "credential_manager", manager), \
             patch.object(antigravity, "get_antigravity_api_url", AsyncMock(return_value="http://mock")), \
             patch.object(antigravity, "post_async", post):
            self.assertEqual(await asyncio.gather(*(antigravity.refresh_credential_models("a.json") for _ in range(5))), [["flash"]] * 5)
            post.assert_awaited_once()
            self.assertEqual(post.call_args.kwargs["json"], {"project": "test-project"})
            models = await antigravity.fetch_available_models()
            self.assertEqual({item["id"] for item in models}, {"flash"})
            self.assertEqual(post.await_count, 2)
            manager.get_valid_credential.assert_awaited_once_with(mode="antigravity")
            post.return_value = httpx.Response(503)
            self.assertIsNone(await antigravity.refresh_credential_models("a.json"))
        self.assertEqual((await self.backend.get_credential_state("a.json"))["supported_models"], ["flash"])

    async def test_model_404_switches_account_in_both_response_modes(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), ExitStack() as stack:
                await self.backend.update_credential_state("a.json", {"supported_models": ["pro", "flash"]})
                await self.backend.update_credential_state("b.json", {"supported_models": ["pro"]})
                await self.backend.update_credential_state("z.json", {"disabled": True})
                manager = self.manager()
                calls = []

                async def post(**kwargs):
                    calls.append(kwargs["headers"]["Authorization"])
                    return httpx.Response(404, json={"error": "model unavailable"}) if len(calls) == 1 else httpx.Response(200, json={"ok": True})

                async def stream(**kwargs):
                    response = await post(**kwargs)
                    yield Response(response.content, response.status_code) if response.status_code != 200 else b"OK"

                for name, value in {
                    "credential_manager": manager, "get_storage_adapter": AsyncMock(return_value=self.adapter),
                    "get_antigravity_stream2nostream": AsyncMock(return_value=False),
                    "get_empty_output_error_enabled": AsyncMock(return_value=False),
                    "get_antigravity_api_url": AsyncMock(return_value="http://mock"),
                    "get_retry_config": AsyncMock(return_value={"max_credentials": 2, "retry_interval": 0}),
                    "refresh_credential_models": AsyncMock(return_value=None),
                    "record_api_call_success": AsyncMock(), "record_api_call_success_later": Mock(),
                    "record_api_call_error": AsyncMock(), "post_async": post, "stream_post_async": stream,
                }.items():
                    stack.enter_context(patch.object(antigravity, name, value))
                stack.enter_context(patch("src.credential_manager.get_session_affinity_enabled", AsyncMock(return_value=False)))
                body = {"model": "pro", "request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}}
                if streaming:
                    output = [chunk async for chunk in antigravity.stream_request.__wrapped__(body, native=True)]
                    self.assertEqual(output, [b"OK"])
                else:
                    self.assertEqual((await antigravity.non_stream_request.__wrapped__(body)).status_code, 200)
                await asyncio.sleep(0)
                self.assertEqual(calls, ["Bearer a.json", "Bearer b.json"])
                state = await self.backend.get_credential_state("a.json")
                self.assertEqual(state["supported_models"], ["flash"])
                self.assertFalse(state["disabled"])
                antigravity.record_api_call_error.assert_not_awaited()
