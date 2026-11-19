"""agy2api-compatible request IDs, labels and field ordering."""
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

def _stable_json(value: Any) -> Optional[str]:
    """稳定序列化（排序键），失败返回 None"""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None

def _anchor_part_feature(part: Any) -> Optional[str]:
    """提取单个 part 的稳定特征（文本或首个 functionCall 的名称+参数）。"""
    if not isinstance(part, dict):
        return None
    if isinstance(part.get("text"), str) and part["text"]:
        return f"text:{part['text']}"
    fc = part.get("functionCall") or part.get("function_call")
    if isinstance(fc, dict):
        name = fc.get("name") or ""
        args_json = _stable_json(fc.get("args"))
        if args_json is not None:
            return f"fc:{name}:{args_json}"
        return f"fc:{name}"
    return None

def _anchor_message_features(content: Any) -> Optional[str]:
    """提取一条消息的稳定特征（按 part 顺序拼接，最多取前 4 个 part）。"""
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    features = []
    for part in parts[:4]:
        feature = _anchor_part_feature(part)
        if feature is not None:
            features.append(feature)
    return "|".join(features) if features else None

def derive_conversation_anchor(
    model_name: str, gemini_request: Dict[str, Any]
) -> Optional[bytes]:
    """
    派生会话联合锚点摘要: SHA256(model + systemInstruction 文本 + 前两条消息特征)。

    联合摘要同时供两处使用，保证锚点口径一致:
    - derive_session_key: 凭证亲和会话 key
    - wrap_cli_request: 上游 sessionId / trajectory_id 派生

    锚点必须包含前两条消息特征，避免共用相同 systemInstruction 的
    不同会话（多 Agent / 主子会话场景）派生出相同的 key 发生撞车。

    必须在格式转换完成后调用（openai2gemini / anthropic2gemini / 原生 gemini 都已
    转成 Gemini 格式），保证不同 API 格式的相同内容派生相同的锚点。

    Returns:
        32 字节 SHA256 摘要；无法派生锚点时返回 None
    """
    if not isinstance(gemini_request, dict):
        return None

    model_part = model_name if isinstance(model_name, str) else ""

    # systemInstruction 文本（对话稳定的锚点之一）
    system_text = ""
    system_instruction = gemini_request.get("systemInstruction")
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts")
        if isinstance(parts, list):
            texts = [
                str(part.get("text"))
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            system_text = "\n".join(texts)

    # 前两条消息特征（区分同 systemInstruction 下的不同会话）
    message_features: List[str] = []
    contents = gemini_request.get("contents")
    if isinstance(contents, list):
        for content in contents[:2]:
            feature = _anchor_message_features(content)
            if feature is not None:
                message_features.append(feature)
    messages_part = "\x1f".join(message_features)

    if not system_text and not messages_part:
        return None

    anchor_material = f"{model_part}\x1e{system_text}\x1e{messages_part}"
    return hashlib.sha256(anchor_material.encode("utf-8")).digest()

def _build_labels(model: str, trajectory_id: str, step: int) -> Dict[str, str]:
    """构建官方上下文标签元数据（键序按官方字母序排布）。

    官方样本（HAR 抓包）：
    {
      "last_step_index": "2",
      "model_enum": "MODEL_PLACEHOLDER_M320",
      "request_id": "0917011e-4959-4da9-8554-20499ed72869-2",
      "trajectory_id": "0917011e-4959-4da9-8554-20499ed72869",
      "used_claude": "false",
      "used_claude_conservative": "false",
      "used_non_gemini_model": "false"
    }

    step 传入「当前步索引」（模型轮数 + 1），与顶层 requestId 末段同步，
    request_id 同步使用该值。
    """
    model_lower = model.lower() if model else ""
    used_claude = "claude" in model_lower
    # used_non_gemini_model：Claude / gpt-oss 等非 Gemini 家族模型为 true
    used_non_gemini = used_claude or "gpt-oss" in model_lower or "grok" in model_lower
    return {
        "last_step_index": str(step),
        "model_enum": model,
        "request_id": f"{trajectory_id}-{step}",
        "trajectory_id": trajectory_id,
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
        "used_non_gemini_model": str(used_non_gemini).lower(),
    }

_JSON_ORDER = {
    "": ("project", "requestId", "request", "model", "userAgent", "requestType"),
    "request": ("contents", "systemInstruction", "tools", "toolConfig", "labels", "generationConfig", "sessionId"),
    "request.contents[]": ("role", "parts"),
    "request.systemInstruction": ("role", "parts"),
    "request.generationConfig": ("candidateCount", "maxOutputTokens", "thinkingConfig", "temperature", "topP", "topK"),
    "request.generationConfig.thinkingConfig": ("includeThoughts", "thinkingLevel", "thinkingBudget"),
    "request.tools[]": ("functionDeclarations",),
    "request.tools[].functionDeclarations[]": ("name", "description", "parameters"),
    "request.tools[].functionDeclarations[].parameters": ("type", "properties", "required"),
}


def order_request(value, path=""):
    if isinstance(value, list) and path + "[]" in _JSON_ORDER:
        return [order_request(item, path + "[]") for item in value]
    if not isinstance(value, dict) or (path not in _JSON_ORDER and path != "request.labels"):
        return value
    keys = sorted(value) if path == "request.labels" else dict.fromkeys((*_JSON_ORDER[path], *value))
    return {key: order_request(value[key], f"{path}.{key}" if path else key)
            for key in keys if key in value}



def derive_session_ids(request, model):
    digest = derive_conversation_anchor(model, request)
    if digest is None:
        return "", ""
    return f"-{int.from_bytes(digest[:8], 'big') & 0x7FFFFFFFFFFFFFFF}", str(uuid.UUID(bytes=digest[8:24]))


def bind_request_identity(request, model="", account_id=None):
    _, trajectory = derive_session_ids(request, model)
    trajectory = trajectory or str(uuid.uuid4())
    step = 1 + sum(isinstance(content, dict) and content.get("role") == "model" for content in request.get("contents", []))
    request["labels"] = _build_labels(model, trajectory, step)
    conversation = hashlib.sha256(str(request["sessionId"]).encode()).hexdigest()[:16]
    return conversation, trajectory, step
