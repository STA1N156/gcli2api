import os
import tempfile
import time
import unittest
from unittest.mock import patch

from src.storage.sqlite_manager import SQLiteManager


class SQLiteRecordSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_only_the_successful_models_old_state(self):
        with tempfile.TemporaryDirectory() as credentials_dir, patch.dict(
            os.environ, {"CREDENTIALS_DIR": credentials_dir}
        ):
            manager = SQLiteManager()
            await manager.initialize()
            await manager.store_credential("credential.json", {"token": "test"})
            await manager.update_credential_state(
                "credential.json",
                {
                    "error_codes": [429],
                    "error_messages": {"429": "old error"},
                },
            )

            expires_at = time.time() + 3600
            await manager.set_model_cooldown(
                "credential.json", "gemini-3.1-pro-preview", expires_at
            )
            await manager.set_model_cooldown(
                "credential.json", "gemini-2.5-pro", expires_at
            )

            await manager.record_success(
                "credential.json", "gemini-3.1-pro-preview"
            )

            state = await manager.get_credential_state("credential.json")
            self.assertEqual(state["error_codes"], [])
            self.assertNotIn(
                "gemini-3.1-pro-preview", state["model_cooldowns"]
            )
            self.assertIn("gemini-2.5-pro", state["model_cooldowns"])


if __name__ == "__main__":
    unittest.main()
