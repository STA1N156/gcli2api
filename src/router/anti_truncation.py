import json
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable, Dict

from fastapi import Response
from fastapi.responses import JSONResponse

from src.api.empty_output import build_empty_model_output_response, is_empty_model_output_error
from src.api.empty_retry import empty_output_stream, prepare_empty_retry
from src.api.utils import collect_streaming_response
from src.converter.anti_truncation import ReplyToolStream, apply_anti_truncation


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


async def anti_truncation_gemini_stream(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
) -> AsyncIterator[Any]:
    """Force a reply tool; retry empty bodies once with a trailing hint, then a leading hint."""
    payload = apply_anti_truncation(api_request)
    passthrough = payload is api_request
    # Internal retry hint; API clients build upstream bodies without this field.
    payload = {**payload, "_anti_truncation": True}
    # Existing/explicit client tool choices are passed through, never swallowed.
    if passthrough:
        async with aclosing(empty_output_stream(payload, stream_request_func)) as stream:
            async for chunk in stream:
                yield chunk
        return

    previous_usage = {}
    continue_hint = {"text": "（继续）"}
    for attempt in range(3):
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
        if not processor.has_output and attempt < 2:
            prepare_empty_retry(payload, continue_hint, attempt)
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
        anti_truncation_gemini_stream(api_request, stream_request_func)
    )
