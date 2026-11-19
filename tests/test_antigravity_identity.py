import copy
import unittest
from unittest.mock import AsyncMock, patch

from src.api.antigravity import wrap_cli_request, build_antigravity_headers
from src.antigravity_identity import order_request
from src.converter.antigravity_fix import normalize_antigravity_request

class AntigravityIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def normalized(self, payload):
        with patch("config.get_return_thoughts_to_frontend", AsyncMock(return_value=True)):
            out = await normalize_antigravity_request(copy.deepcopy(payload))
        model = out.pop("model")
        result, _ = await wrap_cli_request(out, model, "project")
        return order_request(result)

    async def test_hub_chat_keeps_tools_and_thinking(self):
        body = await self.normalized({"model":"gemini-3.8-flash-medium","contents":[{"role":"user","parts":[{"text":"Hi"}]}],
            "tools":[{"functionDeclarations":[{"name":"a","parameters":{"type":"object"}},{"name":"b","parameters":{"type":"object"}}]}]})
        self.assertEqual(body["requestType"],"chat")
        request=body["request"]
        self.assertEqual(len(request["tools"]),2)
        self.assertEqual(request["tools"][0]["functionDeclarations"][0]["parameters"]["type"],"OBJECT")
        self.assertEqual(request["generationConfig"]["thinkingConfig"],{"thinkingLevel":"MEDIUM","includeThoughts":True})
        self.assertEqual(len(request["labels"]),7)
        self.assertNotIn("toolConfig",request)
        self.assertEqual(len(request["safetySettings"]),10)
        self.assertEqual(list(body),["project","requestId","request","model","userAgent","requestType"])

    async def test_claude_forced_tools_and_stable_ids(self):
        payload={"model":"claude-sonnet-4-6","contents":[{"role":"model","parts":[{"functionCall":{"name":"a","args":{"x":1}}}]},{"role":"user","parts":[{"functionResponse":{"name":"a","response":{"ok":True}}}]}],
            "tools":[{"functionDeclarations":[{"name":"a","parameters":{"type":"object"}}]}],"toolConfig":{"functionCallingConfig":{"mode":"ANY"}},"thinkingConfig":{"thinking_budget":512}}
        first=await self.normalized(payload);again=await self.normalized(payload)
        request=first["request"]
        self.assertEqual(first["requestType"],"chat")
        self.assertNotIn("thinkingConfig",request["generationConfig"])
        call=request["contents"][0]["parts"][0]["functionCall"]["id"]
        self.assertEqual(call,request["contents"][1]["parts"][0]["functionResponse"]["id"])
        self.assertEqual(call,again["request"]["contents"][0]["parts"][0]["functionCall"]["id"])

    async def test_continuation_and_image_modes(self):
        body=await self.normalized({"model":"gemini-3.8-flash-medium","contents":[{"role":"model","parts":[{"text":"unfinished"}]}]})
        self.assertEqual(body["request"]["contents"][-2]["parts"][0]["text"],"unfinished")
        self.assertEqual(body["request"]["contents"][-1]["parts"][0]["text"],"继续")
        image=await self.normalized({"model":"gemini-3.1-flash-image","contents":[{"role":"user","parts":[{"text":"draw"}]}]})
        self.assertEqual(image["requestType"],"image_gen")
        self.assertEqual(image["request"]["generationConfig"]["candidateCount"],1)

    async def test_thinking_suffix_and_explicit_level(self):
        for suffix in ("low", "medium"):
            with self.subTest(suffix=suffix):
                body = await self.normalized({"model": f"gemini-3.8-flash-{suffix}", "contents": [{"role": "user", "parts": [{"text": "Hi"}]}]})
                self.assertEqual(body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], suffix.upper())
        body = await self.normalized({"model": "gemini-3.8-flash-low", "thinkingConfig": {"thinkingLevel": "MEDIUM"}, "contents": [{"role": "user", "parts": [{"text": "Hi"}]}]})
        self.assertEqual(body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MEDIUM")
        body = await self.normalized({"model": "gemini-3.5-flash-lite-low", "contents": [{"role": "user", "parts": [{"text": "Hi"}]}]})
        self.assertEqual(body["model"], "gemini-3.5-flash-lite")
        self.assertEqual(body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "LOW")

    def test_hub_header(self):
        self.assertEqual(build_antigravity_headers("token")["User-Agent"],"antigravity/hub/2.28.0 (aidev_client; os_type=windows; arch=amd64; cl=1008300195)")
