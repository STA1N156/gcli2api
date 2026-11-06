"""
OpenAI Router - Handles OpenAI format API requests via Antigravity
通过Antigravity处理OpenAI格式请求的路由模块
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 标准库
import json

# 第三方库
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

# 本地模块 - 配置和日志
from log import log
from src.converter.image_input import ImageInputError

# 本地模块 - 工具和认证
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_bearer,
)

# 本地模块 - 基础路由工具
from src.router.hi_check import is_health_check_request, create_health_check_response
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
)

# 本地模块 - 数据模型
from src.router.openai_request import parse_openai_chat_request
from src.session_affinity import extract_cache_session_key

# 本地模块 - 任务管理
from src.task_manager import create_managed_task


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

@router.post("/antigravity/v1/chat/completions")
async def chat_completions(
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """
    处理OpenAI格式的聊天完成请求（流式和非流式）

    Args:
        openai_request: OpenAI格式的请求体
        token: Bearer认证令牌
    """
    normalized_dict = await parse_openai_chat_request(request)
    model_name = normalized_dict["model"]

    log.debug(f"[ANTIGRAVITY-OPENAI] Request for model: {model_name}")
    cache_session_key = extract_cache_session_key(normalized_dict, request.headers)

    # 健康检查
    if is_health_check_request(normalized_dict, format="openai"):
        response = create_health_check_response(format="openai")
        return JSONResponse(content=response)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model_name)
    real_model = get_base_model_from_feature_model(model_name)

    # 获取流式标志
    is_streaming = bool(normalized_dict.get("stream", False))

    # 对于抗截断模型的非流式请求，后端会使用工具正文抗截断并收集为非流式响应
    if use_anti_truncation and not is_streaming:
        log.info("非流式请求启用抗截断，将收集工具正文后返回完整响应")

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 转换为 Gemini 格式 (使用 converter)
    from src.converter.openai2gemini import convert_openai_to_gemini_request
    try:
        gemini_dict = await convert_openai_to_gemini_request(normalized_dict)
    except ImageInputError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image input: {exc}") from exc

    # convert_openai_to_gemini_request 不包含 model 字段，需要手动添加
    gemini_dict["model"] = real_model

    # 规范化 Gemini 请求 (使用 antigravity 模式)
    from src.converter.antigravity_fix import normalize_antigravity_request
    try:
        gemini_dict = await normalize_antigravity_request(gemini_dict)
    except ImageInputError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image input: {exc}") from exc

    # 准备API请求格式 - 提取model并将其他字段放入request中
    api_request = {
        "model": gemini_dict.pop("model"),
        "request": gemini_dict,
        "cache_session_key": cache_session_key
    }

    # ========== 非流式请求 ==========
    if not is_streaming:
        if use_anti_truncation:
            from src.api.antigravity import stream_request
            from src.router.anti_truncation import collect_anti_truncation_response

            response = await collect_anti_truncation_response(api_request, stream_request)
        else:
            # 调用 API 层的非流式请求
            from src.api.antigravity import non_stream_request
            response = await non_stream_request(body=api_request)

        # 检查响应状态码
        status_code = getattr(response, "status_code", 200)

        # 提取响应体
        if hasattr(response, "body"):
            response_body = response.body.decode() if isinstance(response.body, bytes) else response.body
        elif hasattr(response, "content"):
            response_body = response.content.decode() if isinstance(response.content, bytes) else response.content
        else:
            response_body = str(response)

        try:
            gemini_response = json.loads(response_body)
        except Exception as e:
            log.error(f"Failed to parse Gemini response: {e}")
            raise HTTPException(status_code=500, detail="Response parsing failed")

        # 转换为 OpenAI 格式
        from src.converter.openai2gemini import convert_gemini_to_openai_response
        openai_response = convert_gemini_to_openai_response(
            gemini_response,
            real_model,
            status_code
        )

        return JSONResponse(content=openai_response, status_code=status_code)

    # ========== 流式请求 ==========

    # 流式和非流式抗截断共用工具正文转换。
    async def stream_generator():
        from src.api.antigravity import stream_request
        from fastapi import Response
        import uuid

        # 调用 API 层的流式请求（不使用 native 模式）
        if use_anti_truncation:
            from src.router.anti_truncation import anti_truncation_gemini_stream

            stream_gen = anti_truncation_gemini_stream(api_request, stream_request)
        else:
            stream_gen = stream_request(body=api_request, native=False)
        try:
            first_chunk = await read_first_async_item(stream_gen)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        response_id = str(uuid.uuid4())
        model_state = {}

        # yield所有数据,处理可能的错误Response
        async for chunk in prepend_async_item(first_chunk, stream_gen):
            # 检查是否是Response对象（错误情况）
            if isinstance(chunk, Response):
                # 将Response转换为SSE格式的错误消息
                try:
                    error_content = chunk.body if isinstance(chunk.body, bytes) else (chunk.body or b'').encode('utf-8')
                    gemini_error = json.loads(error_content.decode('utf-8'))
                    # 转换为 OpenAI 格式错误
                    from src.converter.openai2gemini import convert_gemini_to_openai_response
                    openai_error = convert_gemini_to_openai_response(
                        gemini_error,
                        real_model,
                        chunk.status_code
                    )
                    yield f"data: {json.dumps(openai_error)}\n\n".encode('utf-8')
                except Exception:
                    yield f"data: {json.dumps({'error': 'Stream error'})}\n\n".encode('utf-8')
                yield b"data: [DONE]\n\n"
                return
            else:
                # 正常的bytes数据，转换为 OpenAI 格式
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # 跳过空行
                if not chunk_str.strip():
                    continue

                # 处理 [DONE] 标记
                if chunk_str.strip() == "data: [DONE]":
                    yield "data: [DONE]\n\n".encode('utf-8')
                    return

                # 解析并转换 Gemini chunk 为 OpenAI 格式
                if chunk_str.startswith("data: "):
                    try:
                        # 转换为 OpenAI 格式
                        from src.converter.openai2gemini import convert_gemini_to_openai_stream
                        openai_chunk_str = convert_gemini_to_openai_stream(
                            chunk_str,
                            real_model,
                            response_id,
                            model_state=model_state,
                        )

                        if openai_chunk_str:
                            yield openai_chunk_str.encode('utf-8')

                    except Exception as e:
                        log.error(f"Failed to convert chunk: {e}")
                        continue

        # 发送结束标记
        yield "data: [DONE]\n\n".encode('utf-8')

    return await build_streaming_response_or_error(stream_generator())


# ==================== 测试代码 ====================
