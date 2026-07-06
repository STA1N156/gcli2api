import asyncio
import ctypes
import ctypes.util
import gc
import os
import time
from typing import Optional

from log import log


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MEMORY_TRIM_ENABLED = _env_bool("MEMORY_TRIM_ENABLED", True)
MEMORY_TRIM_INTERVAL_SECONDS = float(os.getenv("MEMORY_TRIM_INTERVAL_SECONDS", "30"))
MEMORY_TRIM_MIN_REQUESTS = int(os.getenv("MEMORY_TRIM_MIN_REQUESTS", "500"))
MEMORY_TRIM_LARGE_BODY_BYTES = int(float(os.getenv("MEMORY_TRIM_LARGE_BODY_MB", "2")) * 1024 * 1024)
MEMORY_TRIM_RSS_MB = float(os.getenv("MEMORY_TRIM_RSS_MB", "1024"))

_trim_lock = asyncio.Lock()
_last_trim_at = 0.0
_requests_since_trim = 0


def _load_malloc_trim():
    if os.name != "posix":
        return None

    libc_name = ctypes.util.find_library("c") or "libc.so.6"
    try:
        libc = ctypes.CDLL(libc_name)
        malloc_trim = libc.malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return malloc_trim
    except Exception:
        return None


_malloc_trim = _load_malloc_trim()


def _current_rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as f:
            rss_pages = int(f.read().split()[1])
        return rss_pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except Exception:
        return None


def _trim_sync() -> bool:
    gc.collect()
    if _malloc_trim is None:
        return False
    return bool(_malloc_trim(0))


async def maybe_trim_memory(content_length: Optional[int] = None) -> None:
    global _last_trim_at, _requests_since_trim

    if not MEMORY_TRIM_ENABLED:
        return

    _requests_since_trim += 1
    large_body = bool(content_length and content_length >= MEMORY_TRIM_LARGE_BODY_BYTES)
    if not large_body and _requests_since_trim < MEMORY_TRIM_MIN_REQUESTS:
        return

    now = time.monotonic()
    if now - _last_trim_at < MEMORY_TRIM_INTERVAL_SECONDS:
        return

    rss_mb = _current_rss_mb()
    if rss_mb is not None and rss_mb < MEMORY_TRIM_RSS_MB:
        _requests_since_trim = 0
        return

    async with _trim_lock:
        now = time.monotonic()
        if now - _last_trim_at < MEMORY_TRIM_INTERVAL_SECONDS:
            return

        _last_trim_at = now
        _requests_since_trim = 0
        trimmed = await asyncio.to_thread(_trim_sync)
        if log.is_debug_enabled():
            after_rss = _current_rss_mb()
            before_text = round(rss_mb, 1) if rss_mb is not None else "unknown"
            after_text = round(after_rss, 1) if after_rss is not None else "unknown"
            log.debug(f"[MEMORY_TRIM] trim={trimmed} rss_before={before_text}MB rss_after={after_text}MB")
