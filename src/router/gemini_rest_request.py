from typing import Any, Dict


class GeminiRestRequestError(ValueError):
    pass


_WRAPPED_REQUEST_FIELDS = (
    "contents",
    "systemInstruction",
    "system_instruction",
    "generationConfig",
    "generation_config",
    "safetySettings",
    "safety_settings",
    "tools",
    "toolConfig",
    "tool_config",
    "cachedContent",
    "cached_content",
)


def normalize_gemini_rest_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Accept direct Gemini REST bodies and SDK-style generateContentRequest bodies."""
    if not isinstance(body, dict):
        raise GeminiRestRequestError("Gemini request body must be a JSON object")

    wrapper = body.get("generateContentRequest")
    if wrapper is None:
        normalized = dict(body)
    else:
        if not isinstance(wrapper, dict):
            raise GeminiRestRequestError("generateContentRequest must be a JSON object")

        normalized = dict(wrapper)
        for key in _WRAPPED_REQUEST_FIELDS:
            if key in body and key not in normalized:
                normalized[key] = body[key]

    contents = normalized.get("contents")
    if not isinstance(contents, list):
        raise GeminiRestRequestError("Gemini request must include a contents list")

    normalized["contents"] = [
        _normalize_content(content)
        for content in contents
    ]

    return normalized


def _normalize_content(content: Any) -> Any:
    if not isinstance(content, dict):
        return content

    normalized = dict(content)
    normalized.setdefault("role", "user")
    return normalized
