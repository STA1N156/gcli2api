"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.

- 启动时加载一次配置到内存
- 修改配置时调用 reload_config() 重新从数据库加载
"""

import os
import time
from typing import Any, Optional

# 全局配置缓存
_config_cache: dict[str, Any] = {}
_config_initialized = False
_config_loaded_at = 0.0
_CONFIG_REFRESH_INTERVAL = 5.0

# ====================== 环境变量映射表 ======================
# 统一维护环境变量名和配置键名的映射关系
# 格式: "环境变量名": "配置键名"
ENV_MAPPINGS = {
    "CODE_ASSIST_ENDPOINT": "code_assist_endpoint",
    "CREDENTIALS_DIR": "credentials_dir",
    "PROXY": "proxy",
    "OAUTH_PROXY_URL": "oauth_proxy_url",
    "GOOGLEAPIS_PROXY_URL": "googleapis_proxy_url",
    "RESOURCE_MANAGER_API_URL": "resource_manager_api_url",
    "SERVICE_USAGE_API_URL": "service_usage_api_url",
    "ANTIGRAVITY_API_URL": "antigravity_api_url",
    "CREDENTIAL_RETRY_LIMIT_ENABLED": "credential_retry_limit_enabled",
    "MAX_RETRY_CREDENTIALS": "max_retry_credentials",
    "CREDENTIAL_RETRY_INTERVAL": "credential_retry_interval",
    # 旧变量名继续锁定对应的新配置，便于无感升级。
    "RETRY_429_MAX_RETRIES": "max_retry_credentials",
    "RETRY_429_ENABLED": "credential_retry_limit_enabled",
    "RETRY_429_INTERVAL": "credential_retry_interval",
    "SESSION_AFFINITY_ENABLED": "session_affinity_enabled",
    "SESSION_AFFINITY_TTL_SECONDS": "session_affinity_ttl_seconds",
    "EMPTY_OUTPUT_ERROR_ENABLED": "empty_output_error_enabled",
    "ANTI_TRUNCATION_MAX_ATTEMPTS": "anti_truncation_max_attempts",
    "COMPATIBILITY_MODE": "compatibility_mode_enabled",
    "RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend",
    "ANTIGRAVITY_STREAM2NOSTREAM": "antigravity_stream2nostream",
    "HOST": "host",
    "PORT": "port",
    "API_PASSWORD": "api_password",
    "PANEL_PASSWORD": "panel_password",
    "PASSWORD": "password",
    "KEEPALIVE_URL": "keepalive_url",
    "KEEPALIVE_INTERVAL": "keepalive_interval",
}


# ====================== 配置系统 ======================

async def init_config():
    """初始化配置缓存（启动时调用一次）"""
    global _config_cache, _config_initialized, _config_loaded_at

    if _config_initialized:
        return

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
        _config_loaded_at = time.monotonic()
    except Exception:
        # 初始化失败时使用空缓存
        _config_cache = {}
        _config_initialized = True
        _config_loaded_at = time.monotonic()


async def reload_config():
    """重新加载配置（修改配置后调用）"""
    global _config_cache, _config_initialized, _config_loaded_at

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()

        # 如果后端支持 reload_config_cache，调用它
        if hasattr(storage_adapter._backend, 'reload_config_cache'):
            await storage_adapter._backend.reload_config_cache()

        # 重新加载配置缓存
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
    except Exception:
        pass
    finally:
        _config_loaded_at = time.monotonic()


def _get_cached_config(key: str, default: Any = None) -> Any:
    """从内存缓存获取配置（同步）"""
    return _config_cache.get(key, default)


async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    """Get configuration value with priority: ENV > Storage > default."""
    global _config_loaded_at

    # 每个 Worker 定期从共享存储刷新，避免长期读取其他 Worker 保存前的旧值
    if not _config_initialized:
        await init_config()
    elif time.monotonic() - _config_loaded_at >= _CONFIG_REFRESH_INTERVAL:
        _config_loaded_at = time.monotonic()
        await reload_config()

    # Priority 1: Environment variable
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)

    # Priority 2: Memory cache
    value = _get_cached_config(key)
    if value is not None:
        return value

    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration."""
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    return proxy_url if proxy_url else None


