"""NapCat/OneBot 错误分类与负载辅助工具。"""

from __future__ import annotations

import base64
import re

# "协议端没有这个接口"的真实文案：NapCat 对未实现的 action 抛 `不支持的API <action>`(retcode 1404)。
_ACTION_MISSING_HINTS = (
    "不支持的api",
    "unknown api",
    "unknown action",
    "invalid action",
    "no such action",
    "action not exist",
    "method not found",
    "未实现",
    "不存在该接口",
    "无此接口",
)
# 只有"载荷取不到"才值得换下一种载荷方式。
# 注意不能写裸的 "no such"：NapCat 读不到本地文件时抛的正是
# `ENOENT: no such file or directory`，那是 NapCat 与 AstrBot 不同机的正常信号，不是接口缺失。
_FILE_UNUSABLE_HINTS = (
    "no such file",
    "enoent",
    "failed to read file",
    "cannot find the file",
    "not a directory",
    "文件不存在",
    "无法读取",
    "没有那个文件或目录",
)
# NapCat 传相册不是走内核，而是自己 fetch h5.qzone.qq.com 串行发 16KB 分片
# （napcat-core/apis/webapi.ts 的 uploadQunAlbumSlice），非 2xx 就抛
# `HTTP error! status: 502`，OneBot 侧统一包成 retcode=1200。
# 这种是 QQ 相册网关临时挡人，跟插件这边怎么传没关系，只能退避重试。
_UPSTREAM_HINTS = (
    "http error",
    "bad gateway",
    "service unavailable",
    "gateway time",
    "internal server error",
)
# 走 AstrBot 的 OneBot 连接时 NapCat 只会给出 1400/1200/1404 这类码，
# 语义靠 message 判断比靠 retcode 可靠。
_HINT_RULES = (
    (
        _UPSTREAM_HINTS,
        "QQ 相册网关临时报错，NapCat 传分片被挡，等下一批微博再看",
    ),
    (
        ("permission", "forbidden", "not allowed", "权限"),
        "权限不足：请确认机器人在本群被允许上传相册",
    ),
    (("album", "相册"), "相册可能已被删除或 ID 有误，用 微博列相册 重新确认"),
)
# 值得退避重试的瞬时故障：QQ 相册网关抖动 + 连接层面的问题 + 频控。
_RETRYABLE_HINTS = (
    *_UPSTREAM_HINTS,
    "socket hang up",
    "connection reset",
    "connection refused",
    "connection aborted",
    "remote end closed",
    "other side closed",
    "fetch failed",
    "econnreset",
    "etimedout",
    "econnrefused",
    "network",
    "频繁",
    "重试",
    "超时",
    "timeout",
    "frequent",
    "busy",
    "系统繁忙",
    "稍后",
    "网关",
    "网络",
)
# 值得重试的 HTTP 码：5xx 是对方服务器的事，408/429 是"等一下就好"。
# 401/403/404 不在此列——那是 api_root 或 token 配错了，重试只是白等。
_RETRY_STATUS = frozenset({408, 429, *range(500, 600)})
_HTTP_STATUS_RE = re.compile(r"(?:status|code|状态码)\D{0,3}(\d{3})\b", re.I)
# 连不上 NapCat / 等不到响应这一类异常没有响应体可读，只能按类型名认：
# AstrBot 用的是 aiocqhttp 反向 WS（api_timeout_sec=180），超时抛 NetworkError，
# 它继承 IOError，所以 OSError 一并覆盖了 socket 层面的各种断连。
_TRANSPORT_ERRORS = frozenset(
    {
        "NetworkError",
        "HttpFailed",
        "TimeoutError",
        "OSError",
        "ClientError",
        "ServerDisconnectedError",
    }
)


class NapCatError(Exception):
    """带分类标记的错误，供上层决定"换个方式再试"还是"直接放弃"。"""

    def __init__(
        self,
        message: str = "",
        *,
        action_missing: bool = False,
        file_unusable: bool = False,
    ):
        super().__init__(message)
        self.action_missing = action_missing
        self.file_unusable = file_unusable


def _norm(name: str) -> str:
    return re.sub(r"[\s　]+", "", str(name or "")).casefold()


def _has(msg: str, hints: tuple[str, ...]) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in hints)


def pick(d: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        # 只把 None / 空串当缺失：数值 0 是合法取值，吞掉会把 ID 为 0 的条目丢掉
        if v is not None and v != "":
            return str(v)
    return default


def _payload(e: BaseException) -> dict:
    """NapCat 的原始响应体。

    aiocqhttp 1.4 起 ActionFailed 把它挂在 `.result`（1.3 及更早叫 `.info`），
    而 `str(e)` 只剩 `<ActionFailed status='failed', retcode=1200, ...>` 这个壳，
    retcode 与 message 都在响应体里，读错属性就只能把整个壳当文案抛给用户。
    """
    for attr in ("result", "info"):
        v = getattr(e, attr, None)
        if isinstance(v, dict):
            return v
    return {}


def _transient(msg: str, e: BaseException | None = None) -> bool:
    """这条错误是瞬时的吗（QQ 相册网关 5xx、连接抖动、频控）—— 只有这类值得退避重试。"""
    if _has(msg, _RETRYABLE_HINTS):
        return True
    m = _HTTP_STATUS_RE.search(msg or "")
    if m and int(m.group(1)) in _RETRY_STATUS:
        return True
    if e is None:
        return False
    status = getattr(e, "status_code", None)  # aiocqhttp HttpFailed：HTTP 通道的响应码
    if isinstance(status, int):
        return status in _RETRY_STATUS
    return any(c.__name__ in _TRANSPORT_ERRORS for c in type(e).__mro__)


def _classify(text: str) -> tuple[str, bool, bool]:
    """返回 (带提示的文案, 是否接口缺失, 是否载荷不可用)。"""
    missing = _has(text, _ACTION_MISSING_HINTS)
    unusable = _has(text, _FILE_UNUSABLE_HINTS)
    hint = ""
    for keys, wording in _HINT_RULES:
        if _has(text, keys):
            hint = wording
            break
    if missing:
        hint = "协议端没有这个接口，NapCat 需要 v4.8.101 以上"
    return (
        f"{text}" + (f"（{hint}）" if hint and hint not in text else ""),
        missing,
        unusable,
    )


def _fail(action: str, detail: str, code: int = 0, note: str = "") -> NapCatError:
    text, missing, unusable = _classify(detail)
    msg = f"{action} 失败 retcode={code} {text}" if code else f"{action} 失败 {text}"
    return NapCatError(
        f"{msg}；{note}" if note else msg,
        action_missing=missing,
        file_unusable=unusable,
    )


def b64_payload(raw: bytes, *, prefix: bool = True) -> str:
    """Encode bytes for NapCat in one place.

    ``prefix=True`` returns the ``base64://`` file payload used by album/file
    uploads. ``prefix=False`` returns the bare base64 value required by
    AstrBot's ``Video.fromBase64``. Call this through ``asyncio.to_thread`` for
    large media so encoding never blocks the event loop.
    """
    encoded = base64.b64encode(raw).decode("ascii")
    return "base64://" + encoded if prefix else encoded


_b64_payload = b64_payload
