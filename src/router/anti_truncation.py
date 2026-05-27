from typing import Any, AsyncIterator, Callable, Dict

from fastapi import Response
from fastapi.responses import StreamingResponse

from src.api.utils import collect_streaming_response
from src.converter.anti_truncation import AntiTruncationStreamProcessor, apply_anti_truncation
from src.router.stream_passthrough import prepend_async_item, read_first_async_item


async def anti_truncation_gemini_stream(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
    max_attempts: int,
) -> AsyncIterator[Any]:
    """Run anti-truncation as a Gemini SSE stream."""
    anti_truncation_payload = apply_anti_truncation(api_request)

    first_attempt_stream = stream_request_func(body=anti_truncation_payload, native=False)
    try:
        first_chunk = await read_first_async_item(first_attempt_stream)
    except StopAsyncIteration:
        return

    if isinstance(first_chunk, Response):
        yield first_chunk
        return

    first_attempt_pending = True

    async def stream_request_wrapper(payload: Dict[str, Any]) -> StreamingResponse:
        nonlocal first_attempt_pending

        if first_attempt_pending:
            first_attempt_pending = False
            stream_gen = prepend_async_item(first_chunk, first_attempt_stream)
        else:
            stream_gen = stream_request_func(body=payload, native=False)

        return StreamingResponse(stream_gen, media_type="text/event-stream")

    processor = AntiTruncationStreamProcessor(
        stream_request_wrapper,
        anti_truncation_payload,
        max_attempts,
        enable_prefill_mode=True,
    )

    async for chunk in processor.process_stream():
        yield chunk


async def collect_anti_truncation_response(
    api_request: Dict[str, Any],
    stream_request_func: Callable[..., AsyncIterator[Any]],
    max_attempts: int,
) -> Response:
    """Run anti-truncation and collect the Gemini SSE stream into a non-stream response."""
    stream = anti_truncation_gemini_stream(api_request, stream_request_func, max_attempts)
    return await collect_streaming_response(stream)
