import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiosqlite

from test_failure_state import load_class
from src.model_cooldown import has_active_model_cooldown


def manager(adapter):
    cls = load_class("src/credential_manager.py", "CredentialManager")
    cls.get_valid_credential.__globals__.update(
        hashlib=hashlib, has_active_model_cooldown=has_active_model_cooldown,
        SESSION_BINDING_MAX_ENTRIES=50000, SESSION_BINDING_PRUNE_INTERVAL_SECONDS=60,
        SESSION_BINDING_CACHE_SECONDS=10, SESSION_BINDING_RENEW_SECONDS=60, ROUND_ROBIN_MAX_KEYS=4096,
        get_session_affinity_enabled=AsyncMock(return_value=False),
        create_managed_task=asyncio.create_task,
    )
    item = cls()
    item._initialized = True
    item._storage_adapter = adapter
    return item


class SessionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixteen_workers_commit_one_renewal_and_preserve_changed_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.db"
            db = await aiosqlite.connect(path)
            backends = []
            try:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("CREATE TABLE session_bindings(binding_key TEXT PRIMARY KEY,filename TEXT,expires_at REAL,updated_at REAL)")
                now = time.time()
                key = hashlib.sha256(b"session").hexdigest()
                await db.execute("INSERT INTO session_bindings VALUES(?,?,?,?)", (key, "a.json", now + 1000, now - 61))
                await db.commit()
                commits = []
                cls = load_class("src/storage/sqlite_manager.py", "SQLiteManager")
                workers = []
                for _ in range(16):
                    backend = cls()
                    backend._db_path = str(path)
                    backend._initialized = True
                    original = backend._connection

                    @asynccontextmanager
                    async def connection(*, write=False, original=original):
                        async with original(write=write) as con:
                            await con.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
                            yield con

                    backend._connection = connection
                    backends.append(backend)
                    workers.append(manager(backend))
                self.assertEqual(await asyncio.gather(*(w._get_session_binding("session", 1800) for w in workers)), ["a.json"] * 16)
                await asyncio.gather(*(w._remember_session_binding("session", "a.json", 1800) for w in workers))
                await asyncio.gather(*(w.close() for w in workers))
                self.assertEqual(len(commits), 1)
                await backends[0].set_session_binding(key, "b.json", time.time() + 1800)
                self.assertFalse(await backends[1].renew_session_binding(key, "a.json", time.time() + 2000, time.time() + 60))
                self.assertEqual((await backends[1].get_session_binding(key, time.time()))[0], "b.json")
                await backends[0].delete_session_binding(key)
                self.assertFalse(await backends[1].renew_session_binding(key, "a.json", time.time() + 2000, time.time() + 60))
                self.assertIsNone(await backends[1].get_session_binding(key, time.time()))
            finally:
                await asyncio.gather(*(b.close() for b in backends))
                await db.close()

    async def test_new_binding_waits_but_same_account_renewal_does_not(self):
        gate = asyncio.Event()

        async def save(*args):
            await gate.wait()
            return True

        adapter = SimpleNamespace(set_session_binding=AsyncMock(side_effect=save), renew_session_binding=AsyncMock(side_effect=save))
        worker = manager(adapter)
        task = asyncio.create_task(worker._remember_session_binding("session", "a.json", 1800))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        gate.set()
        await task
        gate.clear()
        now = time.time()
        worker._session_bindings["session"] = ("a.json", now + 10, now - 1)
        await asyncio.wait_for(worker._remember_session_binding("session", "a.json", 1800), .2)
        await asyncio.sleep(0)
        adapter.renew_session_binding.assert_awaited_once()
        await worker._remember_session_binding("session", "a.json", 1800)
        self.assertEqual(len(worker._session_renewals), 1)
        gate.set()
        await worker.close()

    async def test_retries_reuse_order_but_check_current_disabled_and_cooldown_state(self):
        states = {name: {"disabled": False, "model_cooldowns": {}} for name in ["a.json", "b.json", "c.json", "d.json"]}

        async def latest(filename, **kwargs):
            return {"access_token": "test"}, states[filename]

        adapter = SimpleNamespace(get_all_credential_states=AsyncMock(return_value=states),
                                  get_credential_with_state=AsyncMock(side_effect=latest))
        worker = manager(adapter)
        worker._should_refresh_token = AsyncMock(return_value=False)
        selection = {}
        first = await worker.get_valid_credential(model_name="m", selection_state=selection)
        self.assertEqual(first[0], "a.json")
        states["b.json"]["disabled"] = True
        states["c.json"]["model_cooldowns"] = {"m": time.time() + 999}
        second = await worker.get_valid_credential(model_name="m", exclude_credentials={"a.json"}, selection_state=selection)
        self.assertEqual(second[0], "d.json")
        adapter.get_all_credential_states.assert_awaited_once()
        self.assertEqual(adapter.get_credential_with_state.await_count, 4)
