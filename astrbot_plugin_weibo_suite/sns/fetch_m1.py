"""M1 取数：在持久化浏览器里打开博主主页，读平台"自己发出来"的数据。

这一层刻意不懂签名、不懂 Cookie：页面里的前端脚本该签的签、该算的算，我们只
监听它发出去的 XHR / 读它渲染好的 SSR。这就是 M1 比 M2 抗风控的根本原因——
站在平台角度看，这就是一个真实浏览器打开了博主主页。

三个平台的数据面各不相同：

- 抖音：作品列表走 XHR `/aweme/v1/web/aweme/post/`，截获响应即可。
- 小红书：用户页把笔记列表 SSR 进 `window.__INITIAL_STATE__`，读 HTML。
- Instagram：主页历史上 SSR 在 `edge_owner_to_timeline_media`，新版走
  `/graphql/query`；两条都试，最后才退回在 HTML 里切片 JSON。

失败一律返回带信号的 `FetchResult.failure()`，由 `guard` 决定冷却多久——
这里绝不吞异常，也绝不把"没抓到"当成"没有更新"。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from . import constants as C
from .adapters import douyin as dy
from .adapters import instagram as ig
from .adapters import xhs
from .browser import BrowserSession, BrowserUnavailable
from .types import FetchResult

# Instagram 主页可能触发的那几个数据请求（新版是 graphql，旧版在 HTML 里）
_IG_DATA_PATHS = ("/graphql/query", "/api/v1/feed/user/", "web_profile_info")


async def fetch(
    account: Any,
    *,
    browser: BrowserSession,
    post_limit: int = C.FETCH_POST_LIMIT,
) -> FetchResult:
    """取一个博主的最新一页内容。`account` 只需有 `.platform` / `.account_id`。"""
    platform = str(getattr(account, "platform", "") or "")
    account_id = str(getattr(account, "account_id", "") or "")
    if not platform or not account_id:
        return FetchResult.failure("env.unknown", "订阅信息不完整")
    try:
        if platform == "douyin":
            return await _fetch_douyin(browser, account_id, post_limit)
        if platform == "xhs":
            return await _fetch_xhs(browser, account_id, post_limit)
        if platform == "instagram":
            return await _fetch_instagram(browser, account_id, post_limit)
    except BrowserUnavailable as exc:
        return FetchResult.failure("env.unavailable", str(exc))
    except asyncio.TimeoutError:
        return FetchResult.failure("net.timeout", "页面加载超时")
    except Exception as exc:  # 任何未预期异常都不该让整轮调度崩掉
        return FetchResult.failure("env.unknown", f"{exc.__class__.__name__}: {exc}")
    return FetchResult.failure("env.unknown", f"不支持的平台：{platform}")


# ---------------------------------------------------------------- 通用动作


async def _open(
    browser: BrowserSession,
    platform: str,
    url: str,
    url_substrs: tuple[str, ...] = (),
) -> tuple[Any, int, str, list[Any]]:
    """打开页面并（可选）截获匹配 `url_substrs` 的 JSON 响应。

    返回 `(page, http_status, final_url, captured_payloads)`。
    这里不用 `browser.collect_json`：风控判定需要 HTTP 状态与最终 URL，
    被隐藏掉就无法区分"被重定向到登录页"和"页面真的没有数据"。
    """
    page = await browser.page(platform)
    timeout = browser.timeout_ms
    # 整次打开只给一个总预算：goto 用掉多少，后面的 XHR 等待就只剩多少。
    # 以前是"goto 45s 超时 + 再单开 45s 等 XHR"，最坏要占着浏览器锁 90s。
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000.0
    captured: list[Any] = []

    async def on_response(response):
        try:
            if not any(part in str(response.url) for part in url_substrs):
                return
            payload = await response.json()
        except Exception:
            return
        if payload is not None:
            captured.append(payload)

    listener = bool(url_substrs)
    if listener:
        page.on("response", on_response)
    status = 0
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        try:
            status = int(response.status) if response is not None else 0
        except Exception:
            status = 0
        if listener:
            while not captured and loop.time() < deadline:
                await asyncio.sleep(0.2)
    finally:
        if listener:
            with contextlib.suppress(Exception):
                page.remove_listener("response", on_response)
    try:
        final_url = str(page.url or "")
    except Exception:
        final_url = ""
    return page, status, final_url, captured


async def _html(page: Any) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


# ---------------------------------------------------------------- 抖音


async def _fetch_douyin(browser: BrowserSession, account_id: str, post_limit: int) -> FetchResult:
    url = C.PLATFORM_ACCOUNT_URL["douyin"].format(account_id=account_id)
    async with browser.lock("douyin"):
        page, status, _final, captured = await _open(browser, "douyin", url, (dy.POST_API_PATH,))
        payload = captured[0] if captured else None
        if payload is None:
            # 没截到 XHR：先看页面本身有没有风控痕迹，否则按"空响应"记风控。
            # 抖音风控最典型的形态就是 200 + 空内容，这里必须记成 risk。
            signal = dy.detect_signal(await _html(page), status=status)
            return FetchResult.failure(signal or "douyin.empty_silent")
    signal = dy.detect_signal(payload, status=status)
    if signal:
        return FetchResult.failure(signal)
    posts = dy.parse_aweme_list(payload, account_id=account_id)[:post_limit]
    if not posts:
        empty_signal = dy.signal_for_empty(payload)
        if empty_signal:
            return FetchResult.failure(empty_signal)
        return FetchResult.success([])
    return FetchResult.success(
        posts,
        nickname=posts[0].author,
        avatar=dy.author_avatar(payload),
    )


# ---------------------------------------------------------------- 小红书


async def _fetch_xhs(browser: BrowserSession, account_id: str, post_limit: int) -> FetchResult:
    url = C.PLATFORM_ACCOUNT_URL["xhs"].format(account_id=account_id)
    async with browser.lock("xhs"):
        page, status, final_url, _captured = await _open(browser, "xhs", url)
        html = await _html(page)
    signal = xhs.detect_signal(html, final_url, http_status=status)
    if signal:
        return FetchResult.failure(signal)
    state = xhs.extract_initial_state(html)
    if not state:
        return FetchResult.failure("xhs.empty_state")
    if not xhs.has_user_notes(state):
        return FetchResult.failure("xhs.no_user_notes")
    nickname, avatar, posts = xhs.parse_user_notes(state)
    for post in posts:
        post.account_id = account_id
    return FetchResult.success(posts[:post_limit], nickname=nickname, avatar=avatar)


async def fetch_note_detail(browser: BrowserSession, note_id: str) -> dict[str, Any]:
    """补全一篇小红书笔记的图集/视频（列表卡只有封面，且是有水印的小图）。

    只在"确实有新笔记要推送"时才会走到这里，所以额外一次页面加载是可接受的；
    拿到的是详情页 SSR 里的 `imageList[].urlDefault`（无水印原图）。
    """
    url = xhs.NOTE_URL.format(note_id=note_id)
    async with browser.lock("xhs"):
        page, status, final_url, _captured = await _open(browser, "xhs", url)
        html = await _html(page)
    signal = xhs.detect_signal(html, final_url, http_status=status)
    if signal:
        return {"signal": signal}
    state = xhs.extract_initial_state(html)
    if not state:
        return {"signal": "xhs.empty_state"}
    detail = xhs.parse_note_detail(state, note_id)
    if detail:
        return detail
    return {"signal": "xhs.no_user_notes"}


# ---------------------------------------------------------------- Instagram


async def _fetch_instagram(browser: BrowserSession, username: str, post_limit: int) -> FetchResult:
    url = C.PLATFORM_ACCOUNT_URL["instagram"].format(account_id=username)
    async with browser.lock("instagram"):
        page, status, final_url, captured = await _open(browser, "instagram", url, _IG_DATA_PATHS)
        html = await _html(page)
    signal = ig.detect_signal(None, status=status, final_url=final_url, html=html)
    if signal:
        return FetchResult.failure(signal)
    for payload in captured:
        posts = ig.parse_profile(payload, account_id=username)
        if posts:
            return FetchResult.success(posts[:post_limit], nickname=posts[0].author)
    snapshot = ig.profile_from_html(html)
    if snapshot:
        # profile_from_html 返回的是**整段 timeline JSON（dict）**，不是帖子列表；
        # 必须再过一遍 parse_profile 才能拿到 SnsPost。此前直接把 dict 当列表迭代，
        # `post.account_id = ...` 会在 str 键上抛 AttributeError，整条 HTML 兜底
        # 路径 100% 失效（异常又被 fetch 吞成 env.unknown，看着像"没开浏览器"）。
        posts = ig.parse_profile(snapshot, account_id=username)
        if posts:
            return FetchResult.success(posts[:post_limit], nickname=posts[0].author)
    return FetchResult.failure(ig.signal_for_empty(None))


# ---------------------------------------------------------------- 状态


def describe() -> dict[str, Any]:
    """给面板看的"这条取数路径的能力说明"。"""
    return {
        "mode": "browser",
        "platforms": list(C.PLATFORMS),
        "post_limit": C.FETCH_POST_LIMIT,
        "signing": "平台前端自己签（插件不维护签名算法）",
        "cookies": "留在浏览器 profile 内，插件不读取、不导出",
        "note": "打开博主主页，截 XHR 或读 SSR；失败按信号冷却",
        "checked_ts": time.time(),
    }
