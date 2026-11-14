"""Account-scoped HTTP transport backed by the CPA Go TLS profile."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx


def normalize_proxy_url(value):
    if value is not None and not isinstance(value, str):
        raise ValueError("代理地址必须是文本")
    value = (value or "").strip()
    if value in ("", "direct"):
        return value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname or not parsed.port
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("代理地址请填写 http://、https://、socks5:// 或 socks5h://主机:端口") from None
    return value


def credential_http_options(data):
    identity = data.get("refresh_token") or data.get("email") or data.get("access_token") or data.get("token") or ""
    options = {"account_id": hashlib.sha256(identity.encode()).hexdigest()}
    proxy = data.get("proxy_url", data.get("proxy-url", ""))
    if proxy:
        options["proxy"] = normalize_proxy_url(proxy)
    return options


class NativeBridge:
    def __init__(self):
        self.process = None
        self.address = None
        self.local = None
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return self.address
            root = Path(__file__).resolve().parents[1] / "native"
            binary = Path(os.environ.get("AGY_HTTP_BRIDGE") or root / "bin" / ("agy-http.exe" if os.name == "nt" else "agy-http"))
            sources = [root / "build.py", root / "go.mod", *root.rglob("*.go")]
            if not binary.is_file() or (not os.environ.get("AGY_HTTP_BRIDGE") and
                                       any(p.stat().st_mtime > binary.stat().st_mtime for p in sources)):
                build = await asyncio.create_subprocess_exec(sys.executable, str(root / "build.py"), stdout=asyncio.subprocess.DEVNULL)
                if await build.wait():
                    raise RuntimeError("原生 HTTP 传输构建失败，请用 Go 1.27.4 运行 python native/build.py")
            self.process = await asyncio.create_subprocess_exec(
                str(binary), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                self.address = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 10))
                if self.local is None:
                    self.local = httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(
                        max_connections=int(os.getenv("HTTPX_MAX_CONNECTIONS", "128")),
                        max_keepalive_connections=int(os.getenv("HTTPX_MAX_KEEPALIVE_CONNECTIONS", "8")),
                        keepalive_expiry=float(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "30")),
                    ))
            except Exception:
                await self.close()
                raise RuntimeError("原生 HTTP 传输启动失败") from None
            return self.address

    async def close(self):
        if self.local:
            await self.local.aclose()
            self.local = None
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            await self.process.wait()
        self.process = None
        self.address = None


bridge = NativeBridge()


class NativeTransport(httpx.AsyncBaseTransport):
    def __init__(self, account_id, proxy):
        self.account_id = account_id
        self.proxy = proxy or ""

    async def handle_async_request(self, request):
        address = await bridge.start()
        headers = request.headers.copy()
        headers.update({
            "X-Gcli-Key": address["key"], "X-Gcli-Target": str(request.url),
            "X-Gcli-Account": self.account_id, "X-Gcli-Proxy": self.proxy,
        })
        local_request = httpx.Request(request.method, address["url"], headers=headers,
                                     stream=request.stream, extensions=request.extensions)
        try:
            response = await bridge.local.handle_async_request(local_request)
        except httpx.TransportError as exc:
            raise httpx.ConnectError("本地原生 HTTP 传输连接失败", request=request) from exc
        response.request = request
        return response

    async def aclose(self):
        pass  # The bridge owns the shared loopback pool.
