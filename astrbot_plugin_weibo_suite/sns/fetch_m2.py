"""M2 取数：用导出好的 Cookie 走纯 HTTP。

它存在的理由只有一个——**当用户明确愿意把 Cookie 交给插件时，能省掉浏览器**。
代价说清楚，不粉饰：

- 请求的 TLS/JA3 指纹是 `aiohttp` 的，不是浏览器的。抖音的图片/接口曾明确在
  TLS 指纹层面拒绝 aiohttp（上游 README 踩坑表 #6），所以 M2 的失败率天然高于 M1。
- 同样因为不是浏览器，平台看到的是"同 IP + 同一批 Cookie + 非浏览器指纹"，
  一旦被判定为程序化访问，Cookie 直接进冷却期——这正是用户最怕的"账号被风控"。
- 因此 M2 永远只是备选：`fetch_mode` 默认 `browser`，配置面板把 M2 标成红字。

M2 依然遵循同一套纪律：只取第一页、单平台串行、失败一律带信号返回。
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

from . import constants as C
from .adapters import douyin as dy
from .adapters import instagram as ig
from .adapters import xhs
from .identity import missing_required_keys
from .types import FetchResult

# 桌面 UA：与 M1 的窗口保持同一份，避免"浏览器一套、HTTP 一套"被看成两个人
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

_DOUYIN_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}
_XHS_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}
_IG_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}


async def fetch(
    account: Any,
    *,
    session: Any,
    cookie: str = "",
    post_limit: int = C.FETCH_POST_LIMIT,
) -> FetchResult:
    """用 Cookie 走 HTTP 取一页内容。`session` 是插件共享的 aiohttp 会话。"""
    platform = str(getattr(account, "platform", "") or "")
    account_id = str(getattr(account, "account_id", "") or "")
    if not platform or not account_id:
        return FetchResult.failure("env.unknown", "订阅信息不完整")
    if not cookie:
        return FetchResult.failure("env.not_logged_in", "M2 需要 Cookie，但配置里没有")
    missing = missing_required_keys(platform, cookie)
    if missing:
        return FetchResult.failure(
            "env.not_logged_in", f"Cookie 缺少必需字段：{', '.join(missing)}"
        )
    if session is None:
        return FetchResult.failure("env.unavailable", "HTTP 会话未就绪")
    try:
        if platform == "douyin":
            return await _fetch_douyin(session, account_id, cookie, post_limit)
        if platform == "xhs":
            return await _fetch_xhs(session, account_id, cookie, post_limit)
        if platform == "instagram":
            return await _fetch_instagram(session, account_id, cookie, post_limit)
    except asyncio.TimeoutError:
        return FetchResult.failure("net.timeout", "请求超时")
    except Exception as exc:
        return FetchResult.failure("env.unknown", f"{exc.__class__.__name__}: {exc}")
    return FetchResult.failure("env.unknown", f"不支持的平台：{platform}")


# ---------------------------------------------------------------- HTTP 原语


async def _get_json(
    session: Any, url: str, headers: dict[str, str], cookie: str
) -> tuple[int, Any]:
    """发一次 GET 并把响应尽量解析成 JSON；返回 `(status, payload)`。"""
    from ..shared.http import guarded_request, read_limited_text

    merged = dict(headers)
    merged["Cookie"] = cookie
    async with await guarded_request(
        session, "GET", url, prepare=lambda u, m, cross: {"headers": dict(merged)}
    ) as resp:
        text = await read_limited_text(resp)
        status = int(resp.status)
    payload: Any
    try:
        import json

        payload = json.loads(text)
    except Exception:
        payload = text
    return status, payload


async def _get_text(
    session: Any, url: str, headers: dict[str, str], cookie: str
) -> tuple[int, str, str]:
    """发一次 GET 并返回 `(status, final_url, text)`（重定向已由 guarded_request 处理）。"""
    from ..shared.http import guarded_request, read_limited_text

    merged = dict(headers)
    if cookie:
        merged["Cookie"] = cookie
    async with await guarded_request(
        session, "GET", url, prepare=lambda u, m, cross: {"headers": dict(merged)}
    ) as resp:
        text = await read_limited_text(resp)
        status = int(resp.status)
        final = str(resp.url)
    return status, final, text


# ---------------------------------------------------------------- 抖音


async def _fetch_douyin(session: Any, account_id: str, cookie: str, post_limit: int) -> FetchResult:
    uifid = dy.extract_uifid(cookie)
    if not uifid:
        # 没有 UIFID 时签名无从算起——这正是上游插件"403 Uifid Not Found"的根因
        return FetchResult.failure(
            "douyin.status_code_8", "Cookie 里没有 UIFID，无法签名；请改用浏览器取数"
        )
    params = dy.signed_params(dy.post_params(account_id, count=post_limit), uifid=uifid)
    url = f"https://www.douyin.com{dy.POST_API_PATH}?{urlencode(params)}"
    status, payload = await _get_json(session, url, _DOUYIN_HEADERS, cookie)
    signal = dy.detect_signal(payload, status=status)
    if signal:
        return FetchResult.failure(signal)
    posts = dy.parse_aweme_list(payload, account_id=account_id)[:post_limit]
    if not posts:
        empty_signal = dy.signal_for_empty(payload)
        if empty_signal:
            return FetchResult.failure(empty_signal)
        return FetchResult.success([])
    return FetchResult.success(posts, nickname=posts[0].author, avatar=dy.author_avatar(payload))


# ---------------------------------------------------------------- 小红书


async def _fetch_xhs(session: Any, account_id: str, cookie: str, post_limit: int) -> FetchResult:
    url = C.PLATFORM_ACCOUNT_URL["xhs"].format(account_id=account_id)
    status, final_url, text = await _get_text(session, url, _XHS_HEADERS, cookie)
    signal = xhs.detect_signal(text, final_url, http_status=status)
    if signal:
        return FetchResult.failure(signal)
    state = xhs.extract_initial_state(text)
    if not state:
        return FetchResult.failure("xhs.empty_state")
    if not xhs.has_user_notes(state):
        return FetchResult.failure("xhs.no_user_notes")
    nickname, avatar, posts = xhs.parse_user_notes(state)
    for post in posts:
        post.account_id = account_id
    return FetchResult.success(posts[:post_limit], nickname=nickname, avatar=avatar)


async def fetch_note_detail(session: Any, note_id: str, cookie: str = "") -> dict[str, Any]:
    """M2 版本的笔记详情（M1 不可用时的图集补全）。"""
    url = xhs.NOTE_URL.format(note_id=note_id)
    status, final_url, text = await _get_text(session, url, _XHS_HEADERS, cookie)
    signal = xhs.detect_signal(text, final_url, http_status=status)
    if signal:
        return {"signal": signal}
    state = xhs.extract_initial_state(text)
    if not state:
        return {"signal": "xhs.empty_state"}
    return xhs.parse_note_detail(state, note_id) or {"signal": "xhs.no_user_notes"}


# ---------------------------------------------------------------- Instagram


async def _fetch_instagram(
    session: Any, username: str, cookie: str, post_limit: int
) -> FetchResult:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    status, payload = await _get_json(session, url, _IG_HEADERS, cookie)
    signal = ig.detect_signal(payload, status=status)
    if signal:
        return FetchResult.failure(signal)
    posts = ig.parse_profile(payload, account_id=username)
    if not posts:
        return FetchResult.failure(ig.signal_for_empty(payload))
    return FetchResult.success(posts[:post_limit], nickname=posts[0].author)


# ---------------------------------------------------------------- 状态


def describe() -> dict[str, Any]:
    return {
        "mode": "http",
        "platforms": list(C.PLATFORMS),
        "warning": "纯 HTTP 指纹不是浏览器，被判定为程序化访问的概率明显更高",
        "requires": "各平台 Cookie（含抖音 UIFID / 小红书 a1+web_session / IG sessionid）",
        "signing": "抖音 secsdk 签名由本模块纯 Python 计算",
        "note": "只取第一页，平台串行，失败按信号冷却",
    }
