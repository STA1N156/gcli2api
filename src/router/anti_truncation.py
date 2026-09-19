import json
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable, Dict

from fastapi import Response
from fastapi.responses import JSONResponse

from src.api.empty_output import build_empty_model_output_response, EMPTY_MODEL_OUTPUT_STATUS_CODE
from src.api.utils import collect_streaming_response
from src.converter.anti_truncation import ReplyToolStream, apply_anti_truncation


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


async def anti_truncation_gemini_stream(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
    max_attempts: int,
) -> AsyncIterator[Any]:
    """Force a reply tool; retry only a completely empty response, within the limit."""
    payload = apply_anti_truncation(api_request)
    # Existing/explicit client tool choices are passed through, never swallowed.
    if payload is api_request:
        error_response = None
        async with aclosing(stream_request_func(body=payload, native=False)) as stream:
            async for chunk in stream:
                if isinstance(chunk, Response):
                    error_response = chunk
                    break
                yield chunk
        if error_response is not None:
            yield error_response
        return

    previous_usage = {}
    attempt_limit = max(1, min(max_attempts, 10))
    for attempt in range(attempt_limit):
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
                        # An unreadable event must not discard ordinary text from
                        # earlier or later events, or restart a partial response.
                        processor.has_activity = True
                if done or error_response is not None:
                    break
        # Close the upstream connection before handing an HTTP error to a
        # router, which may return immediately after reading this item.
        if error_response is not None and (
            error_response.status_code != EMPTY_MODEL_OUTPUT_STATUS_CODE or processor.has_activity
        ):
            yield error_response
            return
        final = processor.finish()
        if processor.has_output:
            response = final.get("response", final)
            usage = response["usageMetadata"]
            for key, value in previous_usage.items():
                usage[key] = usage.get(key, 0) + value
            yield _sse(final)
            yield b"data: [DONE]\n\n"
            return
        # Thinking, tool calls or any body text rule out an empty-reply retry.
        if processor.has_activity or attempt + 1 >= attempt_limit:
            yield error_response or build_empty_model_output_response()
            return
        for key, value in processor.usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                previous_usage[key] = previous_usage.get(key, 0) + value


async def collect_anti_truncation_response(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
    max_attempts: int,
) -> Response:
    """Use the same reply-tool stream for non-streaming clients."""
    return await collect_streaming_response(
        anti_truncation_gemini_stream(api_request, stream_request_func, max_attempts)
    )
