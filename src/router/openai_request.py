from typing import Any, Dict

from fastapi import HTTPException, Request


async def parse_openai_chat_request(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="OpenAI request body must be a JSON object")

    normalized = {key: value for key, value in body.items() if value is not None}
    model = normalized.get("model")
    messages = normalized.get("messages")

    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="OpenAI request must include a model string")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="OpenAI request must include a messages list")

    return normalized
