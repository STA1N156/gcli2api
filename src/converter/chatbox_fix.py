"""Remove the known Chatbox runtime description, not user reminder blocks."""

CHATBOX_RUNTIME_DESCRIPTION = (
    "<system-reminder>...</system-reminder> blocks are out-of-band runtime metadata from Chatbox "
    "(e.g. the current date and time), not part of the user's message."
)
CHATBOX_RUNTIME_INSTRUCTION = (
    CHATBOX_RUNTIME_DESCRIPTION
    + " Treat their contents as authoritative; do not quote or acknowledge them."
)


def _clean_content(content):
    parts = content.get("parts")
    if not isinstance(parts, list):
        return content
    cleaned = None
    for index, part in enumerate(parts):
        text = part.get("text") if isinstance(part, dict) else None
        replacement = text
        if isinstance(text, str) and not part.get("thought") and "out-of-band runtime metadata from Chatbox" in text:
            replacement = text.replace(CHATBOX_RUNTIME_INSTRUCTION, "").replace(CHATBOX_RUNTIME_DESCRIPTION, "")
        if replacement != text:
            if cleaned is None:
                cleaned = parts[:index]
            if replacement or len(part) > 1:
                cleaned.append({**part, "text": replacement})
        elif cleaned is not None:
            cleaned.append(part)
    return {**content, "parts": cleaned} if cleaned is not None else content


def strip_chatbox_runtime_description(request):
    """Copy only changed text blocks; leave tools, media and caller data intact."""
    result = request.copy()
    system = request.get("systemInstruction")
    if isinstance(system, dict):
        system = _clean_content(system)
        if system.get("parts") == []:
            result.pop("systemInstruction", None)
        else:
            result["systemInstruction"] = system
    contents = request.get("contents")
    if isinstance(contents, list):
        cleaned = None
        for index, item in enumerate(contents):
            replacement = _clean_content(item) if isinstance(item, dict) else item
            if replacement is not item:
                if cleaned is None:
                    cleaned = contents.copy()
                cleaned[index] = replacement
        if cleaned is not None:
            result["contents"] = cleaned
    return result
