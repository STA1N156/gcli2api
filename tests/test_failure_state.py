import ast
import asyncio
import __future__
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiosqlite


ROOT = Path(__file__).resolve().parents[1]


def load_class(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    scope = dict(asyncio=asyncio, asynccontextmanager=asynccontextmanager, suppress=suppress,
                 aiosqlite=aiosqlite, os=os, json=json, time=time,
                 log=SimpleNamespace(error=lambda *a: None, debug=lambda *a: None, warning=lambda *a: None))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    return scope[name]


class FailureStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_and_error_commit_together_preserving_other_models(self):
        cls = load_class("src/storage/sqlite_manager.py", "SQLiteManager")
        manager = cls()
        manager._initialized = True
        db = await aiosqlite.connect(":memory:")
        self.addAsyncCleanup(db.close)
        await db.execute("CREATE TABLE antigravity_credentials(filename TEXT PRIMARY KEY, model_cooldowns TEXT, error_codes TEXT, error_messages TEXT, updated_at REAL, disabled INTEGER)")
        await db.execute("INSERT INTO antigravity_credentials VALUES ('account.json','{\"other\":123}','[]','{}',1,1)")
        await db.commit()
        statements = []
        await db.set_trace_callback(statements.append)

        @asynccontextmanager
        async def connection(**kwargs):
            yield db

        manager._connection = connection
        self.assertTrue(await manager.set_model_cooldown("account.json", "gemini-3.1-flash-lite", 999, error_code=429, error_message="quota"))
        self.assertEqual(sum(s == "COMMIT" for s in statements), 1)
        row = await (await db.execute("SELECT model_cooldowns,error_codes,error_messages,disabled FROM antigravity_credentials")).fetchone()
        self.assertEqual(json.loads(row[0]), {"other": 123, "gemini-3.1-flash-lite": 999})
        self.assertEqual((json.loads(row[1]), json.loads(row[2]), row[3]), ([429], {"429": "quota"}, 1))
        await manager.set_model_cooldown("account.json", "gemini-3.1-flash-lite", None)
        row = await (await db.execute("SELECT model_cooldowns,error_codes FROM antigravity_credentials")).fetchone()
        self.assertEqual((json.loads(row[0]), json.loads(row[1])), ({"other": 123}, [429]))

    async def test_manager_uses_one_save_with_or_without_cooldown(self):
        cls = load_class("src/credential_manager.py", "CredentialManager")
        manager = cls.__new__(cls)
        manager._ensure_initialized = AsyncMock()
        manager.update_credential_state = AsyncMock()
        backend = SimpleNamespace(set_model_cooldown=AsyncMock())
        manager._storage_adapter = SimpleNamespace(_backend=backend)
        await manager.record_api_call_result("account.json", False, error_code=429, model_name="m", cooldown_until=999)
        backend.set_model_cooldown.assert_awaited_once()
        manager.update_credential_state.assert_not_awaited()
        backend.set_model_cooldown.reset_mock()
        await manager.record_api_call_result("account.json", False, error_code=503)
        backend.set_model_cooldown.assert_not_awaited()
        manager.update_credential_state.assert_awaited_once_with("account.json", {"error_codes": [503], "error_messages": {}}, mode="antigravity")
