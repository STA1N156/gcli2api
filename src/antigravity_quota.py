"""Antigravity rolling-window quotas, shared by individual and bulk queries."""

import asyncio
import math
import os
import time
from collections import OrderedDict

import httpx

from config import get_antigravity_api_url
from src.google_oauth_api import Credentials
from src.httpx_client import http_client
from src.utils import ANTIGRAVITY_USER_AGENT


QUOTA_CONCURRENCY = max(1, min(128, int(os.getenv("ANTIGRAVITY_QUOTA_CONCURRENCY", "128"))))
QUOTA_CACHE_SECONDS = 30
_semaphore = asyncio.Semaphore(QUOTA_CONCURRENCY)
_cache = OrderedDict()
_inflight = {}
_limits = httpx.Limits(
    max_connections=QUOTA_CONCURRENCY,
    max_keepalive_connections=QUOTA_CONCURRENCY,
    keepalive_expiry=30,
)
_timeout = httpx.Timeout(15, connect=5, pool=5)


def _fraction(value):
    try:
        number = float(value)
        return max(0.0, min(1.0, number)) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def parse_quota_summary(data):
    buckets = {
        bucket.get("bucketId"): bucket
        for group in data.get("groups", []) if isinstance(group, dict)
        for bucket in group.get("buckets", []) if isinstance(bucket, dict)
    }
    windows = {}
    for family, prefix, ratio in (("gemini", "gemini", 6), ("claude", "3p", 2)):
        periods = {}
        for period, suffix in (("5h", "5h"), ("7d", "weekly")):
            bucket = buckets.get(f"{prefix}-{suffix}")
            if bucket is None:
                continue
            raw = _fraction(bucket.get("remainingFraction"))
            periods[period] = {
                "remaining_fraction": 0.0 if bucket.get("disabled") else raw,
                "raw_remaining_fraction": raw,
                "reset_time": bucket.get("resetTime"),
                "disabled": bool(bucket.get("disabled")),
            }
        if not periods:
            continue
        five, week = periods.get("5h", {}), periods.get("7d", {})
        raw_five, weekly = five.get("remaining_fraction"), week.get("remaining_fraction")
        if raw_five is not None and weekly is not None:
            # The weekly budget also caps what can be spent in the current 5h window.
            five["remaining_fraction"] = min(raw_five, weekly * ratio)
            five["weekly_limited"] = weekly * ratio < raw_five
        for window in periods.values():
            value = window["remaining_fraction"]
            window["remaining_percent"] = round(value * 100, 2) if value is not None else None
        windows[family] = periods
    return {"success": True, "windows": windows, "supported": bool(windows)}


async def fetch_quota_info(access_token, project_id=""):
    api_url = await get_antigravity_api_url()
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": ANTIGRAVITY_USER_AGENT}
    async with http_client.get_client(timeout=_timeout, limits=_limits) as client:
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{api_url.rstrip('/')}/v1internal:retrieveUserQuotaSummary",
                    headers=headers, json={"project": project_id or ""},
                )
                if response.status_code == 200:
                    return parse_quota_summary(response.json())
                if attempt == 0 and response.status_code in (408, 429, 500, 502, 503, 504):
                    await asyncio.sleep(0.35)
                    continue
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": f"额度查询失败（HTTP {response.status_code}）",
                }
            except httpx.TransportError:
                if attempt:
                    return {"success": False, "error": "额度查询连接超时或中断，请重试"}
                await asyncio.sleep(0.35)


async def _query_account(storage, filename):
    data = await storage.get_credential(filename, mode="antigravity")
    if not data:
        return {"success": False, "status_code": 404, "error": "凭证不存在"}
    creds = Credentials.from_dict(data)
    # Missing expiry alone does not invalidate a working access token.
    refreshed = False
    if not creds.access_token or (creds.expires_at and creds.is_expired()):
        await creds.refresh()
        refreshed = True
    result = await fetch_quota_info(creds.access_token, creds.project_id)
    if result.get("status_code") == 401 and not refreshed:
        await creds.refresh()
        refreshed = True
        result = await fetch_quota_info(creds.access_token, creds.project_id)
    if refreshed:
        updated = {**data, **creds.to_dict()}
        if "token" in updated:
            updated["token"] = creds.access_token
        await storage.store_credential(filename, updated, mode="antigravity")
    return result


async def _load_account(storage, filename):
    try:
        async with _semaphore:
            result = await asyncio.wait_for(_query_account(storage, filename), timeout=45)
    except (TimeoutError, httpx.HTTPError):
        result = {"success": False, "error": "额度查询超时，请稍后刷新"}
    except Exception:
        result = {"success": False, "error": "凭证认证或额度查询失败，请检查凭证"}
    result = {**result, "filename": filename, "updated_at": time.time()}
    _cache[filename] = result
    _cache.move_to_end(filename)
    while len(_cache) > 2048:
        _cache.popitem(last=False)
    return result


async def get_account_quota(storage, filename, *, force=False):
    cached = _cache.get(filename)
    ttl = QUOTA_CACHE_SECONDS if cached and cached.get("success") else 5
    if not force and cached and time.time() - cached["updated_at"] < ttl:
        return {**cached, "cached": True}
    task = _inflight.get(filename)
    if task is None:
        task = asyncio.create_task(_load_account(storage, filename))
        _inflight[filename] = task
        task.add_done_callback(lambda done: _inflight.pop(filename, None))
    return {**await asyncio.shield(task), "cached": False}


def summarize_accounts(results, enabled_count, started_at):
    summaries = []
    for family in ("gemini", "claude"):
        for period in ("5h", "7d"):
            values = [item["windows"][family][period]["remaining_fraction"] for item in results
                      if item.get("success") and item.get("windows", {}).get(family, {}).get(period, {}).get("remaining_fraction") is not None]
            average = sum(values) / len(values) if values else None
            summaries.append({
                "family": family, "period": period, "account_count": len(values),
                "exhausted_accounts": sum(value <= 0.0002 for value in values),
                "remaining_percent": round(average * 100, 2) if average is not None else None,
            })
    errors = [{"filename": item["filename"], "error": item.get("error")} for item in results if not item.get("success")]
    return {
        "success": True, "enabled_accounts": enabled_count, "checked_accounts": len(results),
        "failed_accounts": len(errors), "unsupported_accounts": sum(item.get("success") and not item.get("supported") for item in results),
        "quota_accounts": sum(item.get("success") and item.get("supported", False) for item in results),
        "model_summaries": summaries, "updated_at": time.time(), "errors": errors[:10],
        "duration_seconds": round(time.time() - started_at, 2), "concurrency": QUOTA_CONCURRENCY,
    }
