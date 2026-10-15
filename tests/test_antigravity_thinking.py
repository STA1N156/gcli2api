import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from src.api.antigravity import wrap_cli_request
from src.converter.antigravity_fix import _normalize_antigravity_request, normalize_antigravity_request
from src.converter.gemini_fix import normalize_gemini_request
from src.converter.openai2gemini import convert_openai_to_gemini_request


class AntigravityThinkingTests(unittest.TestCase):
    def normalize(self, model, thinking=None, contents=None):
        config = {"thinkingConfig": copy.deepcopy(thinking)} if thinking is not None else {}
        _normalize_antigravity_request({"contents": contents or []}, model, config, True)
        return config.get("thinkingConfig", {})

    def test_level_suffixes(self):
        for model in ("gemini-3.8-flash", "gemini-4-argon", "claude-sonnet-5-5", "claude-opus-5-5-thinking"):
            for level in ("minimal", "low", "medium", "high"):
                with self.subTest(model=model, level=level):
                    result = self.normalize(f"{model}-{level}")
                    expected = None if model == "gemini-4-argon" and level == "high" else level.upper()
                    self.assertEqual(result.get("thinkingLevel"), expected)
                    self.assertNotIn("thinkingBudget", result)
                    self.assertTrue(result["includeThoughts"])

    def test_explicit_level_is_preserved_without_suffix(self):
        for model in ("gemini-3.8-flash", "gemini-4-argon", "claude-opus-4-6"):
            with self.subTest(model=model):
                result = self.normalize(model, {"thinkingLevel": "LOW", "thinkingBudget": 4096})
                self.assertEqual(result["thinkingLevel"], "LOW")
                self.assertNotIn("thinkingBudget", result)

    def test_claude_46_thinking_uses_8192_unless_client_sets_budget(self):
        for model in ("claude-opus-4-6-thinking", "claude-sonnet-4-6-thinking"):
            with self.subTest(model=model):
                expected = {"thinkingBudget": 8192, "includeThoughts": True}
                self.assertEqual(self.normalize(model), expected)
                self.assertEqual(self.normalize(model, {"thinkingLevel": "HIGH"}), expected)
                self.assertEqual(self.normalize(model, {"thinkingBudget": 4096}), {
                    "thinkingBudget": 4096, "includeThoughts": True,
                })

    def test_flash_lite_aliases_use_base_model(self):
        for base_model in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite"):
            for level in ("low", "medium", "high"):
                with self.subTest(model=base_model, level=level):
                    config = {}
                    model = _normalize_antigravity_request(
                        {}, f"{base_model}-{level}", config, True
                    )
                    self.assertEqual(model, base_model)
                    self.assertEqual(config["thinkingConfig"]["thinkingLevel"], level.upper())
                    self.assertNotIn("thinkingBudget", config["thinkingConfig"])

    def test_client_level_overrides_suffix_and_other_suffixes_are_not_levels(self):
        self.assertEqual(self.normalize("gemini-3.8-flash-high", {"thinkingLevel": "LOW"})["thinkingLevel"], "LOW")
        self.assertEqual(self.normalize("claude-opus-5-5-thinking", {"thinkingLevel": "LOW"})["thinkingLevel"], "LOW")
        self.assertNotIn("thinkingLevel", self.normalize("gemini-3.8-flash-tiered"))
        self.assertNotIn("thinkingLevel", self.normalize("gemini-3.1-flash-image"))
        self.assertEqual(self.normalize("gemini-2.5-flash-thinking")["thinkingBudget"], 1024)

    def test_openai_effort_overrides_suffix(self):
        for model in ("gemini-3.8-flash-high", "gemini-4-argon-high", "claude-sonnet-5-5-high"):
            with self.subTest(model=model):
                request = {"model": model, "reasoning_effort": "low",
                           "messages": [{"role": "user", "content": "Hello"}]}
                with patch("src.converter.openai2gemini.merge_system_messages", new=AsyncMock(return_value=request)):
                    result = asyncio.run(convert_openai_to_gemini_request(request))
                thinking = self.normalize(model, result["generationConfig"]["thinkingConfig"])
                self.assertEqual(thinking["thinkingLevel"], "LOW")

    def test_gcli_client_level_overrides_suffix(self):
        request = {"model": "gemini-3.1-pro-preview-high",
                   "generationConfig": {"thinkingConfig": {"thinkingLevel": "LOW"}}}
        with patch("config.get_return_thoughts_to_frontend", new=AsyncMock(return_value=True)):
            result = asyncio.run(normalize_gemini_request(request))
        self.assertEqual(result["generationConfig"]["thinkingConfig"]["thinkingLevel"], "LOW")

    def test_claude_tool_history_preserves_level(self):
        contents = [{"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {}}}]}]
        result = self.normalize("claude-sonnet-5-5-high", contents=contents)
        self.assertEqual(result["thinkingLevel"], "HIGH")
        self.assertNotIn("thinkingBudget", result)
        self.assertEqual(self.normalize("claude-sonnet-4-6-thinking", contents=contents)["thinkingBudget"], 8192)

    def test_max_and_xhigh_are_high(self):
        for effort in ("max", "xhigh"):
            for base in ("gemini-3.8-flash", "gemini-4-argon", "claude-opus-5-5"):
                with self.subTest(model=base, effort=effort):
                    config = {}
                    result = _normalize_antigravity_request({}, f"{base}-{effort}", config, True)
                    self.assertEqual(result, f"{base}-high")
                    self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "HIGH")
                    self.assertEqual(self.normalize(base, {"thinkingLevel": effort.upper()})["thinkingLevel"], "HIGH")
                    self.assertEqual(self.normalize(f"{base}-{effort}", {"thinkingLevel": "LOW"})["thinkingLevel"], "LOW")

            request = {"model": "claude-sonnet-5-5-low", "reasoning_effort": effort.upper(),
                       "messages": [{"role": "user", "content": "Hello"}]}
            with patch("src.converter.openai2gemini.merge_system_messages", new=AsyncMock(return_value=request)):
                result = asyncio.run(convert_openai_to_gemini_request(request))
            self.assertEqual(result["generationConfig"]["thinkingConfig"]["thinkingLevel"], "HIGH")

            requests = [
                {"model": f"gemini-3.1-pro-preview-{effort}"},
                {"model": "gemini-3.1-pro-preview-low", "generationConfig": {"thinkingConfig": {"thinkingLevel": effort}}},
            ]
            for request in requests:
                with patch("config.get_return_thoughts_to_frontend", new=AsyncMock(return_value=True)):
                    result = asyncio.run(normalize_gemini_request(request))
                self.assertEqual(result["model"], "gemini-3.1-pro-preview")
                self.assertEqual(result["generationConfig"]["thinkingConfig"]["thinkingLevel"].upper(), "HIGH")

    def test_output_limits_survive_request_wrapping(self):
        async def check():
            cases = (
                ("claude-opus-4-6-thinking", 128000),
                ("claude-sonnet-4-6-thinking", 128000),
                ("claude-opus-5-5-high", 128000),
                ("claude-sonnet-5-5-low", 128000),
                ("gemini-3.8-flash-high", 64000),
                ("gemini-4-argon-high", 1_000_000),
                ("gemini-3.1-flash-image", None),
            )
            for model, limit in cases:
                with self.subTest(model=model):
                    request = {"model": model, "generationConfig": {"maxOutputTokens": 16},
                               "contents": [{"role": "user", "parts": [{"text": "Hello"}]}]}
                    result = await normalize_antigravity_request(request)
                    self.assertEqual(result["generationConfig"].get("maxOutputTokens"), limit)
                    payload, _ = await wrap_cli_request(result, result["model"], "test-project")
                    self.assertEqual(payload["request"]["generationConfig"].get("maxOutputTokens"), limit)

        with patch("config.get_return_thoughts_to_frontend", new=AsyncMock(return_value=True)):
            asyncio.run(check())

    def test_argon_high_uses_default_and_max_uses_high_unless_client_sets_level(self):
        async def check():
            for suffix in ("high", "max", "xhigh"):
                for effort in (None, "high", "max", "xhigh", "medium", "low"):
                    with self.subTest(suffix=suffix, effort=effort):
                        request = {"model": f"gemini-4-argon-{suffix}",
                                   "messages": [{"role": "user", "content": "Hello"}]}
                        if effort:
                            request["reasoning_effort"] = effort
                        with patch("src.converter.openai2gemini.merge_system_messages", new=AsyncMock(return_value=request)):
                            result = await convert_openai_to_gemini_request(request)
                        result["model"] = request["model"]
                        result = await normalize_antigravity_request(result)
                        payload, _ = await wrap_cli_request(result, result["model"], "test-project")
                        self.assertEqual(payload["model"], "gemini-4-argon-high")
                        thinking = payload["request"]["generationConfig"]["thinkingConfig"]
                        expected = (effort or ("high" if suffix in ("max", "xhigh") else "")).upper()
                        expected = "HIGH" if expected in ("MAX", "XHIGH") else expected or None
                        self.assertEqual(thinking.get("thinkingLevel"), expected)
                        self.assertNotIn("thinkingBudget", thinking)
                        self.assertTrue(thinking["includeThoughts"])

        with patch("config.get_return_thoughts_to_frontend", new=AsyncMock(return_value=True)):
            asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
