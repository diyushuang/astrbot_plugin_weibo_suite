"""插件内部共享的小工具与常量。

只放无业务语义的通用件（配置解析、User-Agent、常量），供 main /
image_bed / vector_search / s3_store 取用，避免同一份真值表与解析
逻辑在各模块各写一份。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

USER_AGENT = "AstrBot-Plugin-WeiboSuite/1.0"

# 查询图兜底下载的超时（秒）。vector_search / image_bed 的请求超时各自
# 可配置，下载兜底路径没有独立配置项，用固定值即可。
DOWNLOAD_TIMEOUT = 30

# 下载兜底路径的分块大小（字节）
DOWNLOAD_CHUNK_SIZE = 65536

# 探测（HEAD / Range）专用超时（秒）。探测只读响应头，与「下载整张图」的
# DOWNLOAD_TIMEOUT 完全不是一回事：此前两者共用 30s，一次卡住的探测就能把
# 整次交付拖到分钟级。自建图床实时缩放较慢时 5s 仍有余量。
PROBE_TIMEOUT = 5

# 单次回传计划的探测总预算（秒）。超出预算仍未返回的探测一律按「无法判定」
# 处理（配文不写「已压缩」），绝不让探测无限期拖住图片发送。
PLAN_TOTAL_TIMEOUT = 8

# 探测并发上限。图床多为自建单机，并发过高会挤占其处理能力；
# 4 足够把一批图的探测压到约一轮往返。
PROBE_CONCURRENCY = 4

# 探测到「图床当前处理不了缩放请求」（API 级拒绝，如缩放请求非 GET 的 405、
# 未配置图片处理器的 501）后的冷却时间（秒）。冷却期内不再为「已压缩」证据
# 探测缩放版，避免每次交付都白花一次请求。
SCALED_VERIFY_COOLDOWN_SECONDS = 600

# QQ URL 上传通道失败后的冷却时间（秒）。图床公网域名存在、但腾讯侧拉不到
# （域名已停用、防盗链、防火墙）时，每一次交付都会先撞一次必败的 URL 上传，
# 白等一轮往返；冷却期内 auto 模式直接走分片。
QQ_URL_COOLDOWN_SECONDS = 600

# 本地压缩并发上限。Pillow 编解码期间释放 GIL，线程并行确有收益；
# 上限用于压住低配机器的 CPU 峰值。
COMPRESS_CONCURRENCY = 3

# 回退下载优先走内存的上限（字节）：超过则落盘，避免多张大图并发时内存峰值过高。
MEMORY_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024

# OneBot image 段的 timeout 字段（秒）：URL 段由协议端（NapCat 等）自行下载，
# 该值即协议端下载网络图片的超时。OneBot v11 规范里此参数默认「不超时」，但
# NapCat 实际另有 sendMsg 内部回调窗口；大图/多图时显式放宽可减少回调超时
# （表现为 retcode 1200 的 sendMsg Timeout，结果不确定因而不敢重发）。
# base64 段不经下载，带该字段无副作用。
ONEBOT_IMAGE_TIMEOUT = 60

# 临时文件最长保留时间（秒），超过后在插件加载/卸载时清理
TMP_MAX_AGE_SECONDS = 86400

# AI 复核最多送审的候选数
AI_VERIFY_MAX_CANDIDATES = 3

# 重扫批量入库的批次大小（单事务条数）
RESCAN_BATCH_SIZE = 500

# 重扫的进度汇报间隔（张数）
RESCAN_PROGRESS_EVERY = 200

# 重扫的并发解码上限。Pillow 解码期间释放 GIL，线程并行确有收益；取 2 是为了
# 压住大图（单图最高 50MP）并发时的内存峰值，24k 级图库仍约有一倍提速。
SCAN_CONCURRENCY = 2

# 字符串型布尔值的真值表；"0"/"false"/"no"/"off" 等一律按假处理
_TRUTHY = frozenset({"1", "true", "yes", "on", "开"})


def is_qq_image_bed_host(host: str) -> bool:
    """精确匹配 QQ 图床根域，避免伪造后缀误触发 Referer。"""
    normalized = (host or "").lower().rstrip(".")
    return normalized in {
        "multimedia.nt.qq.com.cn",
        "gchat.qpic.cn",
    } or normalized.endswith((".nt.qq.com.cn", ".qpic.cn"))


def truthy(value: Any) -> bool:
    """按布尔语义解释配置值：bool 原样返回，其余查真值表。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def is_blank(value: Any) -> bool:
    """未配置：None 或空白字符串。0 / False 是合法取值，不能按未配置处理。"""
    return value is None or (isinstance(value, str) and not value.strip())


def bool_or(value: Any, default: bool) -> bool:
    """布尔配置：未配置（None/空白）时取默认，其余按真值表解释。

    与 ``value or default`` 的区别：0 / False 是合法配置，不能被当成未配置；
    这条口径原先在 main._bool_cfg 与随机图布尔解析里各写一份。
    """
    return default if is_blank(value) else truthy(value)


def read_bytes(path: str) -> bytes:
    """读取整个文件；同步 IO，异步调用方应走 asyncio.to_thread。"""
    with open(path, "rb") as file:
        return file.read()


