"""
Antigravity Format Utilities - 独立的 Antigravity 请求处理和转换工具
专供 src/router/antigravity 使用
────────────────────────────────────────────────────────────────
"""
import json
import hashlib
from typing import Any, Dict, List, Optional

from log import log
from src.converter.chatbox_fix import strip_chatbox_runtime_description
from src.converter.image_input import normalize_inline_image_part
from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR
from src.converter.utils import normalize_schema_enum

# ==================== Gemini API 配置 ====================

DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "OFF"},
]

CLAUDE_MAX_OUTPUT_TOKENS = 64000
GEMINI_MAX_OUTPUT_TOKENS = 65536
CLAUDE_MIN_THINKING_BUDGET = 1024


def _append_schema_hint(schema: Dict[str, Any], hint: str) -> None:
    """Move fragile validation details into description instead of sending them raw."""
    if not hint:
        return
    desc = schema.get("description")
    schema["description"] = f"{desc} ({hint})" if desc else hint


def _resolve_schema_ref(ref: str, root_schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None

    node: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]

    return node if isinstance(node, dict) else None


def _clean_parameters_json_schema(
    schema: Any,
    root_schema: Optional[Dict[str, Any]] = None,
    visited: Optional[set] = None,
) -> Any:
    """Clean a tool schema for Code Assist's parametersJsonSchema field."""
    if isinstance(schema, list):
        return [_clean_parameters_json_schema(item, root_schema, visited) for item in schema]
    if not isinstance(schema, dict):
        return schema

    if root_schema is None:
        root_schema = schema
    if visited is None:
        visited = set()

    schema_id = id(schema)
    if schema_id in visited:
        return {"type": "object", "description": "circular reference"}
    visited.add(schema_id)

    ref_key = "$ref" if "$ref" in schema else ("ref" if "ref" in schema else None)
    if ref_key:
        resolved = _resolve_schema_ref(schema[ref_key], root_schema)
        if resolved:
            merged = dict(resolved)
            for key in ("description", "default"):
                if key in schema:
                    merged[key] = schema[key]
            schema = merged

    if "allOf" in schema:
        result: Dict[str, Any] = {}
        for item in schema.get("allOf") or []:
            cleaned_item = _clean_parameters_json_schema(item, root_schema, visited)
            if not isinstance(cleaned_item, dict):
                continue
            if "properties" in cleaned_item:
                result.setdefault("properties", {}).update(cleaned_item["properties"])
            if "required" in cleaned_item:
                result.setdefault("required", []).extend(cleaned_item["required"])
            for key, value in cleaned_item.items():
                if key not in ("properties", "required"):
                    result[key] = value
        for key, value in schema.items():
            if key not in ("allOf", "properties", "required"):
                result[key] = value
            elif key in ("properties", "required") and key not in result:
                result[key] = value
    else:
        result = dict(schema)

    if result.get("nullable") is True:
        _append_schema_hint(result, "nullable")

    if "type" in result:
        type_value = result["type"]
        if isinstance(type_value, list):
            non_null_types = [
                str(t).lower()
                for t in type_value
                if isinstance(t, str) and t.lower() != "null"
            ]
            if non_null_types:
                result["type"] = non_null_types[0]
                if any(str(t).lower() == "null" for t in type_value):
                    _append_schema_hint(result, "nullable")
            else:
                result["type"] = "string"
        elif isinstance(type_value, str):
            lower_type = type_value.lower()
            if lower_type in {"string", "number", "integer", "boolean", "array", "object"}:
                result["type"] = lower_type
            elif lower_type == "null":
                result["type"] = "string"
                _append_schema_hint(result, "nullable")
            else:
                result.pop("type", None)

    if "anyOf" in result or "oneOf" in result:
        union_key = "anyOf" if "anyOf" in result else "oneOf"
        union_items = result.get(union_key) or []
        cleaned_items = [
            item for item in (
                _clean_parameters_json_schema(item, root_schema, visited)
                for item in union_items
            )
            if isinstance(item, dict)
        ]
        enum_values = [
            item.get("const")
            for item in union_items
            if isinstance(item, dict) and item.get("const") not in ("", None)
        ]
        if enum_values and len(enum_values) == len(union_items):
            result["type"] = "string"
            result["enum"] = [str(v) for v in enum_values]
        else:
            preferred = next(
                (
                    item for item in cleaned_items
                    if item.get("type") in ("object", "array") or item.get("properties")
                ),
                None,
            )
            if preferred is None:
                preferred = next((item for item in cleaned_items if item.get("type") or item.get("enum")), None)
            if preferred:
                original_description = result.get("description")
                result.update(preferred)
                if original_description:
                    _append_schema_hint(result, original_description)
        result.pop("anyOf", None)
        result.pop("oneOf", None)

    if result.get("type") == "array":
        items = result.get("items")
        if isinstance(items, list):
            if items:
                result["items"] = _clean_parameters_json_schema(items[0], root_schema, visited)
                _append_schema_hint(result, "tuple schema simplified")
            else:
                result.pop("items", None)
        elif isinstance(items, dict):
            result["items"] = _clean_parameters_json_schema(items, root_schema, visited)

    validation_keys = {
        "default", "minLength", "maxLength", "minimum", "maximum",
        "minItems", "maxItems", "pattern", "format", "uniqueItems",
    }
    for key in list(result.keys()):
        if key in validation_keys:
            value = result.pop(key)
            if value not in (None, "", {}, []):
                _append_schema_hint(result, f"{key}: {json.dumps(value, ensure_ascii=False)}")

    unsupported_keys = {
        "title", "$schema", "$id", "$ref", "ref", "strict", "nullable",
        "exclusiveMaximum", "exclusiveMinimum", "additionalProperties",
        "allOf", "anyOf", "oneOf", "$defs", "definitions", "example",
        "examples", "readOnly", "writeOnly", "const", "additionalItems",
        "contains", "patternProperties", "dependencies", "propertyNames",
        "if", "then", "else", "contentEncoding", "contentMediaType", "encrypted",
    }
    for key in list(result.keys()):
        if key in unsupported_keys or key.startswith("x-"):
            del result[key]

    if isinstance(result.get("properties"), dict):
        cleaned_props = {}
        for prop_name, prop_schema in result["properties"].items():
            cleaned_props[prop_name] = _clean_parameters_json_schema(prop_schema, root_schema, visited)
        result["properties"] = cleaned_props

    if "properties" in result and "type" not in result:
        result["type"] = "object"

    if isinstance(result.get("required"), list):
        prop_names = set(result.get("properties", {}).keys()) if isinstance(result.get("properties"), dict) else None
        required = []
        for item in result["required"]:
            if not isinstance(item, str):
                continue
            if prop_names is not None and item not in prop_names:
                continue
            if item not in required:
                required.append(item)
        if required:
            result["required"] = required
        else:
            result.pop("required", None)

    normalize_schema_enum(result)
    return result


