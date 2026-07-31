"""Google Antigravity API client."""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import Response

from config import (
    get_antigravity_api_url,
    get_antigravity_stream2nostream,
    get_empty_output_error_enabled,
)
from log import log
from src.api.empty_output import (
    build_empty_model_output_response,
    is_empty_model_output,
    stream_chunk_has_visible_output,
)
from src.api.utils import (
    collect_streaming_response,
    get_retry_config,
    is_retryable_status,
    record_api_call_error,
    record_api_call_success,
    retry_limit_reached,
)
from src.credential_manager import credential_manager
from src.httpx_client import post_async, stream_post_async
from src.models import Model, model_to_dict
from src.session_affinity import extract_cache_session_key
from src.utils import ANTIGRAVITY_USER_AGENT


def _extract_first_user_text(request_payload: Dict[str, Any]) -> str:
    contents = request_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "user":
            continue
        for part in content.get("parts", []):
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    return ""


def _generate_stable_session_id(request_payload: Dict[str, Any]) -> str:
    first_user_text = _extract_first_user_text(request_payload)
    if first_user_text:
        digest = hashlib.sha256(first_user_text.encode("utf-8")).digest()
        return f"-{int.from_bytes(digest[:8], 'big') & 0x7FFFFFFFFFFFFFFF}"
    return f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"


def _generate_request_id(model: str = "") -> str:
    if "image" in model.lower():
        return f"image_gen/{int(time.time() * 1000)}/{uuid.uuid4()}/12"
    return f"agent-{uuid.uuid4()}"


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
) -> Tuple[Dict[str, Any], str]:
    inner = dict(gemini_request)
    inner.pop("safetySettings", None)
    inner["sessionId"] = _generate_stable_session_id(inner)
    system_instruction = inner.get("systemInstruction")
    if isinstance(system_instruction, dict):
        system_instruction = dict(system_instruction)
        system_instruction["role"] = "user"
        inner["systemInstruction"] = system_instruction

    if "claude" in model.lower():
        tool_config = inner.get("toolConfig") or {}
        function_config = tool_config.get("functionCallingConfig") or {}
        function_config["mode"] = "VALIDATED"
        tool_config["functionCallingConfig"] = function_config
        inner["toolConfig"] = tool_config
    else:
        generation_config = inner.get("generationConfig")
        if isinstance(generation_config, dict):
            generation_config = dict(generation_config)
            generation_config.pop("maxOutputTokens", None)
            inner["generationConfig"] = generation_config

    request_id = _generate_request_id(model)
    request_type = "image_gen" if "image" in model.lower() else "agent"
    return (
        {
            "project": project_id,
            "requestId": request_id,
            "request": inner,
            "model": model,
            "userAgent": "antigravity",
            "requestType": request_type,
        },
        request_id,
    )


def build_antigravity_headers(access_token: str) -> Dict[str, str]:
    return {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


async def _get_valid_request_credential(
    model_name: str,
    session_key: Optional[str],
    attempted: Set[str],
) -> Optional[Tuple[str, str, str]]:
    """选择未尝试且可用于当前模型的凭证。"""
    excluded = set(attempted)
    while True:
        result = await credential_manager.get_valid_credential(
            mode="antigravity",
            model_name=model_name,
            session_key=session_key,
            exclude_credentials=set(excluded),
        )
        if not result:
            return None
        filename, credential_data = result
        filename = Path(filename).name
        excluded.add(filename)
        token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id")
        if token and project_id:
            attempted.add(filename)
            return filename, token, project_id
        log.warning(f"[ANTIGRAVITY] 禁用缺少令牌或项目 ID 的凭证: {filename}")
        await credential_manager.set_cred_disabled(
            filename, True, mode="antigravity"
        )


def _response_from_httpx(response) -> Response:
    headers = dict(response.headers)
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
    )


async def _record_response_error(
    filename: str,
    model_name: str,
    status_code: int,
    error_text: str,
) -> None:
    await record_api_call_error(
        credential_manager,
        filename,
        status_code,
        mode="antigravity",
        model_name=model_name,
        error_message=error_text,
    )


