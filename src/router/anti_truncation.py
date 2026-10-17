import asyncio
import json
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable, Dict

from fastapi import Response
from fastapi.responses import JSONResponse

from src.api.empty_output import build_empty_model_output_response, is_empty_model_output_error
from src.api.empty_retry import empty_output_stream, has_current_input_tool_result, wrap_user_input_in_tool
from src.api.utils import collect_streaming_response
from src.converter.anti_truncation import ReplyToolStream, apply_anti_truncation, latest_user_text_parts
from src.converter.thoughtSignature_fix import is_skip_thought_signature_placeholder


FIRST_WAIT_HINT_SECONDS = 20
WAIT_HINT_INTERVAL_SECONDS = 30
FIRST_WAIT_HINT = "[提示] 抗截断为一次性出字，请耐心等待模型全部内容生成完毕后返回，请勿关闭页面或中断连接......"
WAIT_HINT = "[提示] 当前未断连，模型仍在输出，请耐心等待"


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def _wait_hint_chunk(hint):
    prefix = "" if hint == FIRST_WAIT_HINT else "\n\n"
    return _sse({"candidates": [{"index": 0, "content": {
        "role": "model", "parts": [{"text": f"{prefix}{hint}\n\n", "thought": True}],
    }}]})


def _has_thought_text(chunk):
    if not isinstance(chunk, (str, bytes)):
        return False
    prefix = b"data:" if isinstance(chunk, bytes) else "data:"
    for line in chunk.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            data = json.loads(line[5:])
            response = data.get("response", data)
            if any(part.get("thought") and str(part.get("text") or "").strip()
                   and not is_skip_thought_signature_placeholder(part)
                   for candidate in response.get("candidates", [])
                   for part in candidate.get("content", {}).get("parts", [])):
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


async def anti_truncation_gemini_stream(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
) -> AsyncIterator[Any]:
    """Send timed connection notices as thoughts without interrupting upstream reads."""
    loop = asyncio.get_running_loop()
    next_hint = loop.time() + FIRST_WAIT_HINT_SECONDS
    hint = FIRST_WAIT_HINT
    pending = None
    async with aclosing(_reply_tool_stream(api_request, stream_request_func)) as stream:
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(stream.__anext__())
                ready, _ = await asyncio.wait({pending}, timeout=max(0, next_hint - loop.time()))
                if ready:
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    if hint == FIRST_WAIT_HINT and _has_thought_text(chunk):
                        next_hint = loop.time() + WAIT_HINT_INTERVAL_SECONDS
                        yield _wait_hint_chunk(hint)
                        hint = WAIT_HINT
                    yield chunk
                else:
                    next_hint = loop.time() + WAIT_HINT_INTERVAL_SECONDS
                    yield _wait_hint_chunk(hint)
                    hint = WAIT_HINT
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def _reply_tool_stream(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
) -> AsyncIterator[Any]:
    """Wrap current user text in a tool result and request the reply through a tool."""
    payload = apply_anti_truncation(api_request)
    passthrough = payload is api_request
    # Internal retry hint; API clients build upstream bodies without this field.
    payload = {**payload, "_anti_truncation": True}
    if not has_current_input_tool_result(payload["request"]):
        wrap_user_input_in_tool(payload, latest_user_text_parts(payload["request"]))
    # Existing/explicit client tool choices are passed through, never swallowed.
    if passthrough:
        async with aclosing(empty_output_stream(payload, stream_request_func)) as stream:
            async for chunk in stream:
                yield chunk
        return

    previous_usage = {}
    text_parts = latest_user_text_parts(payload["request"])
    for attempt in range(2):
        processor = ReplyToolStream()
        error_response = None
        done = False
        async with aclosing(stream_request_func(body=payload, native=False)) as stream:
            async for chunk in stream:
                if isinstance(chunk, Response):
                    error_response = chunk
                    break
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                for line in text.splitlines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        done = True
                        break
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                        response = data.get("response", data)
                        if response.get("error"):
                            error = response["error"]
                            code = error.get("code", 502) if isinstance(error, dict) else 502
                            error_response = JSONResponse(content=response, status_code=code)
                            break
                        converted = processor.process(data)
                        if converted is not None:
                            yield _sse(converted)
                    except (ValueError, TypeError, AttributeError):
                        # Keep valid output from other events; retry only if no
                        # usable body remains after the complete response.
                        continue
                if done or error_response is not None:
                    break
        # Close the upstream connection before handing an HTTP error to a
        # router, which may return immediately after reading this item.
        if error_response is not None and not is_empty_model_output_error(error_response):
            yield error_response
            return
        final = processor.finish()
        response = final.get("response", final)
        if (not processor.has_output and attempt == 0
                and not has_current_input_tool_result(payload["request"])):
            wrap_user_input_in_tool(payload, text_parts)
            for key, value in processor.usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    previous_usage[key] = previous_usage.get(key, 0) + value
            continue
        if processor.has_output:
            usage = response["usageMetadata"]
            for key, value in previous_usage.items():
                usage[key] = usage.get(key, 0) + value
            yield _sse(final)
            yield b"data: [DONE]\n\n"
            return
        yield error_response or build_empty_model_output_response()
        return


async def collect_anti_truncation_response(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
) -> Response:
    """Use the same reply-tool stream for non-streaming clients."""
    return await collect_streaming_response(
        _reply_tool_stream(api_request, stream_request_func)
    )
