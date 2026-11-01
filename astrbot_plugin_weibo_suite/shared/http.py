"""SSRF 防护工具。

插件自身发起的所有网络请求（图床上传、图片下载兜底）必须经过本模块：

- resolve_public_addrs 解析 URL 主机并校验：仅允许 http/https 协议，且主机
  解析到的全部 IP 不得指向环回、私有、链路本地、保留、多播等非公网地址，
  校验通过时把已确认公网的地址一并返回；
- guarded_request 在此基础上发送请求：禁用 aiohttp 自动重定向，手动逐跳跟随
  并对每一跳重新校验；校验得到的 IP 通过 PinnedResolver 钉扎给 aiohttp 的连接
  阶段，连接时不再二次解析域名，从而堵住“校验时解析到公网、连接时解析到内网”
  的 DNS rebinding 绕过。

使用本模块的会话必须由 make_pinned_connector() 创建连接器，否则 PinnedResolver
不会生效（此时仅剩逐跳校验，rebinding 仍可绕过）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

USER_AGENT = "AstrBot-Plugin-WeiboSuite/1.0"
PrepareCallback = Callable[[str, str, bool], dict]


def _header_int(value: object) -> int | None:
    """Parse a non-negative HTTP integer header, returning None when absent/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class UrlBlockedError(Exception):
    """URL 未通过安全校验。"""


class ResponseTooLargeError(Exception):
    """响应体超过允许的大小上限。"""


# 单次响应的默认读取上限：本插件的接口都是小型 JSON，1 MiB 足够，
# 避免异常或被劫持的端点用超大响应体耗尽内存。
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024

# 跨域重定向时强制剥离的请求头（prepare 回调之外的兜底保护）
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        "token",
        "x-token",
        "x-auth-token",
        "cookie",
    }
)


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> None:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global  # 覆盖 100.64.0.0/10(CGNAT)、文档地址段等"非全局可路由"地址
    ):
        raise UrlBlockedError(f"已拒绝指向非公网地址的请求: {host} -> {ip}")


# 允许解析到内网/私网地址的主机白名单：仅由管理员通过插件配置注入（见
# set_allowed_private_hosts），用于图床与 AstrBot 同机/同内网部署时走内网直连
# （random_media.lan_base_url）。白名单只豁免「私网地址拒绝」这一步：地址仍会
# 逐个解析并钉扎给连接阶段，重定向的每一跳、以及其他一切主机不受影响。
_ALLOWED_PRIVATE_HOSTS: set[str] = set()


def set_allowed_private_hosts(hosts) -> None:
    """整体替换「允许内网地址」的主机白名单（主机名小写、不含端口）。

    以主机名（而非 IP）入白名单：解析结果仍走原有流程（含钉扎），只是不再
    因落在私网段被拒。传空集合即恢复默认的全拒绝，插件卸载时应如此清理。

    历史遗留 / 兼容接口：这是模块级可变全局状态，多实例场景下谁最后调用谁
    生效。新代码请把集合按实例传给 GuardedHttpClient / guarded_request，让
    「是否允许内网」由调用方当场决定，不要依赖这里。
    """
    global _ALLOWED_PRIVATE_HOSTS
    _ALLOWED_PRIVATE_HOSTS = {str(h).strip().lower() for h in hosts if str(h or "").strip()}


# 「不豁免任何私网地址」的显式默认值：调用方应传它而不是留空依赖模块级全局。
# 留空的语义是"沿用 set_allowed_private_hosts 设置的历史全局"，一旦某个实例
# 调过它，同进程内所有未显式传参的请求都会跟着放宽——SSRF 保护随模块状态漂移。
DENY_PRIVATE_HOSTS: frozenset[str] = frozenset()


def _url_port(parsed) -> int:
    """取 URL 端口；缺省按协议补全，非法端口统一转为 UrlBlockedError。"""
    try:
        port = parsed.port
    except ValueError as e:
        raise UrlBlockedError(f"URL 端口不合法: {parsed.netloc}: {e}") from e
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise UrlBlockedError(f"URL 端口超出范围: {port}")
    return port


