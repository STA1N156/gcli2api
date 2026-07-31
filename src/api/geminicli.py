"""Gemini CLI API client."""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import Response

from config import get_code_assist_endpoint, get_empty_output_error_enabled
from log import log
from src.api.empty_output import (
    build_empty_model_output_response,
    is_empty_model_output,
    stream_chunk_has_visible_output,
)
from src.api.utils import (
    get_retry_config,
    is_retryable_status,
    record_api_call_error,
    record_api_call_success,
    retry_limit_reached,
)
from src.credential_manager import credential_manager
from src.httpx_client import post_async, stream_post_async
from src.session_affinity import extract_cache_session_key
from src.utils import get_geminicli_user_agent


class InvalidCredentialError(Exception):
    """凭证缺少请求必需字段。"""


def _get_invalid_credential_reason(credential_data: Dict[str, Any]) -> Optional[str]:
    if not (credential_data.get("token") or credential_data.get("access_token")):
        return "凭证中没有访问令牌"
    if not credential_data.get("project_id"):
        return "凭证中没有项目 ID"
    return None


async def prepare_request_headers_and_payload(
    payload: dict,
    credential_data: dict,
    target_url: str,
) -> Tuple[Dict[str, str], Dict[str, Any], str]:
    invalid_reason = _get_invalid_credential_reason(credential_data)
    if invalid_reason:
        raise InvalidCredentialError(invalid_reason)

    token = credential_data.get("token") or credential_data.get("access_token")
    return (
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": get_geminicli_user_agent(payload.get("model", "")),
        },
        {
            "model": payload.get("model"),
            "project": credential_data["project_id"],
            "request": payload.get("request", {}),
        },
        target_url,
    )


async def _prepare_with_valid_credential(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    model_name: str,
    session_key: Optional[str],
    target_url: str,
    excluded_credentials: Set[str],
) -> Optional[Tuple[str, Dict[str, str], Dict[str, Any], str]]:
    """从未尝试的候选中找到一份结构完整的凭证。"""
    excluded = set(excluded_credentials)
    while True:
        cred_result = await credential_manager.get_valid_credential(
            mode="geminicli",
            model_name=model_name,
            session_key=session_key,
            exclude_credentials=set(excluded),
        )
        if not cred_result:
            return None

        filename, credential_data = cred_result
        filename = Path(filename).name
        excluded.add(filename)
        try:
            auth_headers, payload, prepared_url = (
                await prepare_request_headers_and_payload(
                    body, credential_data, target_url
                )
            )
        except InvalidCredentialError as exc:
            log.warning(f"[GEMINICLI] 禁用无效凭证 {filename}: {exc}")
            await credential_manager.set_cred_disabled(
                filename, True, mode="geminicli"
            )
            continue

        excluded_credentials.add(filename)
        if headers:
            auth_headers.update(headers)
        return filename, auth_headers, payload, prepared_url


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
    current_file: str,
    model_name: str,
    status_code: int,
    error_text: str,
) -> None:
    if status_code == 404 and "preview" in model_name.lower():
        await credential_manager.update_credential_state(
            current_file, {"preview": False}, mode="geminicli"
        )
    await record_api_call_error(
        credential_manager,
        current_file,
        status_code,
        mode="geminicli",
        model_name=model_name,
        error_message=error_text,
    )


