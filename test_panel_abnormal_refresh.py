import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.panel.creds import clear_all_credential_abnormal_status


class AbnormalStatusRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_resets_all_abnormal_credentials_for_both_modes(self):
        for mode in ("geminicli", "antigravity"):
            with self.subTest(mode=mode):
                backend = SimpleNamespace(
                    clear_all_model_cooldowns=AsyncMock(return_value=True)
                )
                storage = SimpleNamespace(
                    _backend=backend,
                    get_all_credential_states=AsyncMock(return_value={
                        "disabled.json": {
                            "disabled": True,
                            "error_codes": [],
                            "model_cooldowns": {},
                        },
                        "error.json": {
                            "disabled": False,
                            "error_codes": [429],
                            "model_cooldowns": {},
                        },
                        "cooled.json": {
                            "disabled": False,
                            "error_codes": [],
                            "model_cooldowns": {"model": time.time() + 3600},
                        },
                        "normal.json": {
                            "disabled": False,
                            "error_codes": [],
                            "model_cooldowns": {},
                        },
                    }),
                    update_credential_state=AsyncMock(return_value=True),
                )

                with patch(
                    "src.panel.creds.get_storage_adapter",
                    AsyncMock(return_value=storage),
                ):
                    response = await clear_all_credential_abnormal_status(
                        token="token", mode=mode
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(storage.update_credential_state.await_count, 3)
                self.assertEqual(
                    backend.clear_all_model_cooldowns.await_count, 3
                )
                updated_files = {
                    call.args[0]
                    for call in storage.update_credential_state.await_args_list
                }
                self.assertEqual(
                    updated_files,
                    {"disabled.json", "error.json", "cooled.json"},
                )
                for call in storage.update_credential_state.await_args_list:
                    self.assertEqual(call.kwargs["mode"], mode)
                    self.assertEqual(call.args[1], {
                        "disabled": False,
                        "error_codes": [],
                        "error_messages": {},
                    })


if __name__ == "__main__":
    unittest.main()