async def get_max_retry_credentials() -> int:
    """开启限制后，单次请求最多尝试的凭证总数。"""
    env_value = os.getenv("MAX_RETRY_CREDENTIALS") or os.getenv(
        "RETRY_429_MAX_RETRIES"
    )
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            pass

    value = await get_config_value("max_retry_credentials")
    if value is None:
        value = await get_config_value("retry_429_max_retries", 5)
    return max(1, int(value))


async def get_credential_retry_limit_enabled() -> bool:
    """是否限制单次请求尝试的凭证数量。"""
    env_value = os.getenv("CREDENTIAL_RETRY_LIMIT_ENABLED") or os.getenv(
        "RETRY_429_ENABLED"
    )
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    value = await get_config_value("credential_retry_limit_enabled")
    if value is None:
        value = await get_config_value("retry_429_enabled", False)
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


async def get_credential_retry_interval() -> float:
    """切换到下一份凭证前的等待时间。"""
    env_value = os.getenv("CREDENTIAL_RETRY_INTERVAL") or os.getenv(
        "RETRY_429_INTERVAL"
    )
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass

    value = await get_config_value("credential_retry_interval")
    if value is None:
        value = await get_config_value("retry_429_interval", 1)
    return float(value)


async def get_session_affinity_enabled() -> bool:
    """Whether requests from the same session should reuse one credential."""
    env_value = os.getenv("SESSION_AFFINITY_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("session_affinity_enabled", False))


async def get_session_affinity_ttl_seconds() -> int:
    """How long a successful session-to-credential binding remains active."""
    env_value = os.getenv("SESSION_AFFINITY_TTL_SECONDS")
    if env_value:
        try:
            return max(60, int(env_value))
        except ValueError:
            pass

    return max(60, int(await get_config_value("session_affinity_ttl_seconds", 3600)))


async def get_empty_output_error_enabled() -> bool:
    """Whether empty model output should be converted to HTTP 461."""
    env_value = os.getenv("EMPTY_OUTPUT_ERROR_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    value = await get_config_value("empty_output_error_enabled", True)
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.

    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    Database config key: anti_truncation_max_attempts
    Default: 3
    """
    env_value = os.getenv("ANTI_TRUNCATION_MAX_ATTEMPTS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("anti_truncation_max_attempts", 3))


# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.

    Environment variable: HOST
    Database config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))


async def get_server_port() -> int:
    """
    Get server port setting.

    Environment variable: PORT
    Database config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("port", 7861))


async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.

    Environment variable: API_PASSWORD
    Database config key: api_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 API_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    api_password = await get_config_value("api_password", None, "API_PASSWORD")
    if api_password is not None:
        return str(api_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.

    Environment variable: PANEL_PASSWORD
    Database config key: panel_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 PANEL_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    panel_password = await get_config_value("panel_password", None, "PANEL_PASSWORD")
    if panel_password is not None:
        return str(panel_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_server_password() -> str:
    """
    Get server password setting (deprecated, use get_api_password or get_panel_password).

    Environment variable: PASSWORD
    Database config key: password
    Default: pwd
    """
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.

    Environment variable: CREDENTIALS_DIR
    Database config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_code_assist_endpoint() -> str:
    """
    Get Code Assist endpoint setting.

    Environment variable: CODE_ASSIST_ENDPOINT
    Database config key: code_assist_endpoint
    Default: https://cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "code_assist_endpoint", "https://cloudcode-pa.googleapis.com", "CODE_ASSIST_ENDPOINT"
        )
    )


async def get_compatibility_mode_enabled() -> bool:
    """
    Get compatibility mode setting.

    兼容性模式：启用后所有system消息全部转换成user，停用system_instructions。
    该选项可能会降低模型理解能力，但是能避免流式空回的情况。

    Environment variable: COMPATIBILITY_MODE
    Database config key: compatibility_mode_enabled
    Default: False
    """
    env_value = os.getenv("COMPATIBILITY_MODE")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("compatibility_mode_enabled", False))


async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    控制是否将思维链返回到前端。
    启用后，思维链会在响应中返回；禁用后，思维链会在响应中被过滤掉。

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    Database config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))


async def get_antigravity_stream2nostream() -> bool:
    """
    Get use stream for non-stream setting.

    控制antigravity非流式请求是否使用流式API并收集为完整响应。
    启用后，非流式请求将在后端使用流式API，然后收集所有块后再返回完整响应。

    Environment variable: ANTIGRAVITY_STREAM2NOSTREAM
    Database config key: antigravity_stream2nostream
    Default: True
    """
    env_value = os.getenv("ANTIGRAVITY_STREAM2NOSTREAM")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("antigravity_stream2nostream", True))



async def get_oauth_proxy_url() -> str:
    """
    Get OAuth proxy URL setting.

    用于Google OAuth2认证的代理URL。

    Environment variable: OAUTH_PROXY_URL
    Database config key: oauth_proxy_url
    Default: https://oauth2.googleapis.com
    """
    return str(
        await get_config_value(
            "oauth_proxy_url", "https://oauth2.googleapis.com", "OAUTH_PROXY_URL"
        )
    )


async def get_googleapis_proxy_url() -> str:
    """
    Get Google APIs proxy URL setting.

    用于Google APIs调用的代理URL。

    Environment variable: GOOGLEAPIS_PROXY_URL
    Database config key: googleapis_proxy_url
    Default: https://www.googleapis.com
    """
    return str(
        await get_config_value(
            "googleapis_proxy_url", "https://www.googleapis.com", "GOOGLEAPIS_PROXY_URL"
        )
    )


async def get_resource_manager_api_url() -> str:
    """
    Get Google Cloud Resource Manager API URL setting.

    用于Google Cloud Resource Manager API的URL。

    Environment variable: RESOURCE_MANAGER_API_URL
    Database config key: resource_manager_api_url
    Default: https://cloudresourcemanager.googleapis.com
    """
    return str(
        await get_config_value(
            "resource_manager_api_url",
            "https://cloudresourcemanager.googleapis.com",
            "RESOURCE_MANAGER_API_URL",
        )
    )


async def get_service_usage_api_url() -> str:
    """
    Get Google Cloud Service Usage API URL setting.

    用于Google Cloud Service Usage API的URL。

    Environment variable: SERVICE_USAGE_API_URL
    Database config key: service_usage_api_url
    Default: https://serviceusage.googleapis.com
    """
    return str(
        await get_config_value(
            "service_usage_api_url", "https://serviceusage.googleapis.com", "SERVICE_USAGE_API_URL"
        )
    )


async def get_antigravity_api_url() -> str:
    """
    Get Antigravity API URL setting.

    用于Google Antigravity API的URL。

    Environment variable: ANTIGRAVITY_API_URL
    Database config key: antigravity_api_url
    Default: https://daily-cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "antigravity_api_url",
            "https://daily-cloudcode-pa.googleapis.com",
            "ANTIGRAVITY_API_URL",
        )
    )


async def get_keepalive_url() -> str:
    """
    Get keep-alive URL setting.

    配置后保活服务会定期向该URL发送GET请求。
    留空表示禁用保活服务。

    Environment variable: KEEPALIVE_URL
    Database config key: keepalive_url
    Default: "" (disabled)
    """
    return str(await get_config_value("keepalive_url", "", "KEEPALIVE_URL"))


async def get_keepalive_interval() -> int:
    """
    Get keep-alive interval in seconds.

    保活请求发送间隔（秒）。

    Environment variable: KEEPALIVE_INTERVAL
    Database config key: keepalive_interval
    Default: 60
    """
    env_value = os.getenv("KEEPALIVE_INTERVAL")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("keepalive_interval", 60))