async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
):
    """流式请求；失败时只选择本次尚未尝试的可用凭证。"""
    model_name = body.get("model", "")
    session_key = extract_cache_session_key(body, headers)
    attempted: Set[str] = set()
    selected = await _get_valid_request_credential(
        model_name, session_key, attempted
    )
    if not selected:
        yield Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json",
        )
        return

    current_file, token, project_id = selected
    api_url = await get_antigravity_api_url()
    target_url = f"{api_url}/v1internal:streamGenerateContent?alt=sse"
    auth_headers = build_antigravity_headers(token)
    if headers:
        auth_headers.update(headers)
    auth_headers["Authorization"] = f"Bearer {token}"
    inner_request = body.get("request", body)
    final_payload, _ = await wrap_cli_request(
        inner_request, model_name, project_id
    )

    retry_config = await get_retry_config()
    empty_output_error_enabled = await get_empty_output_error_enabled()
    last_error: Optional[Response] = None
    refreshed_after_401: Set[str] = set()

    while True:
        success_recorded = False
        retry_current = False
        retry_same = False
        buffered_chunks = []
        try:
            async for chunk in stream_post_async(
                url=target_url,
                body=final_payload,
                native=native,
                headers=auth_headers,
            ):
                if isinstance(chunk, Response):
                    error_text = (
                        chunk.body.decode("utf-8", errors="replace")
                        if isinstance(chunk.body, bytes)
                        else str(chunk.body)
                    )
                    log.warning(
                        "[ANTIGRAVITY STREAM] 上游返回 "
                        f"{chunk.status_code}: credential={current_file}, "
                        f"model={model_name}, "
                        f"request_id={final_payload.get('requestId', '')}"
                    )
                    if (
                        chunk.status_code == 401
                        and current_file not in refreshed_after_401
                    ):
                        refreshed_after_401.add(current_file)
                        refreshed = await credential_manager.refresh_credential(
                            current_file, mode="antigravity"
                        )
                        if refreshed:
                            refreshed_token = (
                                refreshed.get("access_token")
                                or refreshed.get("token")
                            )
                            refreshed_project = refreshed.get("project_id")
                            if refreshed_token and refreshed_project:
                                auth_headers["Authorization"] = (
                                    f"Bearer {refreshed_token}"
                                )
                                final_payload["project"] = refreshed_project
                                final_payload["requestId"] = _generate_request_id(
                                    model_name
                                )
                                retry_same = True
                                break
                    await _record_response_error(
                        current_file, model_name, chunk.status_code, error_text
                    )
                    if success_recorded or not is_retryable_status(chunk.status_code):
                        yield chunk
                        return
                    last_error = chunk
                    retry_current = True
                    break

                if not success_recorded:
                    if empty_output_error_enabled:
                        buffered_chunks.append(chunk)
                        if not stream_chunk_has_visible_output(chunk):
                            continue
                    await record_api_call_success(
                        credential_manager,
                        current_file,
                        mode="antigravity",
                        model_name=model_name,
                    )
                    success_recorded = True
                    if empty_output_error_enabled:
                        for buffered_chunk in buffered_chunks:
                            yield buffered_chunk
                        buffered_chunks.clear()
                    else:
                        yield chunk
                else:
                    yield chunk

            if success_recorded:
                return
            if retry_same:
                continue
            if not retry_current:
                if empty_output_error_enabled:
                    await record_api_call_error(
                        credential_manager,
                        current_file,
                        461,
                        mode="antigravity",
                        model_name=model_name,
                        error_message="模型输出为空，请检查是否含有敏感内容",
                    )
                    yield build_empty_model_output_response()
                return
        except Exception as exc:
            log.warning(
                f"[ANTIGRAVITY STREAM] 请求异常，切换凭证: {current_file}: {exc}"
            )

        if retry_limit_reached(retry_config, len(attempted)):
            yield last_error or Response(
                content=json.dumps({"error": "请求失败，已达到凭证尝试上限"}),
                status_code=500,
                media_type="application/json",
            )
            return
        if retry_config["retry_interval"] > 0:
            await asyncio.sleep(retry_config["retry_interval"])
        selected = await _get_valid_request_credential(
            model_name, session_key, attempted
        )
        if not selected:
            yield last_error or Response(
                content=json.dumps({"error": "没有更多可用凭证"}),
                status_code=500,
                media_type="application/json",
            )
            return
        current_file, token, project_id = selected
        auth_headers["Authorization"] = f"Bearer {token}"
        final_payload["project"] = project_id
        final_payload["requestId"] = _generate_request_id(model_name)


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    if await get_antigravity_stream2nostream():
        return await collect_streaming_response(
            stream_request(body=body, native=False, headers=headers)
        )

    model_name = body.get("model", "")
    session_key = extract_cache_session_key(body, headers)
    attempted: Set[str] = set()
    selected = await _get_valid_request_credential(
        model_name, session_key, attempted
    )
    if not selected:
        return Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json",
        )

    current_file, token, project_id = selected
    api_url = await get_antigravity_api_url()
    target_url = f"{api_url}/v1internal:generateContent"
    auth_headers = build_antigravity_headers(token)
    if headers:
        auth_headers.update(headers)
    auth_headers["Authorization"] = f"Bearer {token}"
    inner_request = body.get("request", body)
    final_payload, _ = await wrap_cli_request(
        inner_request, model_name, project_id
    )

    retry_config = await get_retry_config()
    empty_output_error_enabled = await get_empty_output_error_enabled()
    last_error: Optional[Response] = None
    refreshed_after_401: Set[str] = set()

    while True:
        try:
            response = await post_async(
                url=target_url,
                json=final_payload,
                headers=auth_headers,
                timeout=300.0,
            )
            if response.status_code == 200:
                if empty_output_error_enabled and is_empty_model_output(
                    response.content
                ):
                    await record_api_call_error(
                        credential_manager,
                        current_file,
                        461,
                        mode="antigravity",
                        model_name=model_name,
                        error_message="模型输出为空，请检查是否含有敏感内容",
                    )
                    return build_empty_model_output_response()
                await record_api_call_success(
                    credential_manager,
                    current_file,
                    mode="antigravity",
                    model_name=model_name,
                )
                return _response_from_httpx(response)

            if (
                response.status_code == 401
                and current_file not in refreshed_after_401
            ):
                refreshed_after_401.add(current_file)
                refreshed = await credential_manager.refresh_credential(
                    current_file, mode="antigravity"
                )
                if refreshed:
                    refreshed_token = (
                        refreshed.get("access_token") or refreshed.get("token")
                    )
                    refreshed_project = refreshed.get("project_id")
                    if refreshed_token and refreshed_project:
                        auth_headers["Authorization"] = f"Bearer {refreshed_token}"
                        final_payload["project"] = refreshed_project
                        final_payload["requestId"] = _generate_request_id(model_name)
                        continue

            error_text = getattr(response, "text", "") or ""
            last_error = _response_from_httpx(response)
            log.warning(
                f"[ANTIGRAVITY] 上游返回 {response.status_code}: "
                f"credential={current_file}, model={model_name}, "
                f"request_id={final_payload.get('requestId', '')}"
            )
            await _record_response_error(
                current_file, model_name, response.status_code, error_text
            )
            if not is_retryable_status(response.status_code):
                return last_error
        except Exception as exc:
            log.warning(
                f"[ANTIGRAVITY] 请求异常，切换凭证: {current_file}: {exc}"
            )

        if retry_limit_reached(retry_config, len(attempted)):
            return last_error or Response(
                content=json.dumps({"error": "请求失败，已达到凭证尝试上限"}),
                status_code=500,
                media_type="application/json",
            )
        if retry_config["retry_interval"] > 0:
            await asyncio.sleep(retry_config["retry_interval"])
        selected = await _get_valid_request_credential(
            model_name, session_key, attempted
        )
        if not selected:
            return last_error or Response(
                content=json.dumps({"error": "没有更多可用凭证"}),
                status_code=500,
                media_type="application/json",
            )
        current_file, token, project_id = selected
        auth_headers["Authorization"] = f"Bearer {token}"
        final_payload["project"] = project_id
        final_payload["requestId"] = _generate_request_id(model_name)


