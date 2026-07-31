"""
凭证管理器
"""

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from config import (
    get_session_affinity_enabled,
    get_session_affinity_ttl_seconds,
)
from log import log
from src.google_oauth_api import Credentials
from src.model_cooldown import has_active_model_cooldown
from src.storage_adapter import get_storage_adapter

SESSION_BINDING_MAX_ENTRIES = int(os.getenv("SESSION_BINDING_MAX_ENTRIES", "50000"))
SESSION_BINDING_PRUNE_INTERVAL_SECONDS = 60
ROUND_ROBIN_MAX_KEYS = 4096


class CredentialManager:
    """
    统一凭证管理器
    所有存储操作通过storage_adapter进行
    """

    def __init__(self):
        # 核心状态
        self._initialized = False
        self._storage_adapter = None
        self._session_bindings: Dict[str, Tuple[str, float]] = {}
        self._round_robin_cursors: Dict[str, int] = {}
        self._session_lock = asyncio.Lock()
        self._last_session_prune = 0.0

        # 并发控制（简化）
        # 后端数据库自行处理并发，credential_manager 不再使用本地锁

    async def _ensure_initialized(self):
        """确保管理器已初始化（内部使用）"""
        if not self._initialized or self._storage_adapter is None:
            await self.initialize()

    async def initialize(self):
        """初始化凭证管理器"""
        if self._initialized and self._storage_adapter is not None:
            return

        # 初始化统一存储适配器
        self._storage_adapter = await get_storage_adapter()
        self._initialized = True

    async def close(self):
        """清理资源"""
        log.debug("Closing credential manager...")
        self._initialized = False
        log.debug("Credential manager closed")

    def _session_binding_key(
        self, mode: str, model_name: Optional[str], session_key: Optional[str]
    ) -> Optional[str]:
        if not session_key:
            return None
        return f"{mode}:{model_name or ''}:{session_key}"

    def _session_log_id(self, binding_key: str) -> str:
        return hashlib.sha256(binding_key.encode("utf-8")).hexdigest()[:12]

    def _shared_session_binding_key(self, binding_key: str) -> str:
        return hashlib.sha256(binding_key.encode("utf-8")).hexdigest()

    def _prune_session_bindings_locked(self, now: float) -> None:
        if (
            now - self._last_session_prune < SESSION_BINDING_PRUNE_INTERVAL_SECONDS
            and len(self._session_bindings) <= SESSION_BINDING_MAX_ENTRIES
        ):
            return

        self._last_session_prune = now
        for key, (_, expires_at) in list(self._session_bindings.items()):
            if expires_at <= now:
                self._session_bindings.pop(key, None)

        overflow = len(self._session_bindings) - SESSION_BINDING_MAX_ENTRIES
        if overflow > 0:
            for key in list(self._session_bindings)[:overflow]:
                self._session_bindings.pop(key, None)

    async def _get_session_binding(self, binding_key: str) -> Optional[str]:
        shared_get = getattr(self._storage_adapter, "get_session_binding", None)
        if callable(shared_get):
            filename = await shared_get(
                self._shared_session_binding_key(binding_key), time.time()
            )
            if filename:
                return filename

        async with self._session_lock:
            binding = self._session_bindings.get(binding_key)
            if not binding:
                return None
            filename, expires_at = binding
            if expires_at <= time.time():
                self._session_bindings.pop(binding_key, None)
                return None
            return filename

    async def _remember_session_binding(
        self,
        binding_key: Optional[str],
        filename: str,
        ttl_seconds: int,
    ) -> None:
        if not binding_key or not filename:
            return
        if SESSION_BINDING_MAX_ENTRIES <= 0:
            return
        now = time.time()
        filename = os.path.basename(filename)
        expires_at = now + ttl_seconds
        shared_set = getattr(self._storage_adapter, "set_session_binding", None)
        if callable(shared_set):
            if await shared_set(
                self._shared_session_binding_key(binding_key), filename, expires_at
            ):
                return
        async with self._session_lock:
            self._prune_session_bindings_locked(now)
            self._session_bindings[binding_key] = (
                filename,
                expires_at,
            )

    async def _forget_session_binding(
        self,
        binding_key: Optional[str],
        expected_filename: Optional[str] = None,
    ) -> None:
        if not binding_key:
            return
        expected_filename = (
            os.path.basename(expected_filename) if expected_filename else None
        )
        shared_delete = getattr(self._storage_adapter, "delete_session_binding", None)
        if callable(shared_delete):
            await shared_delete(
                self._shared_session_binding_key(binding_key), expected_filename
            )
        async with self._session_lock:
            local = self._session_bindings.get(binding_key)
            if expected_filename is None or (local and local[0] == expected_filename):
                self._session_bindings.pop(binding_key, None)

    async def _get_bound_credential_if_available(
        self,
        filename: str,
        *,
        mode: str,
        model_name: Optional[str],
        exclude_credentials: Set[str],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        if os.path.basename(filename) in exclude_credentials:
            return None

        state = await self._storage_adapter.get_credential_state(filename, mode=mode)
        if not self._credential_state_allows_model(
            state, mode=mode, model_name=model_name
        ):
            return None

        credential_data = await self._storage_adapter.get_credential(filename, mode=mode)
        if not credential_data:
            return None

        if mode == "antigravity":
            credential_data["enable_credit"] = bool(state.get("enable_credit", False))

        return os.path.basename(filename), credential_data

    def _credential_state_allows_model(
        self,
        state: Dict[str, Any],
        *,
        mode: str,
        model_name: Optional[str],
    ) -> bool:
        if state.get("disabled"):
            return False

        model_lower = (model_name or "").lower()
        if has_active_model_cooldown(state.get("model_cooldowns"), model_name, mode=mode):
            return False

        if mode == "geminicli":
            if "pro" in model_lower and state.get("tier") == "free":
                return False
            if "preview" in model_lower and state.get("preview") is False:
                return False

        return True

    async def _get_available_credential(
        self,
        *,
        mode: str,
        model_name: Optional[str],
        exclude_credentials: Set[str],
        session_fallback_key: Optional[str] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        states = await self._storage_adapter.get_all_credential_states(mode=mode)
        if not states:
            return None

        candidates: List[Tuple[str, Dict[str, Any]]] = []

        for raw_filename, state in states.items():
            filename = os.path.basename(raw_filename)
            if filename in exclude_credentials:
                continue
            if not self._credential_state_allows_model(
                state,
                mode=mode,
                model_name=model_name,
            ):
                continue
            candidates.append((filename, state))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        if session_fallback_key:
            # A worker that has not seen this session yet must choose the same
            # initial credential as other workers. Once chosen, the normal
            # session binding below remains authoritative and can migrate.
            ordered_candidates = sorted(
                candidates,
                key=lambda item: hashlib.sha256(
                    f"{session_fallback_key}\0{item[0]}".encode("utf-8")
                ).digest(),
                reverse=True,
            )
        else:
            cursor_key = f"{mode}:{(model_name or '').strip().lower()}"
            async with self._session_lock:
                if (
                    cursor_key not in self._round_robin_cursors
                    and len(self._round_robin_cursors) >= ROUND_ROBIN_MAX_KEYS
                ):
                    self._round_robin_cursors.clear()
                start = self._round_robin_cursors.get(cursor_key, 0) % len(candidates)
                self._round_robin_cursors[cursor_key] = start + 1

            ordered_candidates = candidates[start:] + candidates[:start]

        for filename, state in ordered_candidates:
            credential_data = await self._storage_adapter.get_credential(filename, mode=mode)
            if not credential_data:
                continue

            if mode == "antigravity":
                credential_data["enable_credit"] = bool(state.get("enable_credit", False))
            return filename, credential_data

        return None

    async def get_valid_credential(
        self,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        session_key: Optional[str] = None,
        exclude_credential: Optional[str] = None,
        exclude_credentials: Optional[Set[str]] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        按 CLIProxy 的方式获取凭证：
        先过滤禁用、模型冷却和本次请求已尝试的凭证，再按模型轮询。
        开启粘性会话后优先复用仍可用的绑定凭证；不可用时自动换下一个。

        Args:
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 完整模型名，用于模型级冷却检查和preview筛选
                       - geminicli: Pro 模型排除 free，preview 模型排除不支持的凭证
                       - antigravity: 按完整模型名过滤冷却
        """
        await self._ensure_initialized()
        excluded_credentials = {
            os.path.basename(filename)
            for filename in (exclude_credentials or set())
            if filename
        }
        if exclude_credential:
            excluded_credentials.add(os.path.basename(exclude_credential))

        affinity_enabled = bool(session_key) and await get_session_affinity_enabled()
        binding_key = (
            self._session_binding_key(mode, model_name, session_key)
            if affinity_enabled
            else None
        )
        affinity_ttl = (
            await get_session_affinity_ttl_seconds()
            if binding_key
            else 0
        )

        if binding_key:
            bound_filename = await self._get_session_binding(binding_key)
            if bound_filename:
                bound_result = await self._get_bound_credential_if_available(
                    bound_filename,
                    mode=mode,
                    model_name=model_name,
                    exclude_credentials=excluded_credentials,
                )
                if bound_result:
                    filename, credential_data = bound_result
                    if await self._should_refresh_token(credential_data):
                        refreshed_data = await self._refresh_token(credential_data, filename, mode=mode)
                        if refreshed_data:
                            if log.is_debug_enabled():
                                log.debug(
                                    "Session route hit after refresh: "
                                    f"session={self._session_log_id(binding_key)}, "
                                    f"credential={filename}, mode={mode}, model={model_name}"
                                )
                            await self._remember_session_binding(
                                binding_key, filename, affinity_ttl
                            )
                            return filename, refreshed_data
                        await self._forget_session_binding(binding_key, filename)
                        excluded_credentials.add(filename)
                    else:
                        if log.is_debug_enabled():
                            log.debug(
                                "Session route hit: "
                                f"session={self._session_log_id(binding_key)}, "
                                f"credential={filename}, mode={mode}, model={model_name}"
                            )
                        await self._remember_session_binding(
                            binding_key, filename, affinity_ttl
                        )
                        return filename, credential_data
                else:
                    await self._forget_session_binding(binding_key, bound_filename)

        while True:
            result = await self._get_available_credential(
                mode=mode,
                model_name=model_name,
                exclude_credentials=excluded_credentials,
                session_fallback_key=binding_key,
            )
            if not result:
                log.warning(f"没有可用凭证 (mode={mode}, model_name={model_name})")
                return None

            filename, credential_data = result
            if await self._should_refresh_token(credential_data):
                log.debug(f"Token需要刷新: {filename} (mode={mode})")
                refreshed_data = await self._refresh_token(credential_data, filename, mode=mode)
                if refreshed_data:
                    credential_data = refreshed_data
                else:
                    excluded_credentials.add(os.path.basename(filename))
                    await self._forget_session_binding(binding_key, filename)
                    continue

            await self._remember_session_binding(
                binding_key, filename, affinity_ttl
            )
            return filename, credential_data

    async def add_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """新增或更新一个凭证。"""
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data)
        log.info(f"Credential added/updated: {credential_name}")

    async def add_antigravity_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """新增或更新一个 Antigravity 凭证。"""
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data, mode="antigravity")
        log.info(f"Antigravity credential added/updated: {credential_name}")

    async def remove_credential(self, credential_name: str, mode: str = "geminicli") -> bool:
        """删除一个凭证"""
        await self._ensure_initialized()
        try:
            await self._storage_adapter.delete_credential(credential_name, mode=mode)
            log.info(f"Credential removed: {credential_name} (mode={mode})")
            return True
        except Exception as e:
            log.error(f"Error removing credential {credential_name}: {e}")
            return False

    async def update_credential_state(self, credential_name: str, state_updates: Dict[str, Any], mode: str = "geminicli"):
        """更新凭证状态"""
        await self._ensure_initialized()
        try:
            success = await self._storage_adapter.update_credential_state(
                credential_name, state_updates, mode=mode
            )
            if not success:
                log.warning(f"Failed to update credential state: {credential_name} (mode={mode})")
            return success
        except Exception as e:
            log.error(f"Error updating credential state {credential_name}: {e}")
            return False

    async def set_cred_disabled(self, credential_name: str, disabled: bool, mode: str = "geminicli"):
        """设置凭证的启用/禁用状态"""
        try:
            success = await self.update_credential_state(
                credential_name, {"disabled": disabled}, mode=mode
            )
            if success:
                action = "disabled" if disabled else "enabled"
                log.info(f"Credential {action}: {credential_name} (mode={mode})")
            return success
        except Exception as e:
            log.error(f"Error setting credential disabled state {credential_name}: {e}")
            return False

    async def get_creds_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证的状态"""
        await self._ensure_initialized()
        try:
            return await self._storage_adapter.get_all_credential_states()
        except Exception as e:
            log.error(f"Error getting credential statuses: {e}")
            return {}

    async def get_creds_summary(self) -> List[Dict[str, Any]]:
        """
        获取所有凭证的摘要信息（轻量级，不包含完整凭证数据）
        使用后端的高性能查询
        """
        await self._ensure_initialized()
        try:
            return await self._storage_adapter._backend.get_credentials_summary()
        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return []

    async def get_or_fetch_user_email(self, credential_name: str, mode: str = "geminicli") -> Optional[str]:
        """获取或获取用户邮箱地址"""
        try:
            # 确保已初始化
            await self._ensure_initialized()
            
            # 从状态中获取缓存的邮箱
            state = await self._storage_adapter.get_credential_state(credential_name, mode=mode)
            cached_email = state.get("user_email") if state else None

            if cached_email:
                return cached_email

            # 如果没有缓存，从凭证数据获取
            credential_data = await self._storage_adapter.get_credential(credential_name, mode=mode)
            if not credential_data:
                return None

            # 创建凭证对象并自动刷新 token
            from .google_oauth_api import Credentials, get_user_email

            credentials = Credentials.from_dict(credential_data)
            if not credentials:
                return None

            # 自动刷新 token（如果需要）
            token_refreshed = await credentials.refresh_if_needed()

            # 如果 token 被刷新了，更新存储
            if token_refreshed:
                log.info(f"Token已自动刷新: {credential_name} (mode={mode})")
                updated_data = credentials.to_dict()
                await self._storage_adapter.store_credential(credential_name, updated_data, mode=mode)

            # 获取邮箱
            email = await get_user_email(credentials)

            if email:
                # 缓存邮箱地址
                await self._storage_adapter.update_credential_state(
                    credential_name, {"user_email": email}, mode=mode
                )
                return email

            return None

        except Exception as e:
            log.error(f"Error fetching user email for {credential_name}: {e}")
            return None

    async def record_api_call_result(
        self,
        credential_name: str,
        success: bool,
        error_code: Optional[int] = None,
        cooldown_until: Optional[float] = None,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """
        记录API调用结果

        Args:
            credential_name: 凭证名称
            success: 是否成功
            error_code: 错误码（如果失败）
            cooldown_until: 冷却截止时间戳（Unix时间戳，针对429 QUOTA_EXHAUSTED）
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 模型名（用于设置模型级冷却）
            error_message: 错误信息（如果失败）
        """
        await self._ensure_initialized()
        try:
            if success:
                # 存储层只在有错误/冷却时写入；这里等待完成，避免旧成功结果
                # 在新冷却之后才落库并把冷却误删。
                await self._storage_adapter._backend.record_success(
                    credential_name, model_name=model_name, mode=mode
                )

            elif error_code:
                # 记录错误码和错误信息
                error_messages = {}
                if error_message:
                    error_messages[str(error_code)] = error_message

                state_updates = {
                    "error_codes": [error_code],
                    "error_messages": error_messages,
                }

                await self.update_credential_state(credential_name, state_updates, mode=mode)

                # 设置模型级冷却
                if cooldown_until is not None and model_name:
                    if hasattr(self._storage_adapter._backend, 'set_model_cooldown'):
                        await self._storage_adapter._backend.set_model_cooldown(
                            credential_name, model_name, cooldown_until, mode=mode
                        )
                        log.info(
                            f"设置模型级冷却: {credential_name}, model_name={model_name}, "
                            f"冷却至: {datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
                        )

        except Exception as e:
            log.error(f"Error recording API call result for {credential_name}: {e}")

    async def refresh_credential(
        self,
        credential_name: str,
        mode: str = "geminicli",
    ) -> Optional[Dict[str, Any]]:
        """收到 401 后强制刷新一次当前凭证。"""
        await self._ensure_initialized()
        credential_data = await self._storage_adapter.get_credential(
            credential_name, mode=mode
        )
        if not credential_data:
            return None
        return await self._refresh_token(
            credential_data, credential_name, mode=mode
        )

    async def _should_refresh_token(self, credential_data: Dict[str, Any]) -> bool:
        """检查token是否需要刷新"""
        try:
            # 如果没有access_token或过期时间，需要刷新
            if not credential_data.get("access_token") and not credential_data.get("token"):
                log.debug("没有access_token，需要刷新")
                return True

            expiry_str = credential_data.get("expiry")
            if not expiry_str:
                log.debug("没有过期时间，需要刷新")
                return True

            # 解析过期时间
            try:
                if isinstance(expiry_str, str):
                    if "+" in expiry_str:
                        file_expiry = datetime.fromisoformat(expiry_str)
                    elif expiry_str.endswith("Z"):
                        file_expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                    else:
                        file_expiry = datetime.fromisoformat(expiry_str)
                else:
                    log.debug("过期时间格式无效，需要刷新")
                    return True

                # 确保时区信息
                if file_expiry.tzinfo is None:
                    file_expiry = file_expiry.replace(tzinfo=timezone.utc)

                # 检查是否还有至少5分钟有效期
                now = datetime.now(timezone.utc)
                time_left = (file_expiry - now).total_seconds()

                log.debug(
                    f"Token时间检查: "
                    f"当前UTC时间={now.isoformat()}, "
                    f"过期时间={file_expiry.isoformat()}, "
                    f"剩余时间={int(time_left/60)}分{int(time_left%60)}秒"
                )

                if time_left > 300:  # 5分钟缓冲
                    return False
                else:
                    log.debug(f"Token即将过期（剩余{int(time_left/60)}分钟），需要刷新")
                    return True

            except Exception as e:
                log.warning(f"解析过期时间失败: {e}，需要刷新")
                return True

        except Exception as e:
            log.error(f"检查token过期时出错: {e}")
            return True

    async def _refresh_token(
        self, credential_data: Dict[str, Any], filename: str, mode: str = "geminicli"
    ) -> Optional[Dict[str, Any]]:
        """刷新token并更新存储"""
        await self._ensure_initialized()
        try:
            # 创建Credentials对象
            creds = Credentials.from_dict(credential_data)

            # 检查是否可以刷新
            if not creds.refresh_token:
                log.error(f"没有refresh_token，无法刷新: {filename} (mode={mode})")
                # 自动禁用没有refresh_token的凭证
                try:
                    await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    log.warning(f"凭证已自动禁用（缺少refresh_token）: {filename}")
                except Exception as e:
                    log.error(f"禁用凭证失败 {filename}: {e}")
                return None

            # 刷新token
            log.debug(f"正在刷新token: {filename} (mode={mode})")
            await creds.refresh()

            # 更新凭证数据
            if creds.access_token:
                credential_data["access_token"] = creds.access_token
                # 保持兼容性
                credential_data["token"] = creds.access_token

            if creds.expires_at:
                credential_data["expiry"] = creds.expires_at.isoformat()

            # 保存到存储
            await self._storage_adapter.store_credential(filename, credential_data, mode=mode)
            log.info(f"Token刷新成功并已保存: {filename} (mode={mode})")

            return credential_data

        except Exception as e:
            error_msg = str(e)
            log.error(f"Token刷新失败 {filename} (mode={mode}): {error_msg}")

            # 尝试提取HTTP状态码（TokenError可能携带status_code属性）
            status_code = None
            if hasattr(e, 'status_code'):
                status_code = e.status_code

            # 仅明确的 OAuth 吊销/失效信息才永久禁用，不能只凭 HTTP 状态码判断。
            is_permanent_failure = self._is_permanent_refresh_failure(error_msg)

            if is_permanent_failure:
                log.warning(f"检测到凭证永久失效 (HTTP {status_code}): {filename}")
                # 记录失效状态
                if status_code:
                    await self.record_api_call_result(filename, False, status_code, mode=mode)
                else:
                    await self.record_api_call_result(filename, False, 400, mode=mode)

                # 禁用失效凭证
                try:
                    disabled_ok = await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    if disabled_ok:
                        log.warning(f"永久失效凭证已禁用: {filename}")
                    else:
                        log.warning("永久失效凭证禁用失败，将由上层逻辑继续处理")
                except Exception as e2:
                    log.error(f"禁用永久失效凭证时出错 {filename}: {e2}")
            else:
                # 网络错误或其他临时性错误，不封禁凭证
                log.warning(f"Token刷新失败但非永久性错误 (HTTP {status_code})，不封禁凭证: {filename}")

            return None

    def _is_permanent_refresh_failure(self, error_msg: str) -> bool:
        """
        判断是否是凭证永久失效的错误

        Args:
            error_msg: 错误信息
        Returns:
            True表示凭证永久失效应封禁，False表示临时错误不应封禁
        """
        permanent_error_patterns = [
            "invalid_grant",
            "refresh_token_expired",
            "invalid_refresh_token",
            "unauthorized_client",
            "invalid_client",
            "token has been expired or revoked",
            "access_denied",
        ]

        error_msg_lower = error_msg.lower()
        for pattern in permanent_error_patterns:
            if pattern.lower() in error_msg_lower:
                log.debug(f"错误信息匹配到永久失效模式: {pattern}")
                return True

        # 默认认为是临时错误（如网络问题），不应封禁凭证
        log.debug("未匹配到明确的永久失效模式，判定为临时错误")
        return False

class _CredentialManagerSingleton:
    """单例包装器，支持懒加载和自动初始化"""

    _instance: Optional[CredentialManager] = None
    _lock = None

    def __init__(self):
        self._manager = None

    async def _get_or_create(self) -> CredentialManager:
        """获取或创建单例实例（线程安全）"""
        if self._instance is None:
            # 简单的实例创建（异步环境下一般不需要复杂的锁）
            if self._instance is None:
                self._instance = CredentialManager()
                await self._instance.initialize()
                log.debug("CredentialManager singleton initialized")

        return self._instance

    def __getattr__(self, name):
        """代理所有方法调用到真实的 CredentialManager 实例"""
        async def _async_wrapper(*args, **kwargs):
            manager = await self._get_or_create()
            method = getattr(manager, name)
            return await method(*args, **kwargs)

        return _async_wrapper


# 全局单例实例 - 直接导入即可使用
credential_manager = _CredentialManagerSingleton()
