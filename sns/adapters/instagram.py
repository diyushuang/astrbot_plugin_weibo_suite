"""Instagram 适配器：用户名解析、主页媒体解析与风控信号判定。

Instagram 是三家里最"脆"的：未登录访问主页只能拿到一张 `csrftoken`，
`/api/v1/users/web_profile_info/` 直接 401 `require_login`；匿名的
`i.instagram.com` 更是 429。所以本适配器主要服务 M1（已登录的浏览器上下文），
解析对象是浏览器里拿到的两种形状：

- 老式 SSR：`edge_owner_to_timeline_media.edges[].node`
- 新式 GraphQL：`data.xdt_api__v1__feed__user_timeline_graphql_connection.edges[].node`
  （M1 拦截 `/graphql/query` 响应，M2 走 `web_profile_info`）

两种形状的 `node` 字段名基本一致，所以 `parse_node()` 一份就够。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import constants as C
from ..types import SnsPost

_PROFILE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]{1,30})", re.I)
_USERNAME_RE = re.compile(r"^@?([A-Za-z0-9._]{1,30})$")

# 主页路径里不是用户名的保留段
_RESERVED = frozenset(
    {
        "p",
        "reel",
        "reels",
        "explore",
        "accounts",
        "stories",
        "tv",
        "about",
        "developer",
        "legal",
        "api",
        "graphql",
        "directory",
        "web",
        "challenge",
        "emails",
        "session",
    }
)

POST_URL = "https://www.instagram.com/p/{shortcode}/"


def account_id_from_text(text: str) -> str | None:
    """从主页链接或 `@username` 里取用户名（小写保留原样，不做大小写折叠）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if "instagram.com" in raw:
        matched = _PROFILE_RE.search(raw)
        if not matched:
            return None
        name = matched.group(1)
        return None if name.lower() in _RESERVED else name
    matched = _USERNAME_RE.match(raw)
    if not matched:
        return None
    name = matched.group(1)
    return None if name.lower() in _RESERVED else name


def _caption(node: dict[str, Any]) -> str:
    """标题/正文：老式 `edge_media_to_caption`，新式 `caption`。"""
    for key in ("edge_media_to_caption", "edge_media_to_caption_"):
        edge = node.get(key)
        if isinstance(edge, dict):
            edges = edge.get("edges")
            if isinstance(edges, list) and edges:
                first = edges[0]
                if isinstance(first, dict):
                    deep = first.get("node")
                    if isinstance(deep, dict) and deep.get("text"):
                        return str(deep["text"])
    if isinstance(node.get("caption"), dict):
        return str(node["caption"].get("text") or "")
    if isinstance(node.get("caption"), str):
        return str(node["caption"])
    return ""


