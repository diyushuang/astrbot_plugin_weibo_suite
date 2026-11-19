"""图床/对象存储共用的小工具与结果类型。

不含任何网络状态，也不 import `image_bed`——三个存储后端（本地、CloudFlare-
ImgBed、R2/OCI）与门面都从这里取纯函数，避免形成环。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlparse

# 上传重试：图床/对象存储的失败大多是瞬时的（网关 502、连接重置、限流），
# 一次失败就永久降级到本地副本，会让"图床其实还能用"的用户长期只拿到本地
# 路径（且只在第一次告警，后面再也看不出来）。
MODE_LOCAL = "local"
MODE_HTTP = "generic_http"
MODE_R2 = "cloudflare_r2"
MODE_OCI = "oracle_oci"
MODE_CFB = "cloudflare_imgbed"

UPLOAD_RETRY_ATTEMPTS = 3
UPLOAD_RETRY_BASE_DELAY = 0.25  # 秒；指数退避 0.25 / 0.5
# 这些状态码重试没有意义：鉴权、权限、请求体本身有问题，重试只是把同一个
# 错误再犯三遍，还让 403 的告警变成三倍噪音
UPLOAD_TERMINAL_STATUS = frozenset({400, 401, 403, 404, 413, 415, 422})
_UPLOAD_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


@dataclass
class StoreResult:
    ok: bool
    url: str | None = None  # 图床直链（若有）
    file_path: str | None = None  # 本地文件路径（若有）
    message: str = ""


def extract_json_path(data: Any, path: str) -> Any:
    """按点号路径从嵌套 JSON 中取值，支持数组下标（如 data.links.0.url）。

    约定：键名含 "." 无法表达；任何一段取不到（键缺失/下标越界/类型
    不符）即返回 None；列表支持负下标（current[-1] 取末元素）。
    """
    current = data
    for part in path.split("."):
        part = part.strip()
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def upload_error_status(exc: BaseException) -> int:
    """从上传异常里抠出 HTTP 状态码；没有就返回 0。

    上传器统一抛 RuntimeError(f"... HTTP {status}: ...")，这里按同一口径解析。
    """
    match = _UPLOAD_HTTP_STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else 0


def is_retryable_upload_error(exc: BaseException) -> bool:
    """这个上传异常是否值得重试。

    - 有 HTTP 状态码：只有非 4xx-终态（鉴权/权限/请求体）才重试，5xx 与 429 重试；
    - 无状态码：ValueError 属于配置/路径错误（如未配置 api_url、目录非法），
      重试必然同样失败；其余（连接重置、超时、响应解析）按瞬时故障重试。
    """
    status = upload_error_status(exc)
    if status:
        return status not in UPLOAD_TERMINAL_STATUS
    return not isinstance(exc, (ValueError, NotImplementedError))


def public_url(base: str, key: str) -> str:
    """拼接公开直链：base 去尾斜杠 + URI 编码后的对象名。"""
    return f"{base.rstrip('/')}/{quote(key)}"


# 兼容旧调用方/测试里的私有名
_public_url = public_url


def sha256_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    """分块计算文件 sha256（调用方应放进 to_thread，避免阻塞事件循环）。

    对象存储的 `x-amz-content-sha256` 必须与实际请求体一致；为了不算错，
    原实现把整个文件读成 bytes。大文件改成分块摘要 + 文件句柄流式上传，
    内存占用与文件大小无关。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def redact_url(url: str) -> str:
    """把带签名的直链收敛成可安全写日志的形态。

    对象存储/图床的直链常带 `?...sign=...&token=...` 这类一次性凭据：
    整条写进日志后，任何能看日志或 /errors 面板的人都拿到了可直接使用的
    下载权限。这里只保留 scheme://host/path，查询串一律换成 `?***`。
    """
    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return "<地址已隐藏>"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return text
    redacted = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{redacted}?***" if parsed.query else redacted


def bed_file_id(url: str) -> str:
    """从图床直链里取出文件 id（即 S3 对象键）。

    图床直链形如 `{base}/file/{id}`，id 可能含中文与 `@`/`#`（故 URL 里是
    百分号编码的），必须 unquote 还原成原始键才能算出同一个点 id。取不到
    返回空串，由调用方按「无替身」处理。
    """
    path = urlparse(str(url or "")).path
    marker = "/file/"
    index = path.find(marker)
    if index < 0:
        return ""
    return unquote(path[index + len(marker) :]).strip()


def host_port(parsed) -> tuple[str, int | None]:
    """URL 的归一化 (主机名小写, 端口)，端口缺省按协议补全。"""
    try:
        port = parsed.port
    except ValueError:
        return "", None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname or "").lower(), port


def same_origin(a, b) -> bool:
    """两个 parsed URL 是否同源（协议 + 主机 + 端口）。"""
    if a.scheme != b.scheme:
        return False
    return host_port(a) == host_port(b)


def clean_folder(folder: object) -> str:
    """图床内目录名归一化：去首尾空白与首尾 "/"。

    store(folder=...) 的四种模式共用这一份归一化：CFB 的 uploadFolder、
    R2/OCI 的对象 key 前缀都按「无首尾斜杠」的形态拼接；空值表示沿用
    各模式现状（CFB 回落到 cfi_upload_folder、对象存储不加前缀）。
    """
    return str(folder or "").strip().strip("/").strip()


# 兼容旧调用方/测试里的私有名
_host_port = host_port
_same_origin = same_origin
_clean_folder = clean_folder
