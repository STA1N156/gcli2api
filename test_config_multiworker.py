import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config


class MultiWorkerConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_worker_cache_reloads_from_shared_storage(self):
        original = (
            config._config_cache,
            config._config_initialized,
            config._config_loaded_at,
        )
        backend = SimpleNamespace(reload_config_cache=AsyncMock())
        storage_adapter = SimpleNamespace(
            _backend=backend,
            get_all_config=AsyncMock(return_value={"max_retry_credentials": 9}),
        )

        try:
            config._config_cache = {"max_retry_credentials": 2}
            config._config_initialized = True
            config._config_loaded_at = 0

            with patch(
                "src.storage_adapter.get_storage_adapter",
                AsyncMock(return_value=storage_adapter),
            ):
                value = await config.get_config_value("max_retry_credentials")

            self.assertEqual(value, 9)
            backend.reload_config_cache.assert_awaited_once()
        finally:
            (
                config._config_cache,
                config._config_initialized,
                config._config_loaded_at,
            ) = original


if __name__ == "__main__":
    unittest.main()