def as_int(value: Any, default: int) -> int:
    """配置取整型；未配置或类型不合法时返回默认值。"""
    if is_blank(value):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    """配置取浮点型；未配置或类型不合法时返回默认值。"""
    if is_blank(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 用户可见文案里的 URL / 裸 IPv4：错误详情只进日志，不能把内网地址带进群聊
ADDRESS_PLACEHOLDER = "<地址已隐藏>"
CREDENTIAL_PLACEHOLDER = "<凭据已隐藏>"
_URL_IN_TEXT_RE = re.compile(r"(?:https?|ftp)://[^\s'\"<>，。；：、）】]+", re.IGNORECASE)
_IPV4_IN_TEXT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")

# 裸 key=value（没有 scheme 的签名串、被网关回显进异常文本的 token）：URL 规则
# 只认 scheme，`X-Amz-Signature=...` 这类纯文本串会整段漏过去。这里只打等号右侧
# 的值、保留键名，便于定位是哪一项凭据。键名白名单保持保守，且只认 `=`——
# `key: value` 多是正常叙述（如 "auth: failed"），按冒号匹配误伤面太大。
_SECRET_KEY_NAMES = (
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "signature",
    "sign",
    "secret",
    "password",
    "pwd",
    "token",
    "auth",
)
# 值至少 4 个字符才打码：1-3 字符基本是 `token=x` 这类占位/示例，打掉只会让
# 诊断信息消失，也扛不住真正的凭据熵。
_SECRET_KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(" + "|".join(_SECRET_KEY_NAMES) + r")="
    r"(?:'([^'\s<>]{4,})'|\"([^\"\s<>]{4,})\"|([^\s&'\"<>，。；：、）】]{4,}))"
)


def _hide_secret_value(match: re.Match[str]) -> str:
    """保留键名与引号，只替换等号右侧的值。"""
    key = match.group(1)
    if match.group(2) is not None:
        return f"{key}='{CREDENTIAL_PLACEHOLDER}'"
    if match.group(3) is not None:
        return f'{key}="{CREDENTIAL_PLACEHOLDER}"'
    return f"{key}={CREDENTIAL_PLACEHOLDER}"


def sanitize_error_text(value: Any, limit: int | None = 200) -> str:
    """把异常文本收敛成可进群聊的短句：隐藏 URL、IP 与裸凭据键值，并截断长度。

    仅用于**用户可见**的错误提示；完整异常仍应写日志。管理员配置的图床
    地址可能含内网 IP 或带签名的查询串，异常字符串常把它们原样拼进去。
    顺序固定为「URL/IP → 裸 key=value」：先整条隐藏带 scheme 的地址，键值规则
    就不会再把它切碎成占位符套占位符的形状。

    ``limit=None`` 供需要按原文长度对照位置的调用方使用（如 embedding 诊断
    片段）：裸凭据替换会改写文本长度，按原文长度截断会切掉尾部、让位置映射
    落空。
    """
    text = str(value or "").strip()
    if not text:
        return "未知错误（详情见机器人日志）"
    text = _URL_IN_TEXT_RE.sub(ADDRESS_PLACEHOLDER, text)
    text = _IPV4_IN_TEXT_RE.sub(ADDRESS_PLACEHOLDER, text)
    text = _SECRET_KV_RE.sub(_hide_secret_value, text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def redact_proxy(url: str) -> str:
    """Hide credentials in a proxy URL before it reaches logs or the UI."""
    text = str(url or "")
    parsed = urlparse(text)
    if not (parsed.username or parsed.password):
        # 没有 scheme 的 `user:pass@host` 会被 urlparse 整串塞进 path，
        # 这里按最后一个 @ 兜底脱敏，避免代理密码进日志/错误面板。
        if "@" in text:
            return f"***@{text.rsplit('@', 1)[-1]}"
        return text
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(parsed._replace(netloc=f"***@{host}"))


def parse_umo(umo: str) -> tuple[str, str, str]:
    """Split AstrBot unified_msg_origin: platform_id:message_type:session_id."""
    parts = (umo or "").split(":", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else ("", "", "")


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse ``k1=v1; k2=v2`` into a dictionary, preserving the last duplicate."""
    cookies: dict[str, str] = {}
    for part in (raw or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            cookies[key.strip()] = value.strip()
    return cookies


def merge_response_cookies(target: dict[str, str], response: Any) -> None:
    """Merge Set-Cookie values from an aiohttp response, ignoring foreign domains."""
    for key, morsel in response.cookies.items():
        domain = morsel.get("domain") or ""
        if domain and "weibo.cn" not in domain and "weibo.com" not in domain:
            continue
        target[str(key)] = str(morsel.value)


def header_int(raw: Any) -> int:
    """Parse an HTTP integer header; missing or invalid values become zero."""
    try:
        return int(str(raw or "").strip())
    except (TypeError, ValueError):
        return 0


def jsonp_data(body: str) -> dict[str, Any] | None:
    """Parse a JSONP response such as ``callback({...})``."""
    match = re.search(r"\((\{.*\})\)\s*;?\s*$", (body or "").strip())
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def album_rule_token(uid: str, gid: str, album: str) -> str:
    """Serialize one album routing rule; the album tail may contain colons."""
    return f"{uid}:{gid}:{album}"


def keyword_rules_upsert(
    rules: list[tuple[str | None, str, str, str]],
    uid: str | None,
    keyword: str,
    gid: str,
    album: str,
) -> bool:
    """Insert or replace a keyword route. Return True when an existing rule changed."""
    for index, (scope_uid, existing_keyword, existing_gid, _album) in enumerate(rules):
        if (scope_uid, existing_keyword, existing_gid) == (uid, keyword, gid):
            rules[index] = (uid, keyword, gid, album)
            return True
    rules.append((uid, keyword, gid, album))
    return False


def cooldown_remaining_minutes(until_ts: float, now: float | None = None) -> int:
    """Whole minutes left in a cooldown, rounded up; zero once it has expired."""
    current = time.time() if now is None else now
    remaining = float(until_ts or 0) - current
    return max(0, int((remaining + 59.999999) // 60))
