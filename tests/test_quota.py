import asyncio
import unittest
from collections import OrderedDict
from unittest.mock import AsyncMock, patch

from src import antigravity_quota as quota


def quota_response():
    return {"groups": [{"buckets": [
        {"bucketId": name, "remainingFraction": value}
        for name, value in (("gemini-5h", .8), ("gemini-weekly", .1),
                            ("3p-5h", .8), ("3p-weekly", .1))
    ]}]}


class QuotaTests(unittest.TestCase):
    def test_weekly_limit_applies_to_five_hours(self):
        windows = quota.parse_quota_summary(quota_response())["windows"]
        self.assertEqual(windows["gemini"]["5h"]["remaining_percent"], 60)
        self.assertEqual(windows["claude"]["5h"]["remaining_percent"], 20)
        self.assertEqual(windows["gemini"]["7d"]["remaining_percent"], 10)

    def test_unknown_is_not_zero_and_disabled_is_zero(self):
        self.assertFalse(quota.parse_quota_summary({})["supported"])
        data = quota_response()
        data["groups"][0]["buckets"][0]["disabled"] = True
        data["groups"][0]["buckets"][2].pop("remainingFraction")
        windows = quota.parse_quota_summary(data)["windows"]
        self.assertEqual(windows["gemini"]["5h"]["remaining_percent"], 0)
        self.assertIsNone(windows["claude"]["5h"]["remaining_percent"])

    def test_failed_accounts_are_not_counted_as_empty(self):
        results = [quota.parse_quota_summary(quota_response()),
                   {"success": False, "filename": "failed.json", "error": "timeout"}]
        summary = quota.summarize_accounts(results, 2, 0)
        self.assertEqual(summary["model_summaries"][0]["remaining_percent"], 60)
        self.assertEqual(summary["model_summaries"][0]["account_count"], 1)
        self.assertEqual(summary["failed_accounts"], 1)


class QuotaRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_deduplicates_and_force_refreshes(self):
        async def fetch(*args):
            await asyncio.sleep(.001)
            return quota.parse_quota_summary(quota_response())

        storage = AsyncMock()
        storage.get_credential.return_value = {"access_token": "test", "project_id": "test"}
        with patch.object(quota, "_cache", OrderedDict()), patch.object(quota, "_inflight", {}), \
                patch.object(quota, "fetch_quota_info", side_effect=fetch) as request, \
                patch.object(quota.Credentials, "refresh", new_callable=AsyncMock) as refresh:
            results = await asyncio.gather(*(quota.get_account_quota(storage, "a.json") for _ in range(20)))
            self.assertTrue(all(result["success"] for result in results))
            self.assertEqual(request.await_count, 1)
            self.assertTrue((await quota.get_account_quota(storage, "a.json"))["cached"])
            await quota.get_account_quota(storage, "a.json", force=True)
            self.assertEqual(request.await_count, 2)
            refresh.assert_not_awaited()
            storage.store_credential.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
