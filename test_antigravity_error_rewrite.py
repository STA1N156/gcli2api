import json
import unittest

from fastapi import Response

from src.api.antigravity import _rewrite_resource_exhausted_response


class AntigravityErrorRewriteTests(unittest.TestCase):
    def test_generic_resource_exhausted_without_reset_gets_clear_message(self):
        response = Response(
            content=json.dumps({
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                }
            }),
            status_code=429,
        )

        rewritten = _rewrite_resource_exhausted_response(response)
        payload = json.loads(rewritten.body)

        self.assertEqual(rewritten.status_code, 429)
        self.assertEqual(payload["error"]["code"], 429)
        self.assertEqual(
            payload["error"]["message"],
            "系统提示词中含有被标记内容，请清理后重试。",
        )

    def test_response_with_reset_time_is_unchanged(self):
        response = Response(
            content=json.dumps({
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                    "details": [{
                        "metadata": {"quotaResetTimeStamp": "2026-09-04T00:00:00Z"}
                    }],
                }
            }),
            status_code=429,
        )

        rewritten = _rewrite_resource_exhausted_response(response)
        payload = json.loads(rewritten.body)

        self.assertEqual(
            payload["error"]["message"],
            "Resource has been exhausted (e.g. check quota).",
        )


if __name__ == "__main__":
    unittest.main()
