import json
from typing import Any, AsyncIterator

from fastapi import Response
from fastapi.responses import StreamingResponse
from src.converter.image_input import ImageInputError


async def prepend_async_item(first_item: Any, iterator: AsyncIterator[Any]):
    """Yield a prefetched item before continuing the original iterator."""
    try:
        yield first_item
        async for item in iterator:
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            await aclose()


async def read_first_async_item(iterator: AsyncIterator[Any]) -> Any:
    """Python 3.9-compatible async equivalent of built-in anext()."""
    return await iterator.__anext__()


async def build_streaming_response_or_error(
    iterator: AsyncIterator[Any],
    media_type: str = "text/event-stream",
):
    """
    Prefetch the first async item so router code can return an upstream error
    response directly before FastAPI commits a 200 streaming response.
    """
    try:
        first_item = await read_first_async_item(iterator)
    except StopAsyncIteration:
        return Response(status_code=204)
    except ImageInputError as exc:
        return Response(
            content=json.dumps({"error": {"message": f"Invalid image input: {exc}"}}),
            status_code=400,
            media_type="application/json",
        )

    if isinstance(first_item, Response):
        return first_item

    return StreamingResponse(
        prepend_async_item(first_item, iterator),
        media_type=media_type,
    )


def unwrap_gemini_response_sse_chunk(chunk: Any) -> Any:
    if not isinstance(chunk, (str, bytes)):
        return chunk

    is_bytes = isinstance(chunk, bytes)
    prefix = b"data: " if is_bytes else "data: "
    response_marker = b'"response"' if is_bytes else '"response"'

    if not chunk.startswith(prefix):
        return chunk

    payload = chunk[len(prefix):].strip()
    if payload == (b"[DONE]" if is_bytes else "[DONE]") or response_marker not in payload:
        return chunk

    try:
        data = json.loads(payload.decode("utf-8") if is_bytes else payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return chunk

    if "response" not in data or "candidates" in data:
        return chunk

    unwrapped_chunk = (
        "data: "
        + json.dumps(data["response"], ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )
    return unwrapped_chunk.encode("utf-8") if is_bytes else unwrapped_chunk
