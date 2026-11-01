from src.converter.chatbox_fix import (
    CHATBOX_RUNTIME_DESCRIPTION,
    CHATBOX_RUNTIME_INSTRUCTION,
    strip_chatbox_runtime_description,
)


def test_removes_only_known_description_without_mutating_messages():
    for description in (CHATBOX_RUNTIME_DESCRIPTION, CHATBOX_RUNTIME_INSTRUCTION):
        original = {
            "systemInstruction": {"parts": [{"text": "before\n" + description + "\nafter"}]},
            "contents": [{"role": "user", "parts": [{"text": description}, {"inlineData": {"data": "image"}}]}],
            "tools": [{"functionDeclarations": [{"name": "example"}]}],
        }
        cleaned = strip_chatbox_runtime_description(original)
        assert cleaned["systemInstruction"]["parts"][0]["text"] == "before\n\nafter"
        assert cleaned["contents"][0]["parts"] == [{"inlineData": {"data": "image"}}]
        assert cleaned["tools"] is original["tools"]
        assert description in original["systemInstruction"]["parts"][0]["text"]


def test_preserves_real_reminders_and_drops_empty_system():
    real = {"role": "user", "parts": [{"text": "<system-reminder>\nCurrent date and time: today\n</system-reminder>"}]}
    original = {"systemInstruction": {"parts": [{"text": CHATBOX_RUNTIME_INSTRUCTION}]}, "contents": [real]}
    cleaned = strip_chatbox_runtime_description(original)
    assert "systemInstruction" not in cleaned
    assert cleaned["contents"] is original["contents"]
