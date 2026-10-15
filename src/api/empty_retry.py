"""Retry an empty reply once with user text in a tool result; stream text immediately."""

import json
import uuid
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
from src.converter.anti_truncation import latest_user_text_parts
from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR


def has_current_input_tool_result(request):
    """Check only the latest user turn, without inspecting text or old history."""
    pending = set()
    for content in reversed(request.get("contents") or []):
        parts = content.get("parts") or []
        if content.get("role") == "user":
            responses = [part["functionResponse"] for part in parts if isinstance(part.get("functionResponse"), dict)]
            if not responses:
                return False
            for response in responses:
                if (response.get("name") != "read_user_input"
                        or not isinstance(response.get("id"), str) or not response["id"]):
                    continue
                data = response.get("response")
                # Anthropic tool_result strings are stored in response.output.
                if isinstance(data, dict) and isinstance(data.get("output"), str) and "source" not in data:
                    try:
                        data = json.loads(data["output"])
                    except ValueError:
                        continue
                if (isinstance(data, dict) and data.get("source") == "user_input"
                        and isinstance(data.get("text"), str)):
                    pending.add(response["id"])
        elif content.get("role") in ("model", "assistant"):
            for part in parts:
                call = part.get("functionCall")
                if (isinstance(call, dict) and call.get("name") == "read_user_input" and isinstance(call.get("id"), str)
                        and call.get("id") in pending):
                    return True
    return False


def prepare_empty_retry(payload, text_parts):
    # Match the original parts by identity: a moved system prompt and the
    # anti-truncation reminder must stay outside the synthetic tool result.
    selected = {id(part) for part in text_parts}
    request = payload["request"]
    contents = list(request.get("contents") or [])
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        parts = content.get("parts") or []
        user_parts = [part for part in parts if id(part) in selected]
        if not user_parts:
            continue
        call_id = f"call_{uuid.uuid4().hex}"
        remaining = []
        for part in parts:
            if part is user_parts[0]:
                remaining.append({"text": "读取 read_user_input 的返回内容，作为本轮用户输入继续回复。"})
            elif id(part) in selected:
                continue
            else:
                remaining.append(part)
        contents[index:index + 1] = [
            {**content, "parts": remaining},
            {"role": "model", "parts": [{
                "functionCall": {"id": call_id, "name": "read_user_input", "args": {}},
                "thoughtSignature": SKIP_THOUGHT_SIGNATURE_VALIDATOR,
            }]},
            {"role": "user", "parts": [{"functionResponse": {
                "id": call_id, "name": "read_user_input",
                "response": {"source": "user_input", "text": "".join(part["text"] for part in user_parts)},
            }}]},
        ]
        payload["request"] = {**request, "contents": contents}
        log.info("[空回重试] 最新用户原文已移至 read_user_input 工具结果，尝试 2/2")
        return


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
    text_parts = latest_user_text_parts(payload["request"])
    previous_usage = {}
    for attempt in range(2):
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
        if attempt == 1 or has_current_input_tool_result(payload["request"]):
            yield build_empty_model_output_response()
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                previous_usage[key] = previous_usage.get(key, 0) + value
        prepare_empty_retry(payload, text_parts)


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
        text_parts = latest_user_text_parts(payload["request"])
        for attempt in range(2):
            response = await func(body=payload, headers=headers)
            empty = is_empty_model_output_error(response) or (
                response.status_code == 200 and is_empty_model_output(response.body)
            )
            if not empty:
                return response
            if attempt == 0 and not has_current_input_tool_result(payload["request"]):
                prepare_empty_retry(payload, text_parts)
            else:
                break
        return build_empty_model_output_response()
    return wrapped
