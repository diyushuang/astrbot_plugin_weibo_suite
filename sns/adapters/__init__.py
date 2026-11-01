"""平台适配器注册表。

三个平台的解析入口形状不同（抖音是 `aweme_list`、小红书是 SSR 状态、IG 是
`edges`），所以不强行套一个统一接口；这里只提供三件真正通用的事：

- `parse_account_id()`：把用户粘的链接/裸 id 变成平台内 id
- `parse_posts()`：把一份"主页响应"解析成 `[SnsPost]`
- `empty_signal()`：拿到空结果时该记什么信号（空响应绝不等于"没有更新"）
"""

from __future__ import annotations

from typing import Any

from .. import constants as C
from ..types import SnsPost
from . import douyin, instagram, rss, xhs

__all__ = [
    "douyin",
    "empty_signal",
    "instagram",
    "parse_account_id",
    "parse_posts",
    "rss",
    "supports",
    "xhs",
]

_MODULES = {"douyin": douyin, "xhs": xhs, "instagram": instagram}


def parse_account_id(platform: str, text: str) -> str | None:
    module = _MODULES.get(platform)
    if module is None:
        return None
    return module.account_id_from_text(text)


def parse_posts(platform: str, payload: Any, *, account_id: str = "") -> list[SnsPost]:
    """把平台响应解析成更新列表（解析失败的形状一律返回空列表）。"""
    if platform == "douyin":
        return douyin.parse_aweme_list(payload, account_id=account_id)
    if platform == "xhs":
        if isinstance(payload, dict):
            _nickname, _avatar, posts = xhs.parse_user_notes(payload)
            for post in posts:
                post.account_id = account_id
            return posts
        return []
    if platform == "instagram":
        return instagram.parse_profile(payload, account_id=account_id)
    if platform == "rss":
        # 必须把平台名传进去：否则 SnsPost.platform 是空串，推送模板里的
        # `{platform}` 占位符会渲染成空白。
        return rss.parse_feed(str(payload or ""), account_id=account_id, platform="rss")
    return []


def empty_signal(platform: str, payload: Any = None) -> str:
    """空结果信号：空列表在抖音/IG 上都是风控特征，小红书由结构判定兜住。"""
    if platform == "douyin":
        return douyin.signal_for_empty(payload)
    if platform == "instagram":
        return instagram.signal_for_empty(payload)
    if platform == "xhs":
        return ""
    return ""


def supports(platform: str) -> bool:
    return platform in C.PLATFORMS
