import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api.utils import parse_quota_reset_timestamp, record_api_call_error
from src.model_cooldown import has_active_model_cooldown
from src.panel.creds import clear_all_credential_abnormal_status


class ModelCooldownTests(unittest.TestCase):
    def test_antigravity_cooldown_only_blocks_the_exact_gemini_model(self):
        cooldowns = {"gemini-3-flash": 2000}

        self.assertTrue(
            has_active_model_cooldown(
                cooldowns, "gemini-3-flash", current_time=1000, mode="antigravity"
            )
        )
        self.assertFalse(
            has_active_model_cooldown(
                cooldowns, "gemini-3.1-pro-preview", current_time=1000, mode="antigravity"
            )
        )

    def test_geminicli_cooldown_only_blocks_the_exact_model(self):
        cooldowns = {"gemini-2.5-flash": 2000}

        self.assertFalse(
            has_active_model_cooldown(
                cooldowns, "gemini-3-flash", current_time=1000, mode="geminicli"
            )
        )

    def test_generic_resource_exhausted_uses_four_hour_cooldown(self):
        error = {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "Resource has been exhausted (e.g. check quota).",
            }
        }

        with patch("time.time", return_value=1000):
            self.assertEqual(
                parse_quota_reset_timestamp(error, mode="antigravity"),
                15400,
            )
            self.assertEqual(parse_quota_reset_timestamp(error), 15400)

    def test_antigravity_cooldown_only_blocks_the_exact_claude_model(self):
        cooldowns = {"claude-sonnet-4-6": 2000}

        self.assertFalse(
            has_active_model_cooldown(
                cooldowns, "claude-opus-4-6-thinking", current_time=1000, mode="antigravity"
            )
        )


class BatchAbnormalStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_403_sets_model_cooldown_without_disabling(self):
        manager = SimpleNamespace(record_api_call_result=AsyncMock())

        with patch("src.api.utils.time.time", return_value=1000):
            await record_api_call_error(
                manager,
                "credential.json",
                403,
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                error_message="permission denied",
            )

        manager.record_api_call_result.assert_awaited_once_with(
            "credential.json",
            False,
            403,
            cooldown_until=2800,
            mode="antigravity",
            model_name="gemini-3.1-pro-preview",
            error_message="permission denied",
        )

    async def test_refresh_abnormal_status_applies_to_every_abnormal_credential(self):
        backend = SimpleNamespace(
            clear_all_model_cooldowns=AsyncMock(return_value=True)
        )
        storage_adapter = SimpleNamespace(
            _backend=backend,
            update_credential_state=AsyncMock(return_value=True),
            get_all_credential_states=AsyncMock(return_value={
                "first.json": {"model_cooldowns": {"gemini-3-flash": 4102444800}},
                "second.json": {"model_cooldowns": {}},
                "third.json": {"model_cooldowns": {"claude-sonnet-4-6": 4102444800}},
            }),
        )

        with patch(
            "src.panel.creds.get_storage_adapter",
            AsyncMock(return_value=storage_adapter),
        ):
            response = await clear_all_credential_abnormal_status(
                token="test",
                mode="antigravity",
            )

        result = json.loads(response.body)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(
            backend.clear_all_model_cooldowns.await_args_list,
            [
                unittest.mock.call("first.json", mode="antigravity"),
                unittest.mock.call("third.json", mode="antigravity"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