async def fetch_available_models() -> List[Dict[str, Any]]:
    result = await credential_manager.get_valid_credential(mode="antigravity")
    if not result:
        return []
    filename, credential_data = result
    token = credential_data.get("access_token") or credential_data.get("token")
    if not token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {filename}")
        return []

    try:
        api_url = await get_antigravity_api_url()
        response = await post_async(
            url=f"{api_url}/v1internal:fetchAvailableModels",
            json={},
            headers=build_antigravity_headers(token),
        )
        if response.status_code != 200:
            log.error(
                f"[ANTIGRAVITY] Failed to fetch models ({response.status_code})"
            )
            return []

        data = response.json()
        models = data.get("models", {})
        timestamp = int(datetime.now(timezone.utc).timestamp())
        result_models = [
            model_to_dict(
                Model(
                    id=model_id,
                    object="model",
                    created=timestamp,
                    owned_by="google",
                )
            )
            for model_id in models
        ]
        if "claude-sonnet-4-6" in models:
            result_models.append(
                model_to_dict(
                    Model(
                        id="claude-sonnet-4-6-thinking",
                        object="model",
                        created=timestamp,
                        owned_by="google",
                    )
                )
            )
        if "claude-opus-4-6-thinking" in models:
            result_models.append(
                model_to_dict(
                    Model(
                        id="claude-opus-4-6",
                        object="model",
                        created=timestamp,
                        owned_by="google",
                    )
                )
            )
        return result_models
    except Exception as exc:
        log.error(f"[ANTIGRAVITY] Failed to fetch models: {exc}")
        return []


async def fetch_quota_info(access_token: str) -> Dict[str, Any]:
    try:
        api_url = await get_antigravity_api_url()
        response = await post_async(
            url=f"{api_url}/v1internal:fetchAvailableModels",
            json={},
            headers=build_antigravity_headers(access_token),
            timeout=30.0,
        )
        if response.status_code != 200:
            return {
                "success": False,
                "error": f"API返回错误: {response.status_code}",
            }

        quota_info = {}
        for model_id, model_data in response.json().get("models", {}).items():
            if not isinstance(model_data, dict) or "quotaInfo" not in model_data:
                continue
            quota = model_data["quotaInfo"]
            reset_time_raw = quota.get("resetTime", "")
            reset_time = "N/A"
            if reset_time_raw:
                try:
                    utc_date = datetime.fromisoformat(
                        reset_time_raw.replace("Z", "+00:00")
                    )
                    reset_time = (utc_date + timedelta(hours=8)).strftime(
                        "%m-%d %H:%M"
                    )
                except ValueError:
                    pass
            quota_info[model_id] = {
                "remaining": quota.get("remainingFraction", 0),
                "resetTime": reset_time,
                "resetTimeRaw": reset_time_raw,
            }
        return {"success": True, "models": quota_info}
    except Exception as exc:
        log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {exc}")
        return {"success": False, "error": str(exc)}