def _best_image(node: dict[str, Any]) -> str:
    """封面优先取最高分辨率，其次 display_url。"""
    for key in ("display_url", "display_src", "thumbnail_src"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def parse_node(node: dict[str, Any], *, account_id: str = "") -> SnsPost | None:
    """一条 media node -> `SnsPost`。"""
    if not isinstance(node, dict):
        return None
    shortcode = str(node.get("shortcode") or node.get("code") or "").strip()
    if not shortcode:
        return None
    caption = _caption(node)
    try:
        ts = float(node.get("taken_at_timestamp") or node.get("taken_at") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    is_video = bool(node.get("is_video") or node.get("isVideo"))
    owner = node.get("owner") if isinstance(node.get("owner"), dict) else {}
    images: list[str] = []
    videos: list[str] = []
    if is_video:
        video_url = node.get("video_url")
        if isinstance(video_url, str) and video_url:
            videos.append(video_url)
        cover = _best_image(node)
        if cover:
            images.append(cover)
    else:
        cover = _best_image(node)
        if cover:
            images.append(cover)
        # 图集：sidecar 的子项
        children = node.get("edge_sidecar_to_children")
        if isinstance(children, dict):
            edges = children.get("edges")
            if isinstance(edges, list):
                for edge in edges:
                    child = edge.get("node") if isinstance(edge, dict) else None
                    if isinstance(child, dict):
                        url = _best_image(child)
                        if url and url not in images:
                            images.append(url)
    return SnsPost(
        platform="instagram",
        account_id=str(account_id or ""),
        post_id=shortcode,
        title=caption.splitlines()[0][:80] if caption else "",
        text=caption,
        link=POST_URL.format(shortcode=shortcode),
        ts=ts,
        author=str(owner.get("username") or ""),
        images=images,
        videos=videos,
        kind="video" if videos else "image",
    )


def parse_edges(edges: Any, *, account_id: str = "") -> list[SnsPost]:
    if not isinstance(edges, list):
        return []
    posts: list[SnsPost] = []
    for edge in edges[: C.FETCH_POST_LIMIT]:
        node = edge.get("node") if isinstance(edge, dict) else edge
        post = parse_node(node, account_id=account_id) if isinstance(node, dict) else None
        if post is not None:
            posts.append(post)
    return posts


def _find_edges(data: Any) -> Any:
    """在若干层嵌套里找 `edges` 列表（老式/新式两种包装都在这里兜住）。"""
    if isinstance(data, list):
        return None
    if not isinstance(data, dict):
        return None
    timeline = data.get("edge_owner_to_timeline_media")
    if isinstance(timeline, dict) and isinstance(timeline.get("edges"), list):
        return timeline["edges"]
    payload = data.get("data")
    if isinstance(payload, dict):
        conn = payload.get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if isinstance(conn, dict) and isinstance(conn.get("edges"), list):
            return conn["edges"]
        found = _find_edges(payload)
        if found is not None:
            return found
    user = data.get("user")
    if isinstance(user, dict):
        found = _find_edges(user)
        if found is not None:
            return found
    for key in (
        "xdt_api__v1__feed__user_timeline_graphql_connection",
        "user",
        "graphql",
        "items",
    ):
        child = data.get(key)
        if isinstance(child, dict):
            found = _find_edges(child)
            if found is not None:
                return found
    return None


def parse_profile(payload: Any, *, account_id: str = "") -> list[SnsPost]:
    """`web_profile_info` 响应或 GraphQL 响应 -> `[SnsPost]`。"""
    if not isinstance(payload, dict):
        return []
    edges = _find_edges(payload)
    return parse_edges(edges, account_id=account_id) if edges else []


def profile_from_html(html: str) -> dict[str, Any] | None:
    """从主页 HTML 里找 timeline 的 JSON 片段（M1 读 SSR 的兜底路径）。

    不做完整 HTML 解析：只在 `edge_owner_to_timeline_media` 第一次出现的位置
    向左找最近的 `{`、向右做括号配平，能截出一段合法 JSON 就算成功。
    """
    text = str(html or "")
    marker = text.find("edge_owner_to_timeline_media")
    if marker < 0:
        return None
    start = text.rfind("{", 0, marker)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start : index + 1]
                try:
                    data = json.loads(snippet)
                except Exception:
                    return None
                return data if isinstance(data, dict) else None
    return None


def detect_signal(
    payload: Any = None,
    *,
    status: int = 200,
    final_url: str = "",
    html: str = "",
) -> str:
    """判风控/登录态信号；空串表示"看起来正常"。

    `checkpoint_required` / `challenge_required` 是"必须人工过验证"的硬信号，
    对应权重 99 —— 服务层看到这种信号会直接暂停账号，绝不重试（重试等于告诉
    平台"这个账号正在被程序驱动"）。
    """
    url = str(final_url or "")
    if "/challenge" in url or "/checkpoint" in url:
        return "ig.challenge"
    if status in (401, 403):
        return "ig.http_401"
    if status == 429:
        return "ig.http_429"
    text = str(html or "")
    if "checkpoint_required" in text:
        return "ig.checkpoint"
    if "challenge_required" in text:
        return "ig.challenge"
    if '"require_login":true' in text or '"require_login": true' in text:
        return "ig.require_login"
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
        if payload.get("require_login"):
            return "ig.require_login"
        lowered = message.lower()
        if "checkpoint" in lowered:
            return "ig.checkpoint"
        # graphql 的 400 响应只给一句 `challenge_required`，不认它会退化成
        # "看起来正常 + 0 条"，把"要人工过验证"错当"没有更新"
        if "challenge" in lowered:
            return "ig.challenge"
        if "wait a few minutes" in message.lower():
            return "ig.http_429"
    if isinstance(payload, str) and "require_login" in payload:
        return "ig.require_login"
    return ""


def signal_for_empty(payload: Any) -> str:
    """ "拿到页面但没有 media 字段"是风控/改版的信号，不是"没有新内容"。"""
    if isinstance(payload, dict) and _find_edges(payload) is not None:
        return ""
    return "ig.empty_edges"