async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
):
    """流式请求；每次失败都换一份本次尚未尝试的可用凭证。"""
    model_name = body.get("model", "")
    session_key = extract_cache_session_key(body, headers)
    target_url = f"{await get_code_assist_endpoint()}/v1internal:streamGenerateContent?alt=sse"
    attempted: Set[str] = set()
    prepared = await _prepare_with_valid_credential(
        body, headers, model_name, session_key, target_url, attempted
    )
    if not prepared:
        yield Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json",
        )
        return

    retry_config = await get_retry_config()
    empty_output_error_enabled = await get_empty_output_error_enabled()
    last_error: Optional[Response] = None
    refreshed_after_401: Set[str] = set()

    while True:
        current_file, auth_headers, final_payload, target_url = prepared
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
                    if (
                        chunk.status_code == 401
                        and current_file not in refreshed_after_401
                    ):
                        refreshed_after_401.add(current_file)
                        refreshed = await credential_manager.refresh_credential(
                            current_file, mode="geminicli"
                        )
                        if refreshed and not _get_invalid_credential_reason(refreshed):
                            token = refreshed.get("token") or refreshed.get("access_token")
                            auth_headers["Authorization"] = f"Bearer {token}"
                            final_payload["project"] = refreshed["project_id"]
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
                        mode="geminicli",
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
                        mode="geminicli",
                        model_name=model_name,
                        error_message="模型输出为空，请检查是否含有敏感内容",
                    )
                    yield build_empty_model_output_response()
                return
        except Exception as exc:
            log.warning(
                f"[GEMINICLI STREAM] 请求异常，切换凭证: {current_file}: {exc}"
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
        prepared = await _prepare_with_valid_credential(
            body, headers, model_name, session_key, target_url, attempted
        )
        if not prepared:
            yield last_error or Response(
                content=json.dumps({"error": "没有更多可用凭证"}),
                status_code=500,
                media_type="application/json",
            )
            return


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """非流式请求；选择、冷却过滤和重试规则与流式请求完全一致。"""
    model_name = body.get("model", "")
    session_key = extract_cache_session_key(body, headers)
    target_url = f"{await get_code_assist_endpoint()}/v1internal:generateContent"
    attempted: Set[str] = set()
    prepared = await _prepare_with_valid_credential(
        body, headers, model_name, session_key, target_url, attempted
    )
    if not prepared:
        return Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json",
        )

    retry_config = await get_retry_config()
    empty_output_error_enabled = await get_empty_output_error_enabled()
    last_error: Optional[Response] = None
    refreshed_after_401: Set[str] = set()

    while True:
        current_file, auth_headers, final_payload, target_url = prepared
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
                        mode="geminicli",
                        model_name=model_name,
                        error_message="模型输出为空，请检查是否含有敏感内容",
                    )
                    return build_empty_model_output_response()
                await record_api_call_success(
                    credential_manager,
                    current_file,
                    mode="geminicli",
                    model_name=model_name,
                )
                return _response_from_httpx(response)

            if (
                response.status_code == 401
                and current_file not in refreshed_after_401
            ):
                refreshed_after_401.add(current_file)
                refreshed = await credential_manager.refresh_credential(
                    current_file, mode="geminicli"
                )
                if refreshed and not _get_invalid_credential_reason(refreshed):
                    token = refreshed.get("token") or refreshed.get("access_token")
                    auth_headers["Authorization"] = f"Bearer {token}"
                    final_payload["project"] = refreshed["project_id"]
                    continue

            error_text = getattr(response, "text", "") or ""
            last_error = _response_from_httpx(response)
            await _record_response_error(
                current_file, model_name, response.status_code, error_text
            )
            if not is_retryable_status(response.status_code):
                return last_error
        except Exception as exc:
            log.warning(
                f"[GEMINICLI] 请求异常，切换凭证: {current_file}: {exc}"
            )

        if retry_limit_reached(retry_config, len(attempted)):
            return last_error or Response(
                content=json.dumps({"error": "请求失败，已达到凭证尝试上限"}),
                status_code=500,
                media_type="application/json",
            )

        if retry_config["retry_interval"] > 0:
            await asyncio.sleep(retry_config["retry_interval"])
        prepared = await _prepare_with_valid_credential(
            body, headers, model_name, session_key, target_url, attempted
        )
        if not prepared:
            return last_error or Response(
                content=json.dumps({"error": "没有更多可用凭证"}),
                status_code=500,
                media_type="application/json",
            )
