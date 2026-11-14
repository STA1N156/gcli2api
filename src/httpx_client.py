"""
Shared async HTTP client helpers.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from config import get_proxy_config
from log import log
from src.agy_transport import NativeTransport, bridge


_CLIENT_KWARGS = {
    "account_id",
    "auth",
    "base_url",
    "cert",
    "cookies",
    "event_hooks",
    "follow_redirects",
    "headers",
    "http1",
    "http2",
    "limits",
    "mounts",
    "proxy",
    "timeout",
    "transport",
    "trust_env",
    "verify",
}


class HttpxClientManager:
    """Reuse HTTP clients so high RPM traffic can keep connections warm."""

    def __init__(self):
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def get_client_kwargs(self, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
        client_kwargs = {"timeout": timeout, **kwargs}
        client_kwargs.setdefault("http2", False)
        client_kwargs.setdefault(
            "limits",
            httpx.Limits(
                max_connections=int(os.getenv("HTTPX_MAX_CONNECTIONS", "128")),
                max_keepalive_connections=int(os.getenv("HTTPX_MAX_KEEPALIVE_CONNECTIONS", "8")),
                keepalive_expiry=float(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "5")),
            ),
        )

        current_proxy_config = await get_proxy_config()
        if current_proxy_config and "proxy" not in client_kwargs:
            client_kwargs["proxy"] = current_proxy_config

        return client_kwargs

    def _client_cache_key(self, client_kwargs: Dict[str, Any]) -> str:
        return repr(sorted((key, repr(value)) for key, value in client_kwargs.items()))

    async def _get_or_create_client(self, timeout: float = 30.0, **kwargs) -> httpx.AsyncClient:
        client_kwargs = await self.get_client_kwargs(timeout=timeout, **kwargs)
        account_id = client_kwargs.pop("account_id", "")
        cache_key = account_id + self._client_cache_key(client_kwargs)

        async with self._lock:
            client = self._clients.get(cache_key)
            if client and not client.is_closed:
                return client

            if "transport" not in client_kwargs:
                client_kwargs["transport"] = NativeTransport(
                    account_id, client_kwargs.pop("proxy", None),
                )
                client_kwargs["trust_env"] = False
            client = httpx.AsyncClient(**client_kwargs)
            client.headers["User-Agent"] = "Go-http-client/1.1"
            client.headers["Accept-Encoding"] = "gzip"
            client.headers.pop("Accept", None)
            client.headers.pop("Connection", None)
            self._clients[cache_key] = client
            return client

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        yield await self._get_or_create_client(timeout=timeout, **kwargs)

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: float = 900.0, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        yield await self._get_or_create_client(timeout=timeout, **kwargs)

    async def close(self):
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                await client.aclose()
            except Exception as e:
                log.warning(f"Error closing HTTP client: {e}")


http_client = HttpxClientManager()


async def close_http_clients():
    await http_client.close()
    await bridge.close()


def _split_httpx_kwargs(kwargs: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    client_kwargs = {key: value for key, value in kwargs.items() if key in _CLIENT_KWARGS}
    request_kwargs = {key: value for key, value in kwargs.items() if key not in _CLIENT_KWARGS}
    return client_kwargs, request_kwargs


async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    client_kwargs, request_kwargs = _split_httpx_kwargs(kwargs)
    async with http_client.get_client(timeout=timeout, **client_kwargs) as client:
        return await client.get(url, headers=headers, **request_kwargs)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    **kwargs,
) -> httpx.Response:
    client_kwargs, request_kwargs = _split_httpx_kwargs(kwargs)
    async with http_client.get_client(timeout=timeout, **client_kwargs) as client:
        return await client.post(url, data=data, json=json, headers=headers, **request_kwargs)


async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    **kwargs,
):
    client_kwargs, request_kwargs = _split_httpx_kwargs(kwargs)
    client_kwargs.setdefault("http2", False)
    async with http_client.get_streaming_client(**client_kwargs) as client:
        async with client.stream("POST", url, json=body, headers=headers, **request_kwargs) as r:
            if r.status_code != 200:
                from fastapi import Response

                body = await r.aread()
                response_headers = dict(r.headers)
                response_headers.pop("content-encoding", None)
                response_headers.pop("content-length", None)
                yield Response(body, r.status_code, response_headers)
                return

            if native:
                async for chunk in r.aiter_bytes():
                    yield chunk
            else:
                async for line in r.aiter_lines():
                    yield line