def _normalize_tools_for_internal_api(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools

    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized_tools.append(tool)
            continue

        normalized_tool = tool.copy()
        declarations = normalized_tool.get("functionDeclarations")
        if declarations is None:
            declarations = normalized_tool.get("function_declarations")
        if isinstance(declarations, list):
            normalized_declarations = []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    normalized_declarations.append(declaration)
                    continue

                normalized_declaration = declaration.copy()
                if "parametersJsonSchema" in normalized_declaration:
                    schema = normalized_declaration["parametersJsonSchema"]
                elif "parameters_json_schema" in normalized_declaration:
                    schema = normalized_declaration.pop("parameters_json_schema", None)
                else:
                    schema = normalized_declaration.pop("parameters", None)

                normalized_declaration.pop("parameters", None)
                normalized_declaration.pop("parameters_json_schema", None)
                if schema not in (None, {}, []):
                    normalized_declaration["parametersJsonSchema"] = _clean_parameters_json_schema(schema)
                else:
                    normalized_declaration.pop("parametersJsonSchema", None)

                normalized_declarations.append(normalized_declaration)

            normalized_tool.pop("function_declarations", None)
            normalized_tool["functionDeclarations"] = normalized_declarations

        normalized_tools.append(normalized_tool)

    return normalized_tools


def _ensure_empty_tool_schema_for_claude(tools: Any, model_name: str, mode: str = "antigravity") -> Any:
    if not isinstance(tools, list):
        return tools

    is_claude = "claude" in (model_name or "").lower()

    if is_claude:
        normalized_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                normalized_tools.append(tool)
                continue

            normalized_tool = tool.copy()

            schema = {"type": "object", "properties": {}}
            name = ""
            description = ""

            # Extract schema from either format
            custom_tool = normalized_tool.get("custom")
            if isinstance(custom_tool, dict):
                schema = custom_tool.get("input_schema") or custom_tool.get("inputSchema") or schema
                name = custom_tool.get("name", "")
                description = custom_tool.get("description", "")
            else:
                declarations = normalized_tool.get("functionDeclarations") or normalized_tool.get("function_declarations")
                if isinstance(declarations, list) and declarations and isinstance(declarations[0], dict):
                    decl = declarations[0]
                    schema = (
                        decl.get("parametersJsonSchema") or
                        decl.get("parameters_json_schema") or
                        decl.get("parameters") or schema
                    )
                    name = decl.get("name", "")
                    description = decl.get("description", "")

            # For ALL Claude models, try outputting functionDeclarations with parameters!
            # If Google's backend expects parameters to translate to input_schema, this will fix the Field required error.
            normalized_tools.append({
                "functionDeclarations": [{
                    "name": name,
                    "description": description,
                    "parameters": schema
                }]
            })

        return normalized_tools

    # 对于 Gemini 模型：
    # 后端需要标准的 functionDeclarations 格式。
    # 并且，必须只能使用 "parameters" 字段，如果使用了 "parametersJsonSchema"，
    # 会报 "parameters_json_schema must not be set when parameters is set" 等冲突错误
    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized_tools.append(tool)
            continue

        normalized_tool = tool.copy()

        # 1. 如果包含 Anthropic 原生的 "custom" 工具格式，将其转换为 Gemini 的 functionDeclarations 格式
        custom_tool = normalized_tool.get("custom")
        if isinstance(custom_tool, dict):
            schema = custom_tool.get("input_schema") or custom_tool.get("inputSchema")
            if schema in (None, {}, []):
                schema = {"type": "object", "properties": {}}
            declaration = {
                "name": custom_tool.get("name", ""),
                "description": custom_tool.get("description", ""),
                "parameters": schema
            }
            normalized_tools.append({
                "functionDeclarations": [declaration]
            })
            continue

        # 2. 如果包含标准的 functionDeclarations 格式，确保参数不为空且只使用 parameters 字段
        declarations = normalized_tool.get("functionDeclarations") or normalized_tool.get("function_declarations")
        if isinstance(declarations, list):
            normalized_declarations = []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    normalized_declarations.append(declaration)
                    continue

                normalized_declaration = declaration.copy()
                # 兼容不同字段格式并归一化到 parameters
                schema = (
                    normalized_declaration.get("parameters")
                    or normalized_declaration.get("parametersJsonSchema")
                    or normalized_declaration.get("parameters_json_schema")
                )

                if schema in (None, {}, []):
                    schema = {"type": "object", "properties": {}}

                # 只保留 parameters 字段，防止与 parametersJsonSchema 冲突
                normalized_declaration["parameters"] = schema
                normalized_declaration.pop("parametersJsonSchema", None)
                normalized_declaration.pop("parameters_json_schema", None)

                normalized_declarations.append(normalized_declaration)

            normalized_tool.pop("function_declarations", None)
            normalized_tool["functionDeclarations"] = normalized_declarations

        normalized_tools.append(normalized_tool)

    return normalized_tools


def _should_skip_thought_signature(part: Dict[str, Any], model_name: str) -> bool:
    if "claude" in (model_name or "").lower():
        return False

    return (
        "functionCall" in part
        or "function_call" in part
        or part.get("thought") is True
        or "thoughtSignature" in part
        or "thought_signature" in part
    )


def _normalize_part_thought_signature(part: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    normalized = part.copy()
    if _should_skip_thought_signature(normalized, model_name):
        normalized.pop("thought_signature", None)
        normalized["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE_VALIDATOR
    return normalized


def _stable_json(value: Any) -> Optional[str]:
    """稳定序列化（排序键），失败返回 None"""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None

def _deterministic_tool_id(material: str) -> str:
    """基于稳定材料确定性派生 toolu_ 前缀 ID（截断 32 hex）。

    同一段历史在多轮往返中补全出的 ID 恒定不变，保证 Claude 路径下
    KV 缓存依赖的前缀 token 序列字节级一致（随机 ID 会使前缀哈希
    从缺失处开始全部改变，导致上游缓存彻底失效）。
    """
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"toolu_{digest[:32]}"

def _ensure_tool_call_ids(contents: Any, model_name: str) -> Any:
    """
    确保 functionCall/functionResponse 携带 id 字段 (仅 Claude)。

    Antigravity 后端在目标模型为 Claude 时，会将 Gemini 的
    functionCall/functionResponse 内部转换为 Anthropic 的
    tool_use/tool_result，而后者的 id 是必填字段。原生 Gemini 请求
    可能不带 id，因此这里按 name 补全缺失的 id，保证同一次调用的
    functionCall 与 functionResponse 使用相同 id。不修改入参。

    缺失的 id 按「消息序号 + part 序号 + 函数名 + 载荷 JSON」确定性
    派生：历史前缀索引跨轮次稳定（append-only），因此同一历史补全出的
    ID 恒定，不会破坏多轮历史的字节级前缀一致性。
    """
    if "claude" not in (model_name or "").lower():
        return contents
    if not isinstance(contents, list):
        return contents

    pending_ids_by_name: Dict[str, list] = {}
    result = []

    for content_index, content in enumerate(contents):
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            result.append(content)
            continue

        new_parts = []
        for part_index, part in enumerate(content["parts"]):
            if isinstance(part, dict):
                fc = part.get("functionCall")
                fr = part.get("functionResponse")
                if isinstance(fc, dict):
                    if not fc.get("id"):
                        args_json = _stable_json(fc.get("args"))
                        material = f"{content_index}:{part_index}:{fc.get('name')}:{args_json}"
                        fc = {**fc, "id": _deterministic_tool_id(material)}
                        part = {**part, "functionCall": fc}
                    pending_ids_by_name.setdefault(fc.get("name"), []).append(fc["id"])
                elif isinstance(fr, dict):
                    queue = pending_ids_by_name.get(fr.get("name")) or []
                    if fr.get("id"):
                        if fr["id"] in queue:
                            queue.remove(fr["id"])
                    else:
                        if queue:
                            new_id = queue.pop(0)
                        else:
                            response_json = _stable_json(fr.get("response"))
                            material = f"{content_index}:{part_index}:{fr.get('name')}:{response_json}"
                            new_id = _deterministic_tool_id(material)
                        part = {**part, "functionResponse": {**fr, "id": new_id}}
            new_parts.append(part)

        result.append({**content, "parts": new_parts})

    return result


def _parse_size_to_image_config(size_str: str) -> Dict[str, str]:
    """
    解析用户传入的 size 参数为 Gemini imageConfig 参数

    支持格式: "1024x1536", "1024*1536", "1024X1536"

    Returns:
        包含 aspectRatio 和/或 imageSize 的字典
    """
    import re

    config = {}
    size_str = size_str.strip()

    match = re.match(r"^(\d+)\s*[xX*×]\s*(\d+)$", size_str)
    if not match:
        return config

    width, height = int(match.group(1)), int(match.group(2))

    if width <= 0 or height <= 0:
        return config

    # 计算最接近的支持宽高比
    target_ratio = width / height
    best_ratio = None
    best_diff = float("inf")
    for w, h in SUPPORTED_ASPECT_RATIOS:
        diff = abs(target_ratio - w / h)
        if diff < best_diff:
            best_diff = diff
            best_ratio = f"{w}:{h}"
    if best_ratio:
        config["aspectRatio"] = best_ratio

    # 根据最大边长确定 imageSize（使用最接近的档位）
    max_dim = max(width, height)
    if max_dim <= 1280:
        config["imageSize"] = "1K"
    elif max_dim <= 2560:
        config["imageSize"] = "2K"
    else:
        config["imageSize"] = "4K"

    return config


def prepare_image_generation_request(
    request_body: Dict[str, Any],
    model: str
) -> Dict[str, Any]:
    """
    图像生成模型请求体后处理

    图片参数优先级（从高到低）:
    1. size 参数: 如 "1024x1536"，自动计算 aspectRatio 和 imageSize
    2. 模型名后缀: 如 -4k, -2k, -16x9, -1x1
    3. generationConfig.imageConfig 中的图片参数
    4. 默认值: 不设置额外参数

    Args:
        request_body: 原始请求体
        model: 模型名称

    Returns:
        处理后的请求体
    """
    request_body = request_body.copy()
    model_lower = model.lower()
    generation_config = request_body.get("generationConfig") or {}
    image_config = dict(generation_config.get("imageConfig") or {})

    # 优先使用 size 参数
    size_str = request_body.pop("size", None)
    if size_str:
        image_config.update(_parse_size_to_image_config(size_str))
        log.debug(f"[IMAGE] 从 size 参数 '{size_str}' 解析: {image_config}")
    else:
        # 从模型名后缀解析
        image_size = "4K" if "-4k" in model_lower else "2K" if "-2k" in model_lower else None

        aspect_ratio = None
        for suffix, ratio in [
            ("-21x9", "21:9"), ("-16x9", "16:9"), ("-9x16", "9:16"),
            ("-4x3", "4:3"), ("-3x4", "3:4"), ("-1x1", "1:1")
        ]:
            if suffix in model_lower:
                aspect_ratio = ratio
                break

        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size

    request_body["model"] = "gemini-3.1-flash-image"  # 统一使用基础模型名
    request_body["generationConfig"] = {
        "candidateCount": 1,
        "responseModalities": generation_config.get("responseModalities") or ["TEXT", "IMAGE"],
        "imageConfig": image_config
    }

    # 移除不需要的字段
    for key in ("systemInstruction", "tools", "toolConfig"):
        request_body.pop(key, None)

    return request_body


# ==================== 模型特性辅助函数 ====================

def is_thinking_model(model_name: str) -> bool:
    """检查是否为思考模型 (模型名包含 think)"""
    return "think" in model_name.lower()


def _clamp_temperature(generation_config: Dict[str, Any]) -> None:
    temperature = generation_config.get("temperature")
    if temperature is None:
        return
    try:
        generation_config["temperature"] = min(float(temperature), 1.0)
    except (TypeError, ValueError):
        generation_config.pop("temperature", None)


def _normalize_antigravity_request(
    result: Dict[str, Any],
    model: str,
    generation_config: Dict[str, Any],
    return_thoughts: bool,
) -> str:
    """antigravity 模式专属处理，返回处理后的模型名"""
    # 客户端参数优先，模型后缀只提供默认思考等级。
    thinking = is_thinking_model(model)
    # Gemini 3.8 Flash 和所有 Gemini 4 的 high 保留上游默认等级；max/xhigh 明确使用 HIGH。
    default_thinking = model.lower() == "gemini-3.8-flash-high" or (
        model.lower().startswith("gemini-4") and model.lower().endswith("-high")
    )
    supports_thinking_level = (
        model.lower().startswith(("gemini-3", "gemini-4")) or "claude" in model.lower()
    )
    if supports_thinking_level and model.lower().endswith(("-max", "-xhigh")):
        model = model.rsplit("-", 1)[0] + "-high"
    legacy_claude_thinking = model.lower() in {
        "claude-opus-4-6-thinking", "claude-sonnet-4-6-thinking"
    }
    suffix = "" if default_thinking else model.lower().rsplit("-", 1)[-1]
    if "claude" in model.lower() and suffix == "thinking":
        suffix = "high"
    thinking_config = generation_config.get("thinkingConfig", {})
    if legacy_claude_thinking and not thinking_config.get("thinkingLevel"):
        # 4.6 的旧 thinking 通道靠预算开启思考，不能只发送等级。
        thinking_config = generation_config.setdefault("thinkingConfig", {})
        if thinking_config.get("thinkingBudget") is None:
            thinking_config["thinkingBudget"] = 8192
    thinking_level = str(thinking_config.get("thinkingLevel") or "").lower()
    if thinking_level in {"none", "minimal"}:
        thinking_config["thinkingLevel"] = "LOW"
    elif thinking_level in {"max", "xhigh"}:
        thinking_config["thinkingLevel"] = "HIGH"
    client_thinking = bool(thinking_config.get("thinkingLevel")) or thinking_config.get("thinkingBudget") is not None
    if (
        supports_thinking_level and not client_thinking
        and suffix in {"minimal", "low", "medium", "high"}
    ):
        generation_config.setdefault("thinkingConfig", {})["thinkingLevel"] = suffix.upper()
    thinking_config = generation_config.get("thinkingConfig", {})
    if thinking_config.get("thinkingLevel"):
        thinking_config.pop("thinkingBudget", None)

    # 针对 Gemini 模型：根据思考设置映射至真实的 Antigravity 后端模型 ID
    if "gemini" in model.lower():
        original_model = model

        # Flash Lite 的等级别名只用于接收请求，上游使用基础型号。
        if (
            suffix in {"low", "medium", "high"}
            and original_model.lower().rsplit("-", 1)[0] in {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite"}
        ):
            model = original_model.lower().rsplit("-", 1)[0]

        # 兼容旧的客户端别名：Antigravity 后端的 Gemini 3.1 Pro High
        # 实际使用 gemini-pro-agent 作为模型 ID。
        elif model.lower() == "gemini-3.1-pro-high":
            model = "gemini-pro-agent"
            log.debug(f"[ANTIGRAVITY] 映射模型: {original_model} -> {model}")

        # Gemini 3/4 使用思考等级，不与旧式预算混传。
        if original_model.lower().startswith(("gemini-3", "gemini-4")):
            thinking_config = generation_config.setdefault("thinkingConfig", {})
            thinking_config.pop("thinkingBudget", None)
            thinking_config["includeThoughts"] = return_thoughts
        else:
            # 对于 Gemini 2.5 系列，保留 thinkingConfig
            if thinking:
                if "thinkingConfig" not in generation_config:
                    generation_config["thinkingConfig"] = {}
                thinking_config = generation_config["thinkingConfig"]
                if not thinking_config.get("thinkingLevel"):
                    thinking_config["thinkingBudget"] = 1024
                thinking_config["includeThoughts"] = return_thoughts
    else:
        # 针对非 Gemini 模型（如 Claude）
        if thinking or ("claude" in model.lower() and thinking_config.get("thinkingLevel")):
            # 只控制思考内容是否返回，不替客户端固定思考预算。
            if "thinkingConfig" not in generation_config:
                generation_config["thinkingConfig"] = {}

            thinking_config = generation_config["thinkingConfig"]
            thinking_config["includeThoughts"] = return_thoughts

        # 检查最后一个 assistant 消息是否以 thinking 块开始
        contents = result.get("contents", [])

        if "claude" in model.lower():
            # 检测是否有工具调用（MCP场景）
            has_tool_calls = any(
                isinstance(content, dict) and
                any(
                    isinstance(part, dict) and ("functionCall" in part or "function_call" in part)
                    for part in content.get("parts", [])
                )
                for content in contents
            )

            if not has_tool_calls:
                # 非 MCP 场景：填充思考块
                # 找到最后一个 model 角色的 content
                for i in range(len(contents) - 1, -1, -1):
                    content = contents[i]
                    if isinstance(content, dict) and content.get("role") == "model":
                        # 在 parts 开头插入思考块（使用官方跳过验证的虚拟签名）
                        parts = content.get("parts", [])
                        thinking_part = {
                            "text": "...",
                            "thoughtSignature": "skip_thought_signature_validator"  # 官方文档推荐的虚拟签名
                        }
                        # 如果第一个 part 不是 thinking，则插入
                        if not parts or not (isinstance(parts[0], dict) and ("thought" in parts[0] or "thoughtSignature" in parts[0])):
                            content["parts"] = [thinking_part] + parts
                            log.debug(f"[ANTIGRAVITY] 已在最后一个 assistant 消息开头插入思考块（含跳过验证签名）")
                        break

    if "claude" in model.lower():
        # 2. Claude 模型关键词映射
        # 使用关键词匹配而不是精确匹配，更灵活地处理各种变体
        original_model = model
        if "claude-opus-4-6" in model.lower():
            model = "claude-opus-4-6-thinking"
        elif "claude-sonnet-4-6-thinking" in model.lower():
            model = "claude-sonnet-4-6"

        if original_model != model:
            log.debug(f"[ANTIGRAVITY] 映射模型: {original_model} -> {model}")

    return model


def ensure_user_ending(request: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """保留 Gemini/Claude 的末尾模型消息，并补一条用户续写指令。"""
    model = model or request.get("model", "")
    if any(keyword in model.lower() for keyword in ("gemini", "claude", "opus", "sonnet")):
        contents = request.get("contents") or []
        if contents and isinstance(contents[-1], dict) and contents[-1].get("role") in ("model", "assistant"):
            request["contents"] = [*contents, {"role": "user", "parts": [{"text": "继续"}]}]
            log.debug(f"[ANTIGRAVITY] {model} 保留末尾 model 消息并追加用户续写指令")
    return request


async def normalize_antigravity_request(
    request: Dict[str, Any],
) -> Dict[str, Any]:
    """
    规范化 Antigravity 请求

    处理逻辑:
    1. 模型特性处理 (thinking config)
    2. 图片生成请求处理
    3. 参数范围限制 (temperature, maxOutputTokens)
    4. 工具清理

    Args:
        request: 原始请求字典

    Returns:
        规范化后的请求
    """
    # 导入配置函数
    from config import get_return_thoughts_to_frontend

    result = strip_chatbox_runtime_description(request)
    model = result.get("model", "")
    generation_config = (result.get("generationConfig") or {}).copy()  # 创建副本避免修改原对象
    # 记录原始请求
    log.debug(f"[ANTIGRAVITY_FIX] 原始请求 - 模型: {model}, generationConfig: {generation_config}")

    # 获取配置值
    return_thoughts = await get_return_thoughts_to_frontend()

    root_tc = result.pop("thinkingConfig", None)
    if root_tc is None:
        root_tc = result.pop("thinking_config", None)
    if root_tc is not None and "thinkingConfig" not in generation_config:
        if isinstance(root_tc, dict):
            # thinking_budget snake_case 归一为 thinkingBudget
            if "thinkingBudget" not in root_tc and "thinking_budget" in root_tc:
                root_tc["thinkingBudget"] = root_tc.pop("thinking_budget")
        generation_config["thinkingConfig"] = root_tc

    model = _normalize_antigravity_request(result, model, generation_config, return_thoughts)
    result["model"] = model

    # 移除 antigravity 模式不支持的字段
    generation_config.pop("presencePenalty", None)
    generation_config.pop("frequencyPenalty", None)
    generation_config.pop("stopSequences", None)

    # ========== 公共处理 ==========

    # 1. 安全设置覆盖
    if "tools" in result:
        result["tools"] = _normalize_tools_for_internal_api(result.get("tools"))
        # 对于 Claude 模型：Antigravity 通道需要标准 functionDeclarations/parametersJsonSchema 格式
        # 对于 Gemini 模型：统一转换为 functionDeclarations 并确保只使用 parameters 字段（移除 parametersJsonSchema 以防报错）
        result["tools"] = _ensure_empty_tool_schema_for_claude(_split_tools_single_function(result.get("tools")), model, "antigravity")
        result["tools"] = _split_tools_single_function(result["tools"])

    result["safetySettings"] = DEFAULT_SAFETY_SETTINGS

    # 2. 参数范围限制
    _clamp_temperature(generation_config)

    if "contents" in result:
        result["contents"] = _ensure_tool_call_ids(result["contents"], model)

        cleaned_contents = []
        for content in result["contents"]:
            if isinstance(content, dict) and "parts" in content:
                # 过滤掉空的或无效的 parts
                valid_parts = []
                for part in content["parts"]:
                    if not isinstance(part, dict):
                        continue

                    # 先清理文字，再判断是否为空，避免空白 user 掩盖末尾 model。
                    part = part.copy()
                    if "text" in part:
                        text_value = part["text"]
                        if isinstance(text_value, list):
                            text_parts = []
                            for t in text_value:
                                if isinstance(t, dict) and "text" in t:
                                    text_parts.append(str(t["text"]))
                                elif isinstance(t, str):
                                    text_parts.append(t)
                                elif t is not None:
                                    text_parts.append(str(t))
                            part["text"] = " ".join(text_parts)
                        elif text_value not in (None, "", {}, []):
                            part["text"] = str(text_value)
                        if isinstance(part["text"], str):
                            part["text"] = part["text"].rstrip()

                    has_valid_value = any(
                        value not in (None, "", {}, [])
                        for key, value in part.items()
                        if key != "thought" and (
                            key not in ("thoughtSignature", "thought_signature")
                            or content.get("role") in ("model", "assistant")
                        )
                    )

                    if has_valid_value:
                        if "image" not in model.lower():
                            part = _normalize_part_thought_signature(part, model)
                        part = normalize_inline_image_part(part)
                        valid_parts.append(part)
                    else:
                        log.warning(f"[ANTIGRAVITY_FIX] 移除空的或无效的 part: {part}")

                # 只添加有有效 parts 的 content
                if valid_parts:
                    cleaned_content = content.copy()
                    if cleaned_content.get("role") == "assistant":
                        cleaned_content["role"] = "model"
                    cleaned_content["parts"] = valid_parts
                    cleaned_contents.append(cleaned_content)
                else:
                    log.warning(f"[ANTIGRAVITY_FIX] 跳过没有有效 parts 的 content: {content.get('role')}")
            else:
                cleaned_contents.append(content)

        result["contents"] = cleaned_contents

    if not isinstance(result.get("systemInstruction"), dict) and isinstance(result.get("system_instruction"), dict):
        result["systemInstruction"] = result["system_instruction"]
    result.pop("system_instruction", None)
    if isinstance(result.get("systemInstruction"), dict):
        result["systemInstruction"] = _canonicalize_system_instruction(result["systemInstruction"])
    result["generationConfig"] = _normalize_generation_config(result, generation_config, model)

    # 图片请求也先清理消息，再保留图片专用参数。
    if "image" in model.lower():
        result = prepare_image_generation_request(result, model)

    # 清理空消息后再补续写指令，避免末尾空 user 被删除后重新露出 model。
    return ensure_user_ending(result)


def _split_tools_single_function(tools: Any) -> Any:
    """将 tools 拆解为官方单函数独立切片形态，保持客户端声明顺序。

    官方 HAR 抓包铁律：原生 329 次带工具报文中，独立切片占比 100%：
        "tools": [
            { "functionDeclarations": [ { "name": "tool1", ... } ] },
            { "functionDeclarations": [ { "name": "tool2", ... } ] }
        ]
    - 每个对象仅包含一个 functionDeclarations，且其中只有一个声明；
    - 参数 schema 的 type 字段统一转大写（官方全工具采用 OBJECT/STRING/INTEGER/BOOLEAN/ARRAY）；
    - 非 functionDeclarations 工具（googleSearch / codeExecution 等）严格保持就地位置。
    """
    if not isinstance(tools, list):
        return tools

    expanded_tools: List[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            expanded_tools.append(tool)
            continue

        declarations = tool.get("functionDeclarations") or tool.get("function_declarations")
        if isinstance(declarations, list):
            for declaration in declarations:
                if isinstance(declaration, dict):
                    decl_copy = dict(declaration)
                    parameters = decl_copy.get("parameters")
                    if isinstance(parameters, (dict, list)):
                        _enforce_uppercase_schema_types(parameters)
                    expanded_tools.append({"functionDeclarations": [decl_copy]})
                else:
                    expanded_tools.append({"functionDeclarations": [declaration]})
            continue

        # 非 functionDeclarations 工具（googleSearch / codeExecution）严格保持就地位置
        expanded_tools.append(tool)

    return expanded_tools

def _canonicalize_system_instruction(system_instruction: Dict[str, Any]) -> Dict[str, Any]:
    """规范化 systemInstruction 为官方形态 {role: "user", parts: [...]}，统一键序 role -> parts -> 其余。

    官方报文 systemInstruction 恒为:
        { "role": "user", "parts": [ { "text": ... }, ... ] }
    """
    if not isinstance(system_instruction, dict):
        return system_instruction

    canonical: Dict[str, Any] = {}
    role = system_instruction.get("role")
    if not isinstance(role, str) or not role:
        role = "user"
    canonical["role"] = role
    if "parts" in system_instruction:
        canonical["parts"] = system_instruction["parts"]
    for key, value in system_instruction.items():
        if key not in ("role", "parts"):
            canonical[key] = value
    return canonical

def _enforce_uppercase_schema_types(schema: Any) -> None:
    """递归将 schema 的 type 字段值转为大写（官方 HAR 全工具采用大写类型标记 OBJECT/STRING/...）。

    注意：仅转换 type 键的值，其他字段（如 enum 值、format）保持原样。
    """
    if isinstance(schema, dict):
        type_value = schema.get("type")
        if isinstance(type_value, str):
            schema["type"] = type_value.upper()
        for key in ("properties",):
            if isinstance(schema.get(key), dict):
                for prop_schema in schema[key].values():
                    _enforce_uppercase_schema_types(prop_schema)
        if isinstance(schema.get("items"), (dict, list)):
            _enforce_uppercase_schema_types(schema["items"])
    elif isinstance(schema, list):
        for item in schema:
            _enforce_uppercase_schema_types(item)

def _normalize_generation_config(
    result: Dict[str, Any],
    generation_config: Dict[str, Any],
    upstream: str,
) -> Dict[str, Any]:
    """thinkingConfig / generationConfig 归一（对齐官方拓扑）。

    - Claude + 强制工具调用（toolConfig mode=ANY）时剔除 thinkingConfig，
      杜绝 Anthropic "Thinking may not be enabled when tool_choice forces tool use" 400；
    - Claude thinkingBudget 下限保护（Anthropic 规范 >= 1024）；
    - 不伪造 topK / topP（官方报文不携带，保留客户端显式值）。
    """
    is_claude = "claude" in (upstream or "").lower()

    if is_claude:
        tool_config = result.get("toolConfig")
        forced_tool_choice = (
            isinstance(tool_config, dict)
            and str(
                (tool_config.get("functionCallingConfig") or {}).get("mode", "")
            ).upper() == "ANY"
        )
        if forced_tool_choice:
            if generation_config.pop("thinkingConfig", None) is not None:
                print(
                    "[ANTIGRAVITY] Claude 目标带强制工具调用 (mode=ANY)，剔除 thinkingConfig "
                    "防止上游 400 (thinking 与 tool_choice 冲突)",
                    flush=True,
                )

        thinking_config = generation_config.get("thinkingConfig")
        if isinstance(thinking_config, dict):
            budget = thinking_config.get("thinkingBudget")
            if isinstance(budget, int) and not isinstance(budget, bool) and 0 < budget < CLAUDE_MIN_THINKING_BUDGET:
                print(
                    f"[ANTIGRAVITY] Claude thinkingBudget {budget} 低于 Anthropic 规范下限，"
                    f"钳制为 {CLAUDE_MIN_THINKING_BUDGET}",
                    flush=True,
                )
                thinking_config["thinkingBudget"] = CLAUDE_MIN_THINKING_BUDGET

    # 官方标配 maxOutputTokens（在 thinkingConfig 剔除/钳制之后补齐）：
    # Claude 64000（严禁超 64k），Gemini 家族 65536
    default_max_output = CLAUDE_MAX_OUTPUT_TOKENS if is_claude else GEMINI_MAX_OUTPUT_TOKENS
    if generation_config.get("maxOutputTokens") in (None, 65536, 64000):
        generation_config["maxOutputTokens"] = default_max_output

    # generationConfig 内部键序: candidateCount -> maxOutputTokens -> thinkingConfig -> temperature -> topP -> topK -> 其余
    ordered_keys = [
        "candidateCount",
        "maxOutputTokens",
        "thinkingConfig",
        "temperature",
        "topP",
        "topK",
    ]
    reordered: Dict[str, Any] = {}
    for key in ordered_keys:
        if key in generation_config:
            reordered[key] = generation_config.pop(key)
    for key, value in generation_config.items():
        if key not in reordered:
            reordered[key] = value
    return reordered
