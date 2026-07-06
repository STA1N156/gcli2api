import asyncio
import json
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from fastapi import Request

import config
from log import log


CAPTURE_TYPES = {"success", "400"}
MAX_CAPTURE_COUNT = 20
REQUEST_CAPTURE_DIRNAME = "request_capture"
REQUEST_CAPTURE_STATE_FILENAME = "state.json"
REQUEST_CAPTURE_LOCK_FILENAME = ".lock"

_ACTIVE_CACHE_TTL = 0.25
_active_cache = {"checked_at": 0.0, "active": False}


async def get_request_capture_dir() -> str:
    credentials_dir = await config.get_credentials_dir()
    return os.path.join(credentials_dir, REQUEST_CAPTURE_DIRNAME)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "active": False,
        "completed": False,
        "capture_type": "400",
        "target_count": 3,
        "captured_count": 0,
        "started_at": None,
        "completed_at": None,
        "captures": [],
    }


def _state_path(capture_dir: str) -> str:
    return os.path.join(capture_dir, REQUEST_CAPTURE_STATE_FILENAME)


def _lock_path(capture_dir: str) -> str:
    return os.path.join(capture_dir, REQUEST_CAPTURE_LOCK_FILENAME)


def _read_state_sync(capture_dir: str) -> dict[str, Any]:
    path = _state_path(capture_dir)
    if not os.path.exists(path):
        return _default_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _default_state()

    default = _default_state()
    default.update(state if isinstance(state, dict) else {})
    return default


def _write_state_sync(capture_dir: str, state: dict[str, Any]) -> None:
    os.makedirs(capture_dir, exist_ok=True)
    path = _state_path(capture_dir)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _acquire_lock(lock_path: str, timeout: float = 1.0) -> Optional[int]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


def _release_lock(lock_path: str, lock_fd: Optional[int]) -> None:
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


async def get_request_capture_status() -> dict[str, Any]:
    capture_dir = await get_request_capture_dir()
    state = await asyncio.to_thread(_read_state_sync, capture_dir)
    state["download_available"] = bool(state.get("captures"))
    return state


async def start_request_capture(capture_type: str, target_count: int) -> dict[str, Any]:
    if capture_type not in CAPTURE_TYPES:
        raise ValueError("捕捉类型只能是 success 或 400")

    target_count = max(1, min(MAX_CAPTURE_COUNT, int(target_count)))
    capture_dir = await get_request_capture_dir()

    def _start() -> dict[str, Any]:
        if os.path.exists(capture_dir):
            shutil.rmtree(capture_dir)
        os.makedirs(capture_dir, exist_ok=True)
        state = _default_state()
        state.update(
            {
                "active": True,
                "completed": False,
                "capture_type": capture_type,
                "target_count": target_count,
                "started_at": _utc_now(),
            }
        )
        _write_state_sync(capture_dir, state)
        return state

    state = await asyncio.to_thread(_start)
    _active_cache.update({"checked_at": time.monotonic(), "active": True})
    return state


async def stop_request_capture() -> dict[str, Any]:
    capture_dir = await get_request_capture_dir()

    def _stop() -> dict[str, Any]:
        state = _read_state_sync(capture_dir)
        state["active"] = False
        if not state.get("completed"):
            state["completed_at"] = _utc_now()
        _write_state_sync(capture_dir, state)
        return state

    state = await asyncio.to_thread(_stop)
    _active_cache.update({"checked_at": time.monotonic(), "active": False})
    return state


async def should_buffer_request_body(request: Request) -> bool:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return False
    if request.url.path.startswith(("/config", "/auth", "/creds", "/logs", "/version", "/front", "/docs")):
        return False
    if "json" not in request.headers.get("content-type", "").lower():
        return False

    now = time.monotonic()
    if now - _active_cache["checked_at"] < _ACTIVE_CACHE_TTL:
        return bool(_active_cache["active"])

    capture_dir = await get_request_capture_dir()

    def _is_active() -> bool:
        return bool(_read_state_sync(capture_dir).get("active"))

    active = await asyncio.to_thread(_is_active)
    _active_cache.update({"checked_at": now, "active": active})
    return active


def _status_matches(capture_type: str, status_code: int) -> bool:
    if capture_type == "400":
        return status_code == 400
    return 200 <= status_code < 400


def _parse_body(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _capture_request_sync(
    capture_dir: str,
    request_info: dict[str, Any],
    status_code: int,
    body: bytes,
) -> dict[str, Any]:
    lock_path = _lock_path(capture_dir)
    os.makedirs(capture_dir, exist_ok=True)
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        return _read_state_sync(capture_dir)

    try:
        state = _read_state_sync(capture_dir)
        if not state.get("active"):
            return state
        if not _status_matches(str(state.get("capture_type")), status_code):
            return state

        captures = state.setdefault("captures", [])
        target_count = int(state.get("target_count") or 3)
        if len(captures) >= target_count:
            state["active"] = False
            state["completed"] = True
            state["completed_at"] = state.get("completed_at") or _utc_now()
            _write_state_sync(capture_dir, state)
            return state

        index = len(captures) + 1
        filename = f"request_{index:03d}.json"
        capture = {
            "metadata": {
                **request_info,
                "status_code": status_code,
                "captured_at": _utc_now(),
            },
            "request_body": _parse_body(body),
        }
        with open(os.path.join(capture_dir, filename), "w", encoding="utf-8") as f:
            json.dump(capture, f, ensure_ascii=False, indent=2)

        captures.append(
            {
                "filename": filename,
                "status_code": status_code,
                "captured_at": capture["metadata"]["captured_at"],
            }
        )
        state["captured_count"] = len(captures)
        if len(captures) >= target_count:
            state["active"] = False
            state["completed"] = True
            state["completed_at"] = _utc_now()

        _write_state_sync(capture_dir, state)
        return state
    finally:
        _release_lock(lock_path, lock_fd)


async def capture_request_if_needed(request: Request, status_code: int, body: bytes) -> None:
    if not body:
        return

    request_info = {
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "content_type": request.headers.get("content-type"),
    }
    try:
        capture_dir = await get_request_capture_dir()
        state = await asyncio.to_thread(_capture_request_sync, capture_dir, request_info, status_code, body)
        _active_cache.update({"checked_at": time.monotonic(), "active": bool(state.get("active"))})
    except Exception as exc:
        log.warning(f"[REQUEST CAPTURE] Failed to capture request body: {exc}")


async def build_request_capture_zip() -> tuple[bytes, str]:
    capture_dir = await get_request_capture_dir()

    def _build() -> tuple[bytes, str]:
        state = _read_state_sync(capture_dir)
        captures = state.get("captures") or []
        if not captures:
            raise FileNotFoundError("暂无捕捉到的请求体")

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in captures:
                filename = item.get("filename")
                if not filename:
                    continue
                path = os.path.join(capture_dir, filename)
                if os.path.exists(path):
                    zf.write(path, arcname=filename)

        capture_type = state.get("capture_type", "request")
        return buffer.getvalue(), f"request_capture_{capture_type}_{len(captures)}.zip"

    return await asyncio.to_thread(_build)
