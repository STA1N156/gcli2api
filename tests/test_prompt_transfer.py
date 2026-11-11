import ast
import __future__
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi import Response
from src.converter.anti_truncation import move_system_to_first_user


class PromptTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "src/api/antigravity.py"
        names = {"_RESOURCE_EXHAUSTED_MESSAGE", "_ANTIGRAVITY_CONTENT_POLICY_MESSAGE"}
        methods = {"_rewrite_resource_exhausted_response", "_move_system_prompt_for_retry"}
        nodes = [n for n in ast.parse(path.read_text(encoding="utf-8")).body if
                 isinstance(n, ast.FunctionDef) and n.name in methods or
                 isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
        cls.scope = {"json": json, "Response": Response, "move_system_to_first_user": move_system_to_first_user,
                     "log": SimpleNamespace(info=lambda *args: None)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag), cls.scope)

    def response(self, *, reset=False):
        error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": self.scope["_RESOURCE_EXHAUSTED_MESSAGE"]}
        if reset:
            error["details"] = [{"metadata": {"quotaResetTimeStamp": "2099-01-01T00:00:00Z"}}]
        return Response(json.dumps({"error": error}), 429)

    def test_matching_429_moves_prompt_for_this_retry(self):
        original = self.response()
        rewritten = self.scope["_rewrite_resource_exhausted_response"](original)
        self.assertIsNot(rewritten, original)
        request = {"systemInstruction": {"parts": [{"text": "system"}]},
                   "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        untouched = copy.deepcopy(request)
        body, payload = {"request": request}, {"request": request}
        self.assertTrue(self.scope["_move_system_prompt_for_retry"](body, payload))
        self.assertNotIn("systemInstruction", body["request"])
        self.assertEqual(payload["request"]["contents"][0]["parts"][0], {"text": "system"})
        self.assertEqual(request, untouched)
        self.assertFalse(self.scope["_move_system_prompt_for_retry"](body, payload))

    def test_quota_reset_429_keeps_normal_quota_handling(self):
        original = self.response(reset=True)
        self.assertIs(self.scope["_rewrite_resource_exhausted_response"](original), original)


if __name__ == "__main__":
    unittest.main()
