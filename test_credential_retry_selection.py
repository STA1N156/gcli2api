import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from src.api.antigravity import non_stream_request
from src.credential_manager import CredentialManager


class CredentialRetrySelectionTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, states):
        backend = SimpleNamespace(
            get_next_available_credential=AsyncMock(return_value=None)
        )
        storage_adapter = SimpleNamespace(
            _backend=backend,
            get_all_credential_states=AsyncMock(return_value=states),
            get_credential=AsyncMock(return_value={
                "access_token": "token",
                "expiry": "2099-01-01T00:00:00+00:00",
            }),
        )
        manager = CredentialManager()
        manager._storage_adapter = storage_adapter
        manager._initialized = True
        return manager, backend

    async def test_antigravity_retry_accumulates_every_attempted_credential(self):
        credentials = [
            ("a.json", {"access_token": "a", "project_id": "a-project"}),
            ("b.json", {"access_token": "b", "project_id": "b-project"}),
            ("c.json", {"access_token": "c", "project_id": "c-project"}),
        ]
        get_credential = AsyncMock(side_effect=credentials)
        responses = [
            SimpleNamespace(status_code=429, text="{}", content=b"{}", headers={}),
            SimpleNamespace(status_code=429, text="{}", content=b"{}", headers={}),
            SimpleNamespace(status_code=200, text="{}", content=b"{}", headers={}),
        ]

        with patch.multiple(
            "src.api.antigravity",
            credential_manager=SimpleNamespace(get_valid_credential=get_credential),
            get_antigravity_stream2nostream=AsyncMock(return_value=False),
            get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
            wrap_cli_request=AsyncMock(return_value=({"project": "a-project"}, {})),
            get_retry_config=AsyncMock(return_value={
                "max_retries": 2,
                "retry_interval": 0,
                "retry_enabled": True,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            get_auto_ban_error_codes=AsyncMock(return_value=[403]),
            post_async=AsyncMock(side_effect=responses),
            handle_error_with_retry=AsyncMock(return_value=True),
            record_api_call_error=AsyncMock(),
            record_api_call_success=AsyncMock(),
        ):
            response = await non_stream_request({"model": "gemini-3.1-pro-preview"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            get_credential.await_args_list,
            [
                call(
                    mode="antigravity",
                    model_name="gemini-3.1-pro-preview",
                    session_key=None,
                ),
                call(
                    mode="antigravity",
                    model_name="gemini-3.1-pro-preview",
                    session_key=None,
                    exclude_credentials={"a.json"},
                ),
                call(
                    mode="antigravity",
                    model_name="gemini-3.1-pro-preview",
                    session_key=None,
                    exclude_credentials={"a.json", "b.json"},
                ),
            ],
        )

    async def test_retry_excludes_attempted_disabled_and_family_cooled_credentials(self):
        states = {
            "attempted.json": {"disabled": False, "model_cooldowns": {}},
            "disabled.json": {"disabled": True, "model_cooldowns": {}},
            "cooled.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-3-flash": 4102444800},
            },
            "available.json": {"disabled": False, "model_cooldowns": {}},
        }
        manager, backend = self.make_manager(states)

        result = await manager.get_valid_credential(
            mode="antigravity",
            model_name="gemini-3.1-pro-preview",
            exclude_credentials={"attempted.json"},
        )

        self.assertEqual(result[0], "available.json")
        backend.get_next_available_credential.assert_not_awaited()

    async def test_session_retry_uses_a_fresh_route_order(self):
        manager, _ = self.make_manager({})
        manager._get_session_routed_credential = AsyncMock(return_value=(
            "available.json",
            {
                "access_token": "token",
                "expiry": "2099-01-01T00:00:00+00:00",
            },
        ))

        with patch("src.credential_manager.time.time_ns", return_value=123):
            result = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="rp-hub-chat",
                exclude_credentials={"attempted.json"},
            )

        self.assertEqual(result[0], "available.json")
        self.assertEqual(
            manager._get_session_routed_credential.await_args.kwargs["binding_key"],
            "retry:123",
        )

    async def test_claude_request_can_use_credential_with_only_gemini_cooldown(self):
        states = {
            "gemini-cooled.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-3-flash": 4102444800},
            },
            "claude-cooled.json": {
                "disabled": False,
                "model_cooldowns": {"claude-sonnet-4-6": 4102444800},
            },
        }
        manager, _ = self.make_manager(states)

        result = await manager.get_valid_credential(
            mode="antigravity",
            model_name="claude-sonnet-4-6",
            exclude_credentials={"already-tried.json"},
        )

        self.assertEqual(result[0], "gemini-cooled.json")


if __name__ == "__main__":
    unittest.main()
