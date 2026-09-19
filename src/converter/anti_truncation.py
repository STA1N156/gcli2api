"""RP-Hub-style anti-truncation: carry the reply in a function argument."""

from typing import Any, Dict


REPLY_TOOL_NAME = "output_reply"
REPLY_TOOL_INSTRUCTION = (
    "本次回复的正文必须通过 output_reply 工具的 content 字段提交，"
    "保留原有格式，不要在普通消息中重复输出。"
)
REPLY_TOOL_DESCRIPTION = (
    "将本次回复交给聊天界面显示。遵守现有输出规则，"
    "正文及需要附带的全部内容等全部放入 content。"
    "检索工具返回结果后，只传新增回复内容。"
)


def apply_anti_truncation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add the reply tool without mutating messages or overriding caller-owned tools."""
    request = payload.get("request", {})
    tools = request.get("tools") or []
    declarations = [
        declaration
        for tool in tools
        for declaration in tool.get("functionDeclarations", [])
    ]
    config = request.get("toolConfig") or {}
    function_config = config.get("functionCallingConfig") or {}
    # RP-Hub may already supply output_reply. Its caller must receive that tool
    # unchanged. Explicit required/none/named choices also belong to the caller.
    if any(item.get("name") == REPLY_TOOL_NAME for item in declarations) or (
        function_config.get("mode") in ("ANY", "NONE")
    ):
        return payload

    reply_tool = {
        "name": REPLY_TOOL_NAME,
        "description": REPLY_TOOL_DESCRIPTION,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {
                    "type": "STRING",
                    "description": "本次回复的原文，保留原有格式；作为 JSON 字符串正确转义。",
                }
            },
            "required": ["content"],
        },
    }
    function_config = {**function_config, "mode": "ANY"}
    # Other functions remain available, just as RP-Hub uses required when its
    # caller has enabled real tools. With no other functions, force output_reply.
    function_config.pop("allowedFunctionNames", None)
    if not declarations:
        function_config["allowedFunctionNames"] = [REPLY_TOOL_NAME]
    system_instruction = dict(request.get("systemInstruction") or {})
    system_parts = list(system_instruction.get("parts") or [])
    if not any(REPLY_TOOL_INSTRUCTION in part.get("text", "") for part in system_parts):
        system_parts.append({"text": REPLY_TOOL_INSTRUCTION})
    system_instruction["parts"] = system_parts
    return {
        **payload,
        "request": {
            **request,
            "systemInstruction": system_instruction,
            "tools": [*tools, {"functionDeclarations": [reply_tool]}],
            "toolConfig": {**config, "functionCallingConfig": function_config},
        },
    }


class ReplyToolStream:
    """Convert native Gemini reply-tool parts to text; keep real tools intact.

    The API client supplies complete JSON SSE lines. Unlike RP-Hub's OpenAI
    transport, Gemini functionCall.args is already a decoded object, so it does
    not need a second incremental JSON-string parser.
    """

    def __init__(self):
        self.candidates = {}
        self.metadata = {}
        self.usage = {}
        self.wrapper = None
        self.has_activity = False
        self.has_output = False

    def _wrap(self, response):
        return {**self.wrapper, "response": response} if self.wrapper is not None else response

    def process(self, data: Dict[str, Any]):
        response = data.get("response", data)
        if "response" in data:
            self.wrapper = {key: value for key, value in data.items() if key != "response"}
        self.metadata.update({
            key: value for key, value in response.items()
            if key not in ("candidates", "usageMetadata")
        })
        self.usage.update(response.get("usageMetadata") or {})
        if (response.get("promptFeedback") or {}).get("blockReason"):
            self.has_activity = True

        outgoing = []
        for position, candidate in enumerate(response.get("candidates") or []):
            index = candidate.get("index", position)
            state = self.candidates.setdefault(index, {
                "plain": [], "tools": [], "reply": [], "reply_seen": False,
                "invalid_reply": False,
                "candidate": {"index": index},
            })
            state["candidate"].update({
                key: value for key, value in candidate.items() if key != "content"
            })
            reason = candidate.get("finishReason")
            if reason and reason not in ("STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
                self.has_activity = True
                state["invalid_reply"] = True

            parts = []
            for part in (candidate.get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    self.has_activity = True
                    continue
                call = part.get("functionCall")
                if call is not None and not isinstance(call, dict):
                    self.has_activity = True
                    continue
                if call is not None and call.get("name") == REPLY_TOOL_NAME:
                    self.has_activity = True
                    args = call.get("args")
                    if (state["reply_seen"] or not isinstance(args, dict)
                            or set(args) != {"content"} or not isinstance(args["content"], str)):
                        state["invalid_reply"] = True
                    elif args["content"].strip():
                        state["reply"].append({"text": args["content"]})
                    state["reply_seen"] = True
                elif call is not None:
                    self.has_activity = self.has_output = True
                    state["tools"].append(part)
                elif "text" in part and not isinstance(part["text"], str):
                    self.has_activity = True
                elif "text" in part and not part.get("thought"):
                    state["plain"].append(part)
                    self.has_activity |= bool(part["text"].strip())
                else:
                    parts.append(part)
                    self.has_activity |= bool(part.get("text", "").strip())
                    if any(key in part for key in (
                        "inlineData", "fileData", "executableCode", "codeExecutionResult",
                    )):
                        self.has_activity = self.has_output = True
            if parts:
                outgoing.append({"index": index, "content": {"role": "model", "parts": parts}})
        if outgoing:
            return self._wrap({**self.metadata, "candidates": outgoing})
        return None

    def finish(self):
        """Choose one body after validation, so later malformed calls cannot duplicate it."""
        candidates = []
        for state in self.candidates.values():
            parts = state["reply"] if (
                state["reply"] and not state["invalid_reply"] and not state["tools"]
            ) else state["plain"]
            parts = [*parts, *state["tools"]]
            self.has_output |= any(part.get("text", "").strip() for part in parts)
            candidates.append({
                **state["candidate"],
                "content": {"role": "model", "parts": parts},
            })
        return self._wrap({
            **self.metadata, "candidates": candidates, "usageMetadata": self.usage,
        })