def _url_origin(url: str) -> str:
    """归一化的源标识（小写主机 + 实际端口），用于判断是否跨域。"""
    parsed = urlparse(url)
    return f"{(parsed.hostname or '').lower()}:{_url_port(parsed)}"


def resolve_public_addrs(
    url: str, allowed_private_hosts: set[str] | None = None
) -> tuple[str, list[ResolveResult]]:
    """校验 URL 并解析主机。返回 (主机名, 已确认公网的地址列表)。

    主机为 IP 字面量时同样返回一份已校验地址，调用方据此钉扎——
    不依赖 aiohttp 对 IP 直连跳过 resolver 的内部实现行为，升级后
    仍保持"未钉扎即拒绝"的 fail-closed 语义。

    allowed_private_hosts 未传（None）时使用模块级全局白名单——仅为兼容既有
    调用方与测试；新代码请显式传入集合（空集合即 deny-by-default），
    GuardedHttpClient 也已按实例携带自己的集合，避免多个插件实例通过全局
    变量互相覆盖。
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UrlBlockedError(f"URL 解析失败: {e}") from e

    if parsed.scheme not in ("http", "https"):
        raise UrlBlockedError(f"仅允许 http/https 协议，收到: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise UrlBlockedError("URL 缺少主机名")

    # 管理员配置的内网直连主机（如 lan_base_url 的主机）豁免私网地址拒绝；
    # 其余主机维持 fail-closed：解析到内网地址即拒绝
    allow_set = _ALLOWED_PRIVATE_HOSTS if allowed_private_hosts is None else allowed_private_hosts
    allow_private = host.lower() in allow_set

    # 端口合法性先于任何放行判断，避免 http://1.2.3.4:99999/ 之类绕过
    port = _url_port(parsed)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not allow_private:
            _check_ip(ip, host)
        return host, [
            ResolveResult(
                hostname=host,
                host=str(ip),
                port=port,
                family=socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as e:
        raise UrlBlockedError(f"域名解析失败: {host}: {e}") from e
    if not infos:
        raise UrlBlockedError(f"域名解析结果为空: {host}")

    addrs: list[ResolveResult] = []
    for family, _type, proto, _canon, sockaddr in infos:
        ip_str = str(sockaddr[0])
        try:
            resolved = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise UrlBlockedError(f"域名解析到非法地址: {host}") from e
        if not allow_private:
            _check_ip(resolved, host)
        addrs.append(
            ResolveResult(
                hostname=host,
                host=ip_str,
                port=int(sockaddr[1]) if len(sockaddr) > 1 else port,
                family=family,
                proto=proto,
                flags=0,
            )
        )
    return host, addrs


# 当前请求钉扎的地址：{小写主机名: [已校验的 ResolveResult]}。
# 用 ContextVar 承载，使并发的不同请求互不干扰。
_PINS: ContextVar[Mapping[str, list[ResolveResult]]] = ContextVar(
    "url_guard_pins", default=MappingProxyType({})
)


class PinnedResolver(AbstractResolver):
    """只返回本次请求已校验过的 IP；未钉扎的主机一律拒绝（fail-closed）。"""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[ResolveResult]:
        addrs = _PINS.get().get(host.lower())
        if not addrs:
            raise OSError(f"目标主机未经过 SSRF 校验，已拒绝连接: {host}")
        return addrs

    async def close(self) -> None:
        return None


def make_pinned_connector(**kwargs: Any) -> aiohttp.TCPConnector:
    """创建启用 IP 钉扎的连接器；插件所有出网会话都应使用它。

    关闭 aiohttp 自带的 DNS 缓存：每个新连接都重新走钉扎逻辑，连接池复用则
    只可能复用此前已校验过的地址。
    """
    kwargs.setdefault("use_dns_cache", False)
    return aiohttp.TCPConnector(resolver=PinnedResolver(), **kwargs)


def prepare_headers(
    prepare: Callable[[str, str, bool], dict] | None,
    url: str,
    method: str,
    cross_origin: bool,
) -> dict[str, Any]:
    """调用 prepare 回调取得请求参数；跨域重定向时剥离凭据类请求头。

    prepare 契约：prepare(url, method, cross_origin) -> dict，其中
    cross_origin 为 True 表示当前跳与首跳不同源，回调自身也应据此不再
    附带鉴权信息（此处的剥离仅针对常见凭据头名做兜底）。
    """
    if prepare is None:
        return {}
    kwargs = dict(prepare(url, method, cross_origin) or {})
    if cross_origin:
        headers = kwargs.get("headers")
        if headers:
            kwargs["headers"] = {
                k: v for k, v in headers.items() if str(k).lower() not in _CREDENTIAL_HEADERS
            }
    return kwargs


async def read_limited_bytes(
    resp: aiohttp.ClientResponse, limit: int = DEFAULT_MAX_RESPONSE_BYTES
) -> bytes:
    """读取完整响应体，超过 limit 立即中止，避免异常端点撑爆内存。

    aiohttp 的 content.read(n) 只承诺"最多 n 字节"：缓冲区里已有多少就
    返回多少、并不等待读满，单次调用会把分块到达的较大响应体静默截断
    （线上曾因此把 39841 字节的 embedding 响应截成 3893 字节），
    必须循环读到 EOF；响应中途断流由 aiohttp 帧校验抛 ClientPayloadError。
    """
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ResponseTooLargeError(f"响应体声明长度 {declared} 超过上限 {limit} 字节")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await resp.content.read(limit + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLargeError(f"响应体超过上限 {limit} 字节")
    return b"".join(chunks)


async def read_limited_text(
    resp: aiohttp.ClientResponse, limit: int = DEFAULT_MAX_RESPONSE_BYTES
) -> str:
    """按响应声明的字符集读取文本，超过 limit 立即中止。"""
    raw = await read_limited_bytes(resp, limit)
    try:
        encoding = resp.get_encoding() or "utf-8"
    except (LookupError, ValueError, RuntimeError):
        # RuntimeError：为限长，响应体由 read_limited_bytes 手动流式读取，
        # aiohttp 的 _body 仍为空，get_encoding 在头部未声明 charset 时
        # 无法从响应体探测编码（application/json 已默认 utf-8，不会走到
        # 这里）；此时头部必无 charset，按 utf-8 解码即可。
        encoding = "utf-8"
    return raw.decode(encoding, errors="replace")


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def guarded_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    max_redirects: int = 3,
    prepare: Callable[[str, str, bool], dict] | None = None,
    allowed_private_hosts: set[str] | None = None,
) -> AbstractAsyncContextManager[aiohttp.ClientResponse]:
    """发起经 SSRF 校验的 HTTP 请求，手动跟随重定向并对每一跳重新校验。

    aiohttp 默认自动跟随重定向，只校验首跳 URL 挡不住
    "公网地址 302 跳内网" 的绕过，因此这里禁用自动重定向、逐跳校验；
    并且每一跳都把校验过的 IP 钉扎给连接阶段，防止 DNS rebinding。

    prepare: 可选回调 prepare(url, method, cross_origin) -> dict，用于生成
    每次请求的额外参数（如 FormData、headers）；重定向后方法变化或跨域时会
    重新生成，回调应据 cross_origin 决定是否附带鉴权信息。
    返回最终的（非重定向）响应对象，由调用方负责关闭。
    """
    origin = _url_origin(url)
    current = url
    for hop in range(max_redirects + 1):
        if allowed_private_hosts is None:
            host, addrs = await asyncio.to_thread(resolve_public_addrs, current)
        else:
            host, addrs = await asyncio.to_thread(
                resolve_public_addrs, current, allowed_private_hosts
            )
        cross_origin = _url_origin(current) != origin
        kwargs = prepare_headers(prepare, current, method, cross_origin)

        pins = dict(_PINS.get())
        if addrs:
            pins[host.lower()] = addrs
        token = _PINS.set(pins)
        try:
            resp = await session.request(method, current, allow_redirects=False, **kwargs)
        finally:
            _PINS.reset(token)

        if resp.status not in REDIRECT_STATUSES:
            return resp
        location = resp.headers.get("Location", "")
        # aiohttp 的 ClientResponse.release() 是同步方法（返回 None），不能 await；
        # 早期测试替身把它写成 async，掩盖了真实环境里「跟一次重定向就 TypeError」。
        resp.release()
        if not location or hop == max_redirects:
            raise UrlBlockedError(f"重定向无效或次数超过上限 {max_redirects}: {url}")
        current = urljoin(current, location)
        if resp.status in (301, 302, 303):
            method = "GET"


class GuardedHttpClient:
    """Own one pinned ``aiohttp`` session for all plugin HTTP requests."""

    def __init__(self, allowed_private_hosts: set[str] | None = None) -> None:
        self._session: aiohttp.ClientSession | None = None
        # 同一个集合对象由调用方（插件实例）持有并原地更新；请求时按实例白名单
        # 豁免私网地址，不依赖 url_guard 的模块级全局集合
        self.allowed_private_hosts = allowed_private_hosts

    async def request(
        self,
        method: str,
        url: str,
        *,
        prepare: PrepareCallback | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> aiohttp.ClientResponse:
        """Run a guarded request and return the final response context."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                connector=make_pinned_connector(),
            )

        def wrapped_prepare(current_url: str, current_method: str, cross_origin: bool) -> dict:
            kwargs = (
                dict(prepare(current_url, current_method, cross_origin) or {}) if prepare else {}
            )
            if timeout is not None:
                kwargs["timeout"] = timeout
            return kwargs

        return await guarded_request(
            self._session,
            method,
            url,
            prepare=wrapped_prepare,
            **(
                {"allowed_private_hosts": self.allowed_private_hosts}
                if self.allowed_private_hosts is not None
                else {}
            ),
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


HttpClientGetter = Callable[[], Awaitable[GuardedHttpClient]]


async def stream_to_file(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    prepare: Callable[[str, str, bool], dict] | None = None,
    allowed_private_hosts: set[str] | frozenset[str] | None = DENY_PRIVATE_HOSTS,
    chunk_size: int = 64 * 1024,
) -> int:
    """Download one guarded response into ``destination`` atomically.

    The implementation rejects declared or observed oversize bodies, HTML error
    disguises and truncated bodies. Data is written to ``.part`` first and only
    atomically replaces the destination after a complete successful read.

    磁盘操作（建目录、逐块写、flush/fsync、原子改名、清理 .part）都放进
    ``asyncio.to_thread``，await 本函数不会阻塞事件循环。

    allowed_private_hosts 默认是空集合（deny-by-default），不读取模块级
    全局白名单：调用方要放行内网图床必须显式传集合，避免 SSRF 拦截强度
    取决于"别的实例有没有调过 set_allowed_private_hosts"。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    destination = Path(destination)
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    response = await guarded_request(
        session,
        method,
        url,
        prepare=prepare,
        **(
            {"allowed_private_hosts": allowed_private_hosts}
            if allowed_private_hosts is not None
            else {}
        ),
    )
    try:
        if response.status < 200 or response.status >= 300:
            raise aiohttp.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message=f"HTTP {response.status}",
                headers=response.headers,
            )
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if content_type.startswith(("text/html", "application/xhtml+xml")):
            raise ValueError("远端返回 HTML 而非媒体文件")
        declared = _header_int(response.headers.get("Content-Length"))
        if declared is not None and declared > max_bytes:
            raise ResponseTooLargeError(f"响应声明大小 {declared} 超过上限 {max_bytes}")
        total = 0
        try:
            with part.open("wb") as handle:
                async for chunk in response.content.iter_chunked(chunk_size):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(f"响应实际大小超过上限 {max_bytes}")
                    # 写盘放线程：慢盘上单块 write 也可能阻塞事件循环
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(part.unlink)
            raise
        encoded = str(response.headers.get("Content-Encoding", "")).strip().lower()
        if declared is not None and not encoded and total != declared:
            raise ValueError(f"响应被截断：收到 {total}/{declared} 字节")
        await asyncio.to_thread(part.replace, destination)
        return total
    finally:
        response.release()
