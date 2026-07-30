"""
Base API Client - 共用的 API 客户端基础功能
提供错误记录和重试配置等共同功能
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Response

from config import (
    get_credential_retry_interval,
    get_credential_retry_limit_enabled,
    get_empty_output_error_enabled,
    get_max_retry_credentials,
)
from log import log
from src.api.empty_output import build_empty_model_output_response, is_empty_model_output_payload
from src.credential_manager import CredentialManager


RETRYABLE_STATUS_CODES = {401, 402, 403, 404, 408, 429, 500, 502, 503, 504}


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


# ==================== 重试配置获取 ====================

async def get_retry_config() -> Dict[str, Any]:
    """未开启限制时遍历全部可用凭证；开启后限制总尝试凭证数。"""
    limit_enabled = await get_credential_retry_limit_enabled()
    return {
        "max_credentials": await get_max_retry_credentials()
        if limit_enabled
        else 0,
        "retry_interval": await get_credential_retry_interval(),
    }


def retry_limit_reached(retry_config: Dict[str, Any], attempted_count: int) -> bool:
    maximum = int(retry_config.get("max_credentials", 0) or 0)
    return maximum > 0 and attempted_count >= maximum


# ==================== API调用结果记录 ====================

async def record_api_call_success(
    credential_manager: CredentialManager,
    credential_name: str,
    mode: str = "geminicli",
    model_name: Optional[str] = None
) -> None:
    """
    记录API调用成功
    
    Args:
        credential_manager: 凭证管理器实例
        credential_name: 凭证名称
        mode: 模式（geminicli 或 antigravity）
        model_name: 模型名称（用于模型级CD）
    """
    if credential_manager and credential_name:
        await credential_manager.record_api_call_result(
            credential_name, True, mode=mode, model_name=model_name
        )


async def record_api_call_error(
    credential_manager: CredentialManager,
    credential_name: str,
    status_code: int,
    cooldown_until: Optional[float] = None,
    mode: str = "geminicli",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None
) -> None:
    """
    记录API调用错误

    Args:
        credential_manager: 凭证管理器实例
        credential_name: 凭证名称
        status_code: HTTP状态码
        cooldown_until: 冷却截止时间（Unix时间戳）
        mode: 模式（geminicli 或 antigravity）
        model_name: 模型名称（用于模型级CD）
        error_message: 错误信息（可选）
    """
    if credential_manager and credential_name:
        if cooldown_until is None and model_name:
            if status_code in (429, 503) and error_message:
                cooldown_until = await parse_and_log_cooldown(
                    error_message, mode=mode
                )
            if cooldown_until is None:
                cooldown_seconds = {
                    401: 24 * 60 * 60,
                    402: 30 * 60,
                    403: 30 * 60,
                    404: 12 * 60 * 60,
                    408: 60,
                    429: 5,
                    500: 5,
                    502: 60,
                    503: 5,
                    504: 60,
                }.get(status_code)
                if cooldown_seconds:
                    cooldown_until = time.time() + cooldown_seconds

        await credential_manager.record_api_call_result(
            credential_name,
            False,
            status_code,
            cooldown_until=cooldown_until,
            mode=mode,
            model_name=model_name,
            error_message=error_message
        )


# ==================== 429错误处理 ====================

async def parse_and_log_cooldown(
    error_text: str,
    mode: str = "geminicli"
) -> Optional[float]:
    """
    解析并记录冷却时间

    Args:
        error_text: 错误响应文本
        mode: 模式（geminicli 或 antigravity）

    Returns:
        冷却截止时间（Unix时间戳），如果解析失败则返回None
    """
    try:
        error_data = json.loads(error_text)
        cooldown_until = parse_quota_reset_timestamp(error_data, mode=mode)
        if cooldown_until:
            log.info(
                f"[{mode.upper()}] 检测到quota冷却时间: "
                f"{datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
            )
            return cooldown_until
    except Exception as parse_err:
        log.debug(f"[{mode.upper()}] Failed to parse cooldown time: {parse_err}")
    return None


# ==================== 流式响应收集 ====================

async def collect_streaming_response(stream_generator) -> Response:
    """
    将Gemini流式响应收集为一条完整的非流式响应

    Args:
        stream_generator: 流式响应生成器，产生 "data: {json}" 格式的行或Response对象

    Returns:
        Response: 合并后的完整响应对象

    Example:
        >>> async for line in stream_generator:
        ...     # line format: "data: {...}" or Response object
        >>> response = await collect_streaming_response(stream_generator)
    """
    # 初始化响应结构
    merged_response = {
        "response": {
            "candidates": [{
                "content": {
                    "parts": [],
                    "role": "model"
                },
                "finishReason": None,
                "safetyRatings": [],
                "citationMetadata": None
            }],
            "usageMetadata": {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "totalTokenCount": 0
            }
        }
    }

    collected_text = []  # 用于收集文本内容
    collected_thought_text = []  # 用于收集思维链内容
    collected_other_parts = []  # 用于收集其他类型的parts（图片、文件、工具调用等）
    collected_tool_parts_count = 0  # 记录工具调用相关part数量
    has_data = False
    line_count = 0
    debug_enabled = log.is_debug_enabled()
    empty_output_error_enabled = await get_empty_output_error_enabled()

    if debug_enabled:
        log.debug("[STREAM COLLECTOR] Starting to collect streaming response")

    try:
        async for line in stream_generator:
            line_count += 1

            # 如果收到的是Response对象（错误），直接返回
            if isinstance(line, Response):
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] 收到错误Response，状态码: {line.status_code}")
                return line

            # 处理 bytes 类型
            if isinstance(line, bytes):
                line_str = line.decode('utf-8', errors='ignore')
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Processing bytes line {line_count}: {line_str[:200] if line_str else 'empty'}")
            elif isinstance(line, str):
                line_str = line
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Processing line {line_count}: {line_str[:200] if line_str else 'empty'}")
            else:
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Skipping non-string/bytes line: {type(line)}")
                continue

            # 解析流式数据行
            if not line_str.startswith("data: "):
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Skipping line without 'data: ' prefix: {line_str[:100]}")
                continue

            raw = line_str[6:].strip()
            if raw == "[DONE]":
                if debug_enabled:
                    log.debug("[STREAM COLLECTOR] Received [DONE] marker")
                break

            try:
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Parsing JSON: {raw[:200]}")
                chunk = json.loads(raw)
                has_data = True
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Chunk keys: {chunk.keys() if isinstance(chunk, dict) else type(chunk)}")

                # 提取响应对象
                response_obj = chunk.get("response", {})
                if not response_obj:
                    if debug_enabled:
                        log.debug("[STREAM COLLECTOR] No 'response' key in chunk, trying direct access")
                    response_obj = chunk  # 尝试直接使用chunk

                candidates = response_obj.get("candidates", [])
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Found {len(candidates)} candidates")
                if not candidates:
                    if debug_enabled:
                        log.debug(f"[STREAM COLLECTOR] No candidates in chunk, chunk structure: {list(chunk.keys()) if isinstance(chunk, dict) else type(chunk)}")
                    continue

                candidate = candidates[0]

                # 收集文本内容
                content = candidate.get("content", {})
                parts = content.get("parts", [])
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Processing {len(parts)} parts from candidate")

                for part in parts:
                    if not isinstance(part, dict):
                        continue

                    # 优先保留工具调用相关 part（functionCall / functionResponse）
                    # 避免在 stream2nostream 模式下工具调用丢失
                    if "functionCall" in part or "functionResponse" in part or "function_call" in part:
                        collected_other_parts.append(part)
                        collected_tool_parts_count += 1
                        if debug_enabled:
                            log.debug(f"[STREAM COLLECTOR] Collected tool part: {list(part.keys())}")
                        continue

                    # 处理文本内容
                    text = part.get("text", "")
                    if text:
                        # 区分普通文本和思维链
                        if part.get("thought", False):
                            collected_thought_text.append(text)
                            if debug_enabled:
                                log.debug(f"[STREAM COLLECTOR] Collected thought text: {text[:100]}")
                        else:
                            collected_text.append(text)
                            if debug_enabled:
                                log.debug(f"[STREAM COLLECTOR] Collected regular text: {text[:100]}")
                    # 处理非文本内容（图片、文件等）
                    elif "inlineData" in part or "fileData" in part or "executableCode" in part or "codeExecutionResult" in part:
                        collected_other_parts.append(part)
                        if debug_enabled:
                            log.debug(f"[STREAM COLLECTOR] Collected non-text part: {list(part.keys())}")

                # 收集其他信息（使用最后一个块的值）
                if candidate.get("finishReason"):
                    merged_response["response"]["candidates"][0]["finishReason"] = candidate["finishReason"]

                if candidate.get("safetyRatings"):
                    merged_response["response"]["candidates"][0]["safetyRatings"] = candidate["safetyRatings"]

                if candidate.get("citationMetadata"):
                    merged_response["response"]["candidates"][0]["citationMetadata"] = candidate["citationMetadata"]

                # 更新使用元数据
                usage = response_obj.get("usageMetadata", {})
                if usage:
                    merged_response["response"]["usageMetadata"].update(usage)

            except json.JSONDecodeError as e:
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Failed to parse JSON chunk: {e}")
                continue
            except Exception as e:
                if debug_enabled:
                    log.debug(f"[STREAM COLLECTOR] Error processing chunk: {e}")
                continue

    except Exception as e:
        log.error(f"[STREAM COLLECTOR] Error collecting stream after {line_count} lines: {e}")
        return Response(
            content=json.dumps({"error": f"收集流式响应失败: {str(e)}"}),
            status_code=500,
            media_type="application/json"
        )

    if debug_enabled:
        log.debug(f"[STREAM COLLECTOR] Finished iteration, has_data={has_data}, line_count={line_count}")

    # 如果没有收集到任何数据，返回错误
    if not has_data:
        if empty_output_error_enabled:
            log.error(f"[STREAM COLLECTOR] No data collected from stream after {line_count} lines")
            return build_empty_model_output_response()

    # 组装最终的parts
    final_parts = []

    # 先添加思维链内容（如果有）
    if collected_thought_text:
        final_parts.append({
            "text": "".join(collected_thought_text),
            "thought": True
        })

    # 再添加普通文本内容
    if collected_text:
        final_parts.append({
            "text": "".join(collected_text)
        })

    # 添加其他类型的parts（图片、文件等）
    final_parts.extend(collected_other_parts)

    # 如果没有任何内容，添加空文本
    if not final_parts:
        final_parts.append({"text": ""})

    merged_response["response"]["candidates"][0]["content"]["parts"] = final_parts

    if debug_enabled:
        log.debug(
            f"[STREAM COLLECTOR] Collected {len(collected_text)} text chunks, "
            f"{len(collected_thought_text)} thought chunks, {len(collected_other_parts)} other parts "
            f"(tool parts: {collected_tool_parts_count})"
        )

    # 去掉嵌套的 "response" 包装（Antigravity格式 -> 标准Gemini格式）
    if "response" in merged_response and "candidates" not in merged_response:
        if debug_enabled:
            log.debug("[STREAM COLLECTOR] 展开response包装")
        merged_response = merged_response["response"]

    # 返回纯JSON格式
    if is_empty_model_output_payload(merged_response):
        if empty_output_error_enabled:
            log.warning("[STREAM COLLECTOR] Collected stream contains empty model output")
            return build_empty_model_output_response()

    return Response(
        content=json.dumps(merged_response, ensure_ascii=False).encode('utf-8'),
        status_code=200,
        headers={},
        media_type="application/json"
    )


RESOURCE_EXHAUSTED_COOLDOWN_HOURS = 4  # RESOURCE_EXHAUSTED 错误的默认冷却时间（小时）


def parse_quota_reset_timestamp(
    error_response: dict,
    mode: str = "geminicli",
) -> Optional[float]:
    """
    从Google API错误响应中提取quota重置时间戳

    Args:
        error_response: Google API返回的错误响应字典
        mode: 请求模式

    Returns:
        Unix时间戳（秒），如果无法解析则返回None

    示例错误响应:
    {
      "error": {
        "code": 429,
        "message": "You have exhausted your capacity...",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
          {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "QUOTA_EXHAUSTED",
            "metadata": {
              "quotaResetTimeStamp": "2025-11-30T14:57:24Z",
              "quotaResetDelay": "13h19m1.20964964s"
            }
          }
        ]
      }
    }
    """
    try:
        error_obj = error_response.get("error", {})
        details = error_obj.get("details", [])
        is_generic_resource_exhausted = (
            error_obj.get("status") == "RESOURCE_EXHAUSTED"
            and error_obj.get("message") == "Resource has been exhausted (e.g. check quota)."
        )

        for detail in details:
            if detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo":
                reset_timestamp_str = detail.get("metadata", {}).get("quotaResetTimeStamp")

                if reset_timestamp_str:
                    if reset_timestamp_str.endswith("Z"):
                        reset_timestamp_str = reset_timestamp_str.replace("Z", "+00:00")

                    reset_dt = datetime.fromisoformat(reset_timestamp_str)
                    if reset_dt.tzinfo is None:
                        reset_dt = reset_dt.replace(tzinfo=timezone.utc)

                    return reset_dt.astimezone(timezone.utc).timestamp()

        # 如果是 RESOURCE_EXHAUSTED 错误且消息完全匹配，设置默认4小时冷却时间
        if is_generic_resource_exhausted:
            import time
            cooldown_until = time.time() + RESOURCE_EXHAUSTED_COOLDOWN_HOURS * 3600
            return cooldown_until

        return None

    except Exception:
        return None
