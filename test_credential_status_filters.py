import os
import tempfile
import time
import unittest
from unittest.mock import patch

from src.credential_status import (
    credential_status_flags,
    matches_cooldown_filter,
    matches_status_filter,
)
from src.storage.sqlite_manager import SQLiteManager


class CredentialStatusFilterTests(unittest.TestCase):
    def test_model_family_abnormal_status_is_separate(self):
        gemini = credential_status_flags(
            False, [429], {"gemini-3.1-pro-preview": 2000}, "antigravity"
        )
        claude = credential_status_flags(
            False, [429], {"claude-sonnet-4-6": 2000}, "antigravity"
        )

        self.assertTrue(matches_status_filter("gemini_abnormal", gemini))
        self.assertFalse(matches_status_filter("claude_abnormal", gemini))
        self.assertTrue(matches_status_filter("claude_abnormal", claude))
        self.assertFalse(matches_status_filter("gemini_abnormal", claude))

    def test_cooldown_filter_supports_each_family_and_all(self):
        cooldowns = {
            "gemini-3.1-pro-preview": 2000,
            "claude-sonnet-4-6": 2000,
        }

        self.assertTrue(matches_cooldown_filter("gemini_cooldown", cooldowns))
        self.assertTrue(matches_cooldown_filter("claude_cooldown", cooldowns))
        self.assertTrue(matches_cooldown_filter("in_cooldown", cooldowns))
        self.assertFalse(matches_cooldown_filter("no_cooldown", cooldowns))

    def test_unscoped_antigravity_error_affects_both_families(self):
        flags = credential_status_flags(True, [], {}, "antigravity")

        self.assertTrue(flags["claude_abnormal"])
        self.assertTrue(flags["gemini_abnormal"])

    def test_disabled_credential_affects_both_families_with_cooldown(self):
        flags = credential_status_flags(
            True, [403], {"gemini-3.1-pro-preview": 2000}, "antigravity"
        )

        self.assertTrue(flags["claude_abnormal"])
        self.assertTrue(flags["gemini_abnormal"])


class CredentialSummaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_summary_and_family_filters(self):
        with tempfile.TemporaryDirectory() as credentials_dir, patch.dict(
            os.environ, {"CREDENTIALS_DIR": credentials_dir}
        ):
            manager = SQLiteManager()
            await manager.initialize()
            for filename in ("normal.json", "gemini.json", "claude.json", "disabled.json"):
                await manager.store_credential(
                    filename, {"token": filename}, mode="antigravity"
                )

            expires_at = time.time() + 3600
            await manager.set_model_cooldown(
                "gemini.json", "gemini-3.1-pro-preview", expires_at, "antigravity"
            )
            await manager.set_model_cooldown(
                "claude.json", "claude-sonnet-4-6", expires_at, "antigravity"
            )
            await manager.set_model_cooldown(
                "disabled.json", "gemini-3.1-pro-preview", expires_at, "antigravity"
            )
            await manager.update_credential_state(
                "disabled.json", {"disabled": True}, mode="antigravity"
            )

            summary = await manager.get_credentials_summary(mode="antigravity")
            self.assertEqual(
                summary["stats"],
                {
                    "total": 4,
                    "normal": 1,
                    "abnormal": 3,
                    "claude_abnormal": 2,
                    "gemini_abnormal": 2,
                },
            )

            claude = await manager.get_credentials_summary(
                mode="antigravity", status_filter="claude_abnormal"
            )
            gemini_cooldown = await manager.get_credentials_summary(
                mode="antigravity", cooldown_filter="gemini_cooldown"
            )
            self.assertEqual(
                {item["filename"] for item in claude["items"]},
                {"claude.json", "disabled.json"},
            )
            self.assertEqual(
                {item["filename"] for item in gemini_cooldown["items"]},
                {"gemini.json", "disabled.json"},
            )

if __name__ == "__main__":
    unittest.main()
