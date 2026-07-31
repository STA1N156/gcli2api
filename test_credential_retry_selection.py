import os
import tempfile
import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from fastapi import Response

from src.api.antigravity import non_stream_request as antigravity_request
from src.api.antigravity import stream_request as antigravity_stream_request
from src.api.antigravity import wrap_cli_request
from src.api.geminicli import non_stream_request as geminicli_request
from src.api.utils import get_retry_config, is_retryable_status
from src.credential_manager import CredentialManager
from src.session_affinity import extract_cache_session_key
from src.storage.sqlite_manager import SQLiteManager


def http_response(status_code, body=b"{}"):
    return SimpleNamespace(
        status_code=status_code,
        text=body.decode(),
        content=body,
        headers={},
    )


class SessionKeyTests(unittest.TestCase):
    def test_per_request_id_does_not_change_the_chat_session(self):
        payload = {
            "messages": [{"role": "user", "content": "first message"}]
        }
        first = extract_cache_session_key(
            payload, {"x-client-request-id": "request-a"}
        )
        second = extract_cache_session_key(
            payload, {"x-client-request-id": "request-b"}
        )

        self.assertEqual(first, second)


class CredentialSelectionTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, states, session_store=None):
        credentials = {
            filename: {
                "access_token": f"token-{filename}",
                "project_id": f"project-{filename}",
                "expiry": "2099-01-01T00:00:00+00:00",
            }
            for filename in states
        }
        storage_adapter = SimpleNamespace(
            get_all_credential_states=AsyncMock(return_value=states),
            get_credential=AsyncMock(
                side_effect=lambda filename, mode: credentials.get(filename)
            ),
            get_credential_state=AsyncMock(
                side_effect=lambda filename, mode: states.get(filename, {})
            ),
        )
        if session_store is not None:
            async def get_session_binding(binding_key, now):
                binding = session_store.get(binding_key)
                return binding[0] if binding and binding[1] > now else None

            async def set_session_binding(binding_key, filename, expires_at):
                session_store[binding_key] = (filename, expires_at)
                return True

            async def delete_session_binding(binding_key, expected_filename=None):
                binding = session_store.get(binding_key)
                if binding and (
                    expected_filename is None or binding[0] == expected_filename
                ):
                    session_store.pop(binding_key, None)
                return True

            storage_adapter.get_session_binding = get_session_binding
            storage_adapter.set_session_binding = set_session_binding
            storage_adapter.delete_session_binding = delete_session_binding
        manager = CredentialManager()
        manager._storage_adapter = storage_adapter
        manager._initialized = True
        return manager

    async def test_round_robin_is_the_only_default_selection_policy(self):
        manager = self.make_manager({
            "a.json": {"disabled": False, "model_cooldowns": {}},
            "b.json": {"disabled": False, "model_cooldowns": {}},
            "c.json": {"disabled": False, "model_cooldowns": {}},
        })

        with patch(
            "src.credential_manager.get_session_affinity_enabled",
            AsyncMock(return_value=False),
        ):
            selected = [
                (
                    await manager.get_valid_credential(
                        mode="antigravity", model_name="gemini-3.1-pro-preview"
                    )
                )[0]
                for _ in range(3)
            ]

        self.assertEqual(selected, ["a.json", "b.json", "c.json"])

    async def test_selector_filters_disabled_cooled_and_tried_credentials(self):
        manager = self.make_manager({
            "attempted.json": {"disabled": False, "model_cooldowns": {}},
            "disabled.json": {"disabled": True, "model_cooldowns": {}},
            "cooled.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-3.1-pro-preview": 4102444800},
            },
            "available.json": {"disabled": False, "model_cooldowns": {}},
        })

        with patch(
            "src.credential_manager.get_session_affinity_enabled",
            AsyncMock(return_value=False),
        ):
            result = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                exclude_credentials={"attempted.json"},
            )

        self.assertEqual(result[0], "available.json")

    async def test_cooldown_is_per_exact_model(self):
        manager = self.make_manager({
            "flash-cooled.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-3-flash": 4102444800},
            },
        })

        with patch(
            "src.credential_manager.get_session_affinity_enabled",
            AsyncMock(return_value=False),
        ):
            pro_result = await manager.get_valid_credential(
                mode="antigravity", model_name="gemini-3.1-pro-preview"
            )
            flash_result = await manager.get_valid_credential(
                mode="antigravity", model_name="gemini-3-flash"
            )

        self.assertEqual(pro_result[0], "flash-cooled.json")
        self.assertIsNone(flash_result)

    async def test_optional_affinity_reuses_available_credential(self):
        manager = self.make_manager({
            "a.json": {"disabled": False, "model_cooldowns": {}},
            "b.json": {"disabled": False, "model_cooldowns": {}},
        })

        with (
            patch(
                "src.credential_manager.get_session_affinity_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.credential_manager.get_session_affinity_ttl_seconds",
                AsyncMock(return_value=3600),
            ),
        ):
            first = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            second = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )

        self.assertEqual(first[0], second[0])

    async def test_affinity_uses_the_same_initial_credential_across_workers(self):
        states = {
            "a.json": {"disabled": False, "model_cooldowns": {}},
            "b.json": {"disabled": False, "model_cooldowns": {}},
            "c.json": {"disabled": False, "model_cooldowns": {}},
        }
        session_store = {}
        first_worker = self.make_manager(states, session_store)
        second_worker = self.make_manager(
            dict(reversed(list(states.items()))), session_store
        )

        with (
            patch(
                "src.credential_manager.get_session_affinity_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.credential_manager.get_session_affinity_ttl_seconds",
                AsyncMock(return_value=3600),
            ),
        ):
            first = await first_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            second = await second_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )

        self.assertEqual(first[0], second[0])

    async def test_affinity_migrates_binding_after_model_cooldown(self):
        states = {
            "a.json": {"disabled": False, "model_cooldowns": {}},
            "b.json": {"disabled": False, "model_cooldowns": {}},
            "c.json": {"disabled": False, "model_cooldowns": {}},
        }
        session_store = {}
        first_worker = self.make_manager(states, session_store)
        second_worker = self.make_manager(states, session_store)

        with (
            patch(
                "src.credential_manager.get_session_affinity_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.credential_manager.get_session_affinity_ttl_seconds",
                AsyncMock(return_value=3600),
            ),
        ):
            first = await first_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            same_binding = await second_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            states[first[0]]["model_cooldowns"] = {
                "gemini-3.1-pro-preview": 4102444800
            }
            replacement = await first_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            states[first[0]]["model_cooldowns"] = {}
            next_request = await second_worker.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )

        self.assertEqual(first[0], same_binding[0])
        self.assertNotEqual(first[0], replacement[0])
        self.assertEqual(replacement[0], next_request[0])

    async def test_affinity_falls_back_and_rebinds_when_tried(self):
        manager = self.make_manager({
            "a.json": {"disabled": False, "model_cooldowns": {}},
            "b.json": {"disabled": False, "model_cooldowns": {}},
            "c.json": {"disabled": False, "model_cooldowns": {}},
        })

        with (
            patch(
                "src.credential_manager.get_session_affinity_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.credential_manager.get_session_affinity_ttl_seconds",
                AsyncMock(return_value=3600),
            ),
        ):
            first = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )
            replacement = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
                exclude_credentials={first[0]},
            )
            next_request = await manager.get_valid_credential(
                mode="antigravity",
                model_name="gemini-3.1-pro-preview",
                session_key="chat",
            )

        self.assertNotEqual(first[0], replacement[0])
        self.assertEqual(replacement[0], next_request[0])


class SessionBindingStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_binding_is_shared_and_migration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"CREDENTIALS_DIR": temp_dir}
        ):
            first_worker = SQLiteManager()
            second_worker = SQLiteManager()
            await first_worker.initialize()
            await second_worker.initialize()
            try:
                expires_at = time.time() + 3600
                await first_worker.set_session_binding("chat", "a.json", expires_at)
                self.assertEqual(
                    await second_worker.get_session_binding("chat", time.time()),
                    "a.json",
                )

                await second_worker.set_session_binding("chat", "b.json", expires_at)
                await first_worker.delete_session_binding("chat", "a.json")
                self.assertEqual(
                    await first_worker.get_session_binding("chat", time.time()),
                    "b.json",
                )
            finally:
                await first_worker.close()
                await second_worker.close()

class CredentialRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_antigravity_wrapper_matches_cliproxy_session_and_request_ids(self):
        request = {
            "contents": [{"role": "user", "parts": [{"text": "same chat"}]}],
            "generationConfig": {"maxOutputTokens": 64000},
            "systemInstruction": {"parts": [{"text": "system"}]},
        }

        first, _ = await wrap_cli_request(request, "gemini-3-flash", "project")
        second, _ = await wrap_cli_request(request, "gemini-3-flash", "project")
        image, _ = await wrap_cli_request(
            request, "gemini-3.1-flash-image", "project"
        )

        self.assertTrue(first["requestId"].startswith("agent-"))
        self.assertNotEqual(first["requestId"], second["requestId"])
        self.assertEqual(
            first["request"]["sessionId"], second["request"]["sessionId"]
        )
        self.assertNotIn("labels", first["request"])
        self.assertNotIn("toolConfig", first["request"])
        self.assertEqual(
            first["request"]["generationConfig"]["maxOutputTokens"], 64000
        )
        self.assertNotIn("enabledCreditTypes", first)
        self.assertNotIn("role", first["request"]["systemInstruction"])
        self.assertEqual(image["requestType"], "image_gen")
        self.assertTrue(image["requestId"].startswith("image_gen/"))

        claude, _ = await wrap_cli_request(
            {
                **request,
                "generationConfig": {"maxOutputTokens": 4096},
            },
            "claude-sonnet-4-6",
            "project",
        )
        self.assertEqual(
            claude["request"]["toolConfig"]["functionCallingConfig"]["mode"],
            "VALIDATED",
        )
        self.assertEqual(
            claude["request"]["generationConfig"]["maxOutputTokens"], 64000
        )

        with_client_session, _ = await wrap_cli_request(
            {**request, "sessionId": "client-session"},
            "gemini-3-flash",
            "project",
        )
        self.assertEqual(
            with_client_session["request"]["sessionId"], "client-session"
        )

    async def test_antigravity_retry_never_reuses_an_attempted_credential(self):
        get_credential = AsyncMock(side_effect=[
            ("a.json", {"access_token": "a", "project_id": "a-project"}),
            ("b.json", {"access_token": "b", "project_id": "b-project"}),
            ("c.json", {"access_token": "c", "project_id": "c-project"}),
        ])
        sent_payloads = []
        responses = iter([
            http_response(429),
            http_response(429),
            http_response(200),
        ])

        async def post(**kwargs):
            sent_payloads.append(deepcopy(kwargs["json"]))
            return next(responses)

        with patch.multiple(
            "src.api.antigravity",
            credential_manager=SimpleNamespace(
                get_valid_credential=get_credential,
                set_cred_disabled=AsyncMock(),
            ),
            get_antigravity_stream2nostream=AsyncMock(return_value=False),
            get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
            wrap_cli_request=AsyncMock(return_value=({
                "project": "a-project",
                "requestId": "id-a",
                "request": {"sessionId": "stable-session"},
            }, "id-a")),
            _generate_request_id=Mock(side_effect=["id-b", "id-c"]),
            get_retry_config=AsyncMock(return_value={
                "max_credentials": 0,
                "retry_interval": 0,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            post_async=post,
            record_api_call_error=AsyncMock(),
            record_api_call_success=AsyncMock(),
        ):
            response = await antigravity_request(
                {"model": "gemini-3.1-pro-preview"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            get_credential.await_args_list,
            [
                call(
                    mode="antigravity",
                    model_name="gemini-3.1-pro-preview",
                    session_key=None,
                    exclude_credentials=set(),
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
        self.assertEqual(
            [payload["requestId"] for payload in sent_payloads],
            ["id-a", "id-b", "id-c"],
        )
        self.assertEqual(
            [payload["request"]["sessionId"] for payload in sent_payloads],
            ["stable-session"] * 3,
        )

    async def test_antigravity_stream_retry_renews_request_id(self):
        get_credential = AsyncMock(side_effect=[
            ("a.json", {"access_token": "a", "project_id": "a-project"}),
            ("b.json", {"access_token": "b", "project_id": "b-project"}),
        ])
        sent_payloads = []

        def stream_post(**kwargs):
            sent_payloads.append(deepcopy(kwargs["body"]))

            async def chunks():
                if len(sent_payloads) == 1:
                    yield Response(content="quota", status_code=429)
                else:
                    yield b'data: {"response": {"candidates": []}}\n\n'

            return chunks()

        with patch.multiple(
            "src.api.antigravity",
            credential_manager=SimpleNamespace(
                get_valid_credential=get_credential,
                set_cred_disabled=AsyncMock(),
            ),
            get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
            wrap_cli_request=AsyncMock(return_value=({
                "project": "a-project",
                "requestId": "id-a",
                "request": {"sessionId": "stable-session"},
            }, "id-a")),
            _generate_request_id=Mock(return_value="id-b"),
            get_retry_config=AsyncMock(return_value={
                "max_credentials": 0,
                "retry_interval": 0,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            stream_post_async=stream_post,
            record_api_call_error=AsyncMock(),
            record_api_call_success=AsyncMock(),
        ):
            chunks = [
                chunk
                async for chunk in antigravity_stream_request(
                    {"model": "gemini-3.1-pro-preview"}
                )
            ]

        self.assertTrue(chunks)
        self.assertEqual(
            [payload["requestId"] for payload in sent_payloads],
            ["id-a", "id-b"],
        )
        self.assertEqual(
            [payload["request"]["sessionId"] for payload in sent_payloads],
            ["stable-session", "stable-session"],
        )

    async def test_enabled_limit_counts_total_credentials(self):
        get_credential = AsyncMock(side_effect=[
            ("a.json", {"access_token": "a", "project_id": "a-project"}),
            ("b.json", {"access_token": "b", "project_id": "b-project"}),
        ])
        disabled = AsyncMock()

        with patch.multiple(
            "src.api.antigravity",
            credential_manager=SimpleNamespace(
                get_valid_credential=get_credential,
                set_cred_disabled=disabled,
            ),
            get_antigravity_stream2nostream=AsyncMock(return_value=False),
            get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
            wrap_cli_request=AsyncMock(return_value=({"project": "a-project"}, "id")),
            get_retry_config=AsyncMock(return_value={
                "max_credentials": 2,
                "retry_interval": 0,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            post_async=AsyncMock(side_effect=[
                http_response(403),
                http_response(429),
            ]),
            record_api_call_error=AsyncMock(),
        ):
            response = await antigravity_request(
                {"model": "gemini-3.1-pro-preview"}
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(get_credential.await_count, 2)
        disabled.assert_not_awaited()

    async def test_401_refreshes_current_credential_once_before_switching(self):
        manager = SimpleNamespace(
            get_valid_credential=AsyncMock(return_value=(
                "a.json",
                {"access_token": "old", "project_id": "a-project"},
            )),
            refresh_credential=AsyncMock(return_value={
                "access_token": "new",
                "project_id": "a-project",
            }),
            set_cred_disabled=AsyncMock(),
        )
        record_error = AsyncMock()
        post = AsyncMock(side_effect=[
            http_response(401),
            http_response(200),
        ])

        with patch.multiple(
            "src.api.antigravity",
            credential_manager=manager,
            get_antigravity_stream2nostream=AsyncMock(return_value=False),
            get_antigravity_api_url=AsyncMock(return_value="https://example.test"),
            wrap_cli_request=AsyncMock(return_value=({"project": "a-project"}, "id")),
            get_retry_config=AsyncMock(return_value={
                "max_credentials": 1,
                "retry_interval": 0,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            post_async=post,
            record_api_call_error=record_error,
            record_api_call_success=AsyncMock(),
        ):
            response = await antigravity_request(
                {"model": "gemini-3.1-pro-preview"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(manager.get_valid_credential.await_count, 1)
        manager.refresh_credential.assert_awaited_once_with(
            "a.json", mode="antigravity"
        )
        self.assertEqual(
            post.await_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer new",
        )
        record_error.assert_not_awaited()

    async def test_geminicli_uses_the_same_tried_set(self):
        get_credential = AsyncMock(side_effect=[
            ("a.json", {"access_token": "a", "project_id": "a-project"}),
            ("b.json", {"access_token": "b", "project_id": "b-project"}),
        ])

        with patch.multiple(
            "src.api.geminicli",
            credential_manager=SimpleNamespace(
                get_valid_credential=get_credential,
                set_cred_disabled=AsyncMock(),
            ),
            get_code_assist_endpoint=AsyncMock(return_value="https://example.test"),
            get_retry_config=AsyncMock(return_value={
                "max_credentials": 0,
                "retry_interval": 0,
            }),
            get_empty_output_error_enabled=AsyncMock(return_value=False),
            post_async=AsyncMock(side_effect=[
                http_response(429),
                http_response(200),
            ]),
            record_api_call_error=AsyncMock(),
            record_api_call_success=AsyncMock(),
        ):
            response = await geminicli_request(
                {"model": "gemini-3.1-pro-preview", "request": {}}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            get_credential.await_args_list[1],
            call(
                mode="geminicli",
                model_name="gemini-3.1-pro-preview",
                session_key=None,
                exclude_credentials={"a.json"},
            ),
        )


class RetryConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_400_and_empty_output_error_are_not_retryable(self):
        self.assertFalse(is_retryable_status(400))
        self.assertFalse(is_retryable_status(461))

    async def test_disabled_limit_means_no_numeric_cap(self):
        with patch.multiple(
            "src.api.utils",
            get_credential_retry_limit_enabled=AsyncMock(return_value=False),
            get_max_retry_credentials=AsyncMock(return_value=2),
            get_credential_retry_interval=AsyncMock(return_value=0.5),
        ):
            config = await get_retry_config()

        self.assertEqual(config, {
            "max_credentials": 0,
            "retry_interval": 0.5,
        })

    async def test_enabled_limit_is_total_credentials(self):
        with patch.multiple(
            "src.api.utils",
            get_credential_retry_limit_enabled=AsyncMock(return_value=True),
            get_max_retry_credentials=AsyncMock(return_value=3),
            get_credential_retry_interval=AsyncMock(return_value=1),
        ):
            config = await get_retry_config()

        self.assertEqual(config["max_credentials"], 3)


if __name__ == "__main__":
    unittest.main()
