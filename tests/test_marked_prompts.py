import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src import marked_prompts
from src.api.empty_retry import empty_output_stream, wrap_user_input_in_tool
from src.converter.anti_truncation import apply_anti_truncation, latest_user_text_parts
from src.panel import marked_prompts as routes
from src.storage.sqlite_manager import SQLiteManager
from src.utils import verify_panel_token


class MarkedPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = SQLiteManager()
        with patch.dict(os.environ, {"CREDENTIALS_DIR": self.temp.name}):
            await self.storage.initialize()
        self.body = {"model": "gemini-4-argon-high", "request": {
            "systemInstruction": {"parts": [{"text": "测试系统提示词 <script>alert(1)</script>"}]},
            "contents": [{"role": "user", "parts": [{"text": "走吧"}]}],
        }}

    async def asyncTearDown(self):
        await self.storage.close()
        self.temp.cleanup()

    async def remember(self, body=None):
        body = body or self.body
        with patch.object(marked_prompts, "get_storage_adapter", AsyncMock(return_value=self.storage)):
            await marked_prompts.remember_marked_prompt(body["request"], body["model"])

    async def test_exact_match_is_shared_deduplicated_and_keeps_one_retry(self):
        await asyncio.gather(*(self.remember() for _ in range(8)))
        listing = await self.storage.list_marked_prompts(0, 30)
        self.assertEqual(listing["total"], 1)
        self.assertNotIn("text", listing["items"][0])
        body = apply_anti_truncation(self.body)
        parts = latest_user_text_parts(body["request"])
        second_worker = SQLiteManager()
        second_worker._db_path = self.storage._db_path
        with patch.object(marked_prompts, "get_storage_adapter", AsyncMock(return_value=second_worker)):
            await marked_prompts.relocate_known_prompt(body)
        self.assertNotIn("systemInstruction", body["request"])
        self.assertIn("测试系统提示词", body["request"]["contents"][0]["parts"][0]["text"])
        wrap_user_input_in_tool(body, parts)
        self.assertEqual(body["request"]["contents"][-1]["parts"][0]["functionResponse"]["response"]["text"], "走吧")
        different = {"systemInstruction": {"parts": [{"text": "测试系统提示词"}]}}
        self.assertFalse(await self.storage.has_marked_prompt(marked_prompts.system_prompt_identity(different)[0]))
        self.assertIsNone(marked_prompts.system_prompt_identity(apply_anti_truncation({"request": {}})["request"]))
        self.assertEqual(await self.storage.get_all_config(), {})

    async def test_downloads_keep_full_utf8_text_and_require_auth(self):
        await self.remember()
        prompt_id, text = marked_prompts.system_prompt_identity(self.body["request"])
        app = FastAPI()
        app.include_router(routes.router)
        with patch.object(routes, "get_storage_adapter", AsyncMock(return_value=self.storage)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertNotEqual((await client.get("/marked-prompts")).status_code, 200)
                app.dependency_overrides[verify_panel_token] = lambda: "test-admin"
                listing = (await client.get("/marked-prompts")).json()
                self.assertEqual(listing["total"], 1)
                response = await client.get(f"/marked-prompts/{prompt_id}/download")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content.decode("utf-8-sig"), text)
                response = await client.post("/marked-prompts/download", json={"ids": [prompt_id, prompt_id]})
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    self.assertEqual(len(archive.namelist()), 1)
                    self.assertEqual(archive.read(archive.namelist()[0]).decode("utf-8-sig"), text)
                self.assertEqual((await client.get("/marked-prompts/bad/download")).status_code, 400)
                self.assertEqual((await client.get(f'/marked-prompts/{"0" * 64}/download')).status_code, 404)
                self.assertEqual((await client.post("/marked-prompts/download", json={"ids": []})).status_code, 422)

    async def test_known_prompt_skips_failed_original_but_keeps_one_empty_retry(self):
        await self.remember()
        seen = []

        async def upstream(body, **kwargs):
            await marked_prompts.relocate_known_prompt(body)
            seen.append(copy.deepcopy(body["request"]))
            parts = [] if len(seen) == 1 else [{"text": "OK"}]
            yield 'data: ' + json.dumps({"candidates": [{"content": {"parts": parts}}]}) + '\n\n'

        with patch.object(marked_prompts, "get_storage_adapter", AsyncMock(return_value=self.storage)):
            result = [chunk async for chunk in empty_output_stream(self.body, upstream)]
        self.assertEqual(len(seen), 2)
        self.assertTrue(all("systemInstruction" not in request for request in seen))
        self.assertEqual(seen[0]["contents"][-1]["parts"][-1]["text"], "走吧")
        self.assertEqual(seen[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["text"], "走吧")
        self.assertTrue(any(b"OK" in chunk for chunk in result))

    async def test_storage_failure_does_not_break_or_change_request(self):
        original = copy.deepcopy(self.body)
        storage = AsyncMock()
        storage.has_marked_prompt.side_effect = RuntimeError("storage unavailable")
        storage.save_marked_prompt.side_effect = RuntimeError("storage unavailable")
        with patch.object(marked_prompts, "get_storage_adapter", AsyncMock(return_value=storage)):
            await marked_prompts.relocate_known_prompt(self.body)
            await marked_prompts.remember_marked_prompt(self.body["request"], self.body["model"])
        self.assertEqual(self.body, original)


if __name__ == "__main__":
    unittest.main()
