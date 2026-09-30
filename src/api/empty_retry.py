"""Retry empty replies twice without buffering ordinary streaming text."""

import json
from contextlib import aclosing
from functools import wraps

from fastapi import Response
from fastapi.responses import JSONResponse

from log import log
from src.api.empty_output import (
    build_empty_model_output_response,
    has_visible_model_output_payload,
    is_empty_model_output,
    is_empty_model_output_error,
)
from src.converter.anti_truncation import move_continue_hint


def prepare_empty_retry(payload, hint, attempt):
    payload["request"] = move_continue_hint(payload["request"], hint, to_start=attempt > 0)
    log.info(f"[空回重试] 将（继续）移到最新用户输入{'开头' if attempt else '末尾'}，尝试 {attempt + 2}/3")


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def _add_usage(data, previous_usage):
    response = data.get("response", data)
    if response.get("usageMetadata"):
        usage = response["usageMetadata"]
        for key, value in previous_usage.items():
            usage[key] = usage.get(key, 0) + value


async def empty_output_stream(body, stream_request_func, headers=None):
    payload = {**body, "_empty_retry_active": True}
    hint = {"text": "（继续）"}
    previous_usage = {}
    for attempt in range(3):
        has_output = False
        error = None
        usage = {}
        pending = []
        # The HTTP helper supplies complete SSE lines in non-native mode.
        async with aclosing(stream_request_func(body=payload, native=False, headers=headers)) as stream:
            async for chunk in stream:
                if isinstance(chunk, Response):
                    error = chunk
                    break
                if has_output and not previous_usage:
                    yield chunk
                    continue
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                for line in text.splitlines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        if has_output:
                            yield b"data: [DONE]\n\n"
                        continue
                    try:
                        data = json.loads(raw)
                        response = data.get("response", data)
                        if response.get("error"):
                            details = response["error"]
                            code = details.get("code", 502) if isinstance(details, dict) else 502
                            error = JSONResponse(content=response, status_code=code)
                            break
                        usage.update(response.get("usageMetadata") or {})
                        has_output |= has_visible_model_output_payload(data)
                    except (ValueError, TypeError, AttributeError):
                        # Do not retry an unknown event after forwarding it.
                        has_output = True
                        for item in pending:
                            yield _sse(item)
                        pending.clear()
                        yield f"{line}\n\n".encode()
                        continue
                    if has_output:
                        for item in pending:
                            _add_usage(item, previous_usage)
                            yield _sse(item)
                        pending.clear()
                        _add_usage(data, previous_usage)
                        yield _sse(data)
                    else:
                        # Thoughts flow immediately. Hold only whitespace and
                        # completion metadata so an empty attempt cannot end the client stream.
                        thoughts = []
                        for index, candidate in enumerate(response.get("candidates") or []):
                            content = candidate.get("content") or {}
                            parts = [part for part in content.get("parts") or [] if isinstance(part, dict)]
                            thought_parts = [part for part in parts if part.get("thought")]
                            if thought_parts:
                                thoughts.append({
                                    "index": candidate.get("index", index),
                                    "content": {**content, "parts": thought_parts},
                                })
                                candidate["content"] = {
                                    **content, "parts": [part for part in parts if not part.get("thought")],
                                }
                        if thoughts:
                            thought_data = {"candidates": thoughts}
                            yield _sse({"response": thought_data} if "response" in data else thought_data)
                        pending.append(data)
                if error is not None:
                    break
        if error is not None and (has_output or not is_empty_model_output_error(error)):
            yield error
            return
        if has_output:
            return
        if attempt == 2:
            yield build_empty_model_output_response()
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                previous_usage[key] = previous_usage.get(key, 0) + value
        prepare_empty_retry(payload, hint, attempt)


def retry_empty_stream(func):
    @wraps(func)
    async def wrapped(body, native=False, headers=None):
        if body.get("_anti_truncation") or body.get("_empty_retry_active"):
            stream = func(body=body, native=native, headers=headers)
        else:
            stream = empty_output_stream(body, func, headers=headers)
        async with aclosing(stream):
            async for chunk in stream:
                yield chunk
    return wrapped


def retry_empty_response(func):
    @wraps(func)
    async def wrapped(body, headers=None):
        if body.get("_empty_retry_active"):
            return await func(body=body, headers=headers)
        payload = {**body, "_empty_retry_active": True}
        hint = {"text": "（继续）"}
        for attempt in range(3):
            response = await func(body=payload, headers=headers)
            empty = is_empty_model_output_error(response) or (
                response.status_code == 200 and is_empty_model_output(response.body)
            )
            if not empty:
                return response
            if attempt < 2:
                prepare_empty_retry(payload, hint, attempt)
        return build_empty_model_output_response()
    return wrapped
