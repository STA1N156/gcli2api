"""
Gemini Router - Handles native Gemini format API requests (Antigravity backend)
处理原生Gemini格式请求的路由模块（Antigravity后端）
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
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

# 本地模块 - 配置和日志
from log import log
from src.converter.image_input import ImageInputError

# 本地模块 - 工具和认证
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_gemini_flexible,
)

# 本地模块 - 基础路由工具
from src.router.hi_check import is_health_check_request, create_health_check_response
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
    unwrap_gemini_response_sse_chunk,
)
from src.router.gemini_rest_request import GeminiRestRequestError, normalize_gemini_rest_request

# 本地模块 - 数据模型
from src.session_affinity import extract_cache_session_key

# 本地模块 - 任务管理
from src.task_manager import create_managed_task


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

@router.post("/antigravity/v1beta/models/{model:path}:generateContent")
@router.post("/antigravity/v1/models/{model:path}:generateContent")
async def generate_content(
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    处理Gemini格式的内容生成请求（非流式）

    Args:
        gemini_request: Gemini格式的请求体
        model: 模型名称
        api_key: API 密钥
    """
    log.debug(f"[ANTIGRAVITY] Non-streaming request for model: {model}")

    # 转换为字典
    try:
        normalized_dict = normalize_gemini_rest_request(await request.json())
    except GeminiRestRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    cache_session_key = extract_cache_session_key(normalized_dict, request.headers)

    # 健康检查
    if is_health_check_request(normalized_dict, format="gemini"):
        response = create_health_check_response(format="gemini")
        return JSONResponse(content=response)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # 对于抗截断模型的非流式请求，后端会使用工具正文抗截断并收集为非流式响应
    if use_anti_truncation:
        log.info("非流式请求启用抗截断，将收集工具正文后返回完整响应")

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 规范化 Gemini 请求 (使用 antigravity 模式)
    from src.converter.antigravity_fix import normalize_antigravity_request
    try:
        normalized_dict = await normalize_antigravity_request(normalized_dict)
    except ImageInputError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image input: {exc}") from exc

    # 准备API请求格式 - 提取model并将其他字段放入request中
    api_request = {
        "model": normalized_dict.pop("model"),
        "request": normalized_dict,
        "cache_session_key": cache_session_key
    }

    if use_anti_truncation:
        from src.api.antigravity import stream_request
        from src.router.anti_truncation import collect_anti_truncation_response

        response = await collect_anti_truncation_response(api_request, stream_request)
    else:
        # 调用 API 层的非流式请求
        from src.api.antigravity import non_stream_request
        response = await non_stream_request(body=api_request)

    # 解包装响应：Antigravity API 可能返回的格式有额外的 response 包装层
    # 需要提取并返回标准 Gemini 格式
    # 保持 Gemini 原生的 inlineData 格式,不进行 Markdown 转换
    try:
        if response.status_code == 200:
            response_data = json.loads(response.body if hasattr(response, 'body') else response.content)
            # 如果有 response 包装，解包装它
            if "response" in response_data:
                unwrapped_data = response_data["response"]
                return JSONResponse(content=unwrapped_data)
        # 错误响应或没有 response 字段，直接返回
        return response
    except Exception as e:
        log.warning(f"Failed to unwrap response: {e}, returning original response")
        return response

@router.post("/antigravity/v1beta/models/{model:path}:streamGenerateContent")
@router.post("/antigravity/v1/models/{model:path}:streamGenerateContent")
async def stream_generate_content(
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    处理Gemini格式的流式内容生成请求

    Args:
        gemini_request: Gemini格式的请求体
        model: 模型名称
        api_key: API 密钥
    """
    log.debug(f"[ANTIGRAVITY] Streaming request for model: {model}")

    # 转换为字典
    try:
        normalized_dict = normalize_gemini_rest_request(await request.json())
    except GeminiRestRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    cache_session_key = extract_cache_session_key(normalized_dict, request.headers)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 流式和非流式抗截断共用工具正文转换。
    async def stream_generator():
        from src.converter.antigravity_fix import normalize_antigravity_request
        from src.api.antigravity import stream_request
        from fastapi import Response

        normalized_req = await normalize_antigravity_request(normalized_dict.copy())

        # 准备API请求格式 - 提取model并将其他字段放入request中
        api_request = {
            "model": normalized_req.pop("model"),
            "request": normalized_req,
            "cache_session_key": cache_session_key
        }

        # 普通流式请求只在确实有 response 包装时才拆 JSON
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

        # 只在确实有 response 包装时才拆 JSON
        async for chunk in prepend_async_item(first_chunk, stream_gen):
            # 检查是否是Response对象（错误情况）
            if isinstance(chunk, Response):
                # 将Response转换为SSE格式的错误消息
                try:
                    error_content = chunk.body if isinstance(chunk.body, bytes) else (chunk.body or b'').encode('utf-8')
                    error_json = json.loads(error_content.decode('utf-8'))
                except Exception:
                    error_json = {"error": {"code": chunk.status_code, "message": "upstream error", "status": "ERROR"}}
                log.error(f"[ANTIGRAVITY STREAM] 返回错误给客户端: status={chunk.status_code}, error={str(error_json)[:200]}")
                yield f"data: {json.dumps(error_json)}\n\n".encode('utf-8')
                yield b"data: [DONE]\n\n"
                return

            yield unwrap_gemini_response_sse_chunk(chunk)

    return await build_streaming_response_or_error(stream_generator())

@router.post("/antigravity/v1beta/models/{model:path}:countTokens")
@router.post("/antigravity/v1/models/{model:path}:countTokens")
async def count_tokens(
    request: Request = None,
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    模拟Gemini格式的token计数

    使用简单的启发式方法：大约4字符=1token
    """

    try:
        request_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # 简单的token计数模拟 - 基于文本长度估算
    total_tokens = 0

    # 如果有contents字段
    if "contents" in request_data:
        for content in request_data["contents"]:
            if "parts" in content:
                for part in content["parts"]:
                    if "text" in part:
                        # 简单估算：大约4字符=1token
                        text_length = len(part["text"])
                        total_tokens += max(1, text_length // 4)

    # 如果有generateContentRequest字段
    elif "generateContentRequest" in request_data:
        gen_request = request_data["generateContentRequest"]
        if "contents" in gen_request:
            for content in gen_request["contents"]:
                if "parts" in content:
                    for part in content["parts"]:
                        if "text" in part:
                            text_length = len(part["text"])
                            total_tokens += max(1, text_length // 4)

    # 返回Gemini格式的响应
    return JSONResponse(content={"totalTokens": total_tokens})

# ==================== 测试代码 ====================
