"""小红书适配器：主页 SSR 解析、笔记详情解析与风控信号判定。

小红书的"免登录解析"只到**单篇笔记**为止：笔记页是分享卡片的落地页，必须对
未登录访问者渲染标题/封面/图集，所以 `window.__INITIAL_STATE__` 里就有
`note.noteDetailMap[note_id].note.imageList[].urlDefault`（无水印原图）。

**博主时间线是另一套东西**：`/user/profile/<id>` 在未登录时 302 到 `/login`，
带 Cookie 才渲染 `state.user.notes`。这就是订阅必须登录的原因，也是本适配器
两个入口分开写的原因：

- `parse_user_notes(state)`：订阅轮询用，从博主主页 SSR 里取笔记卡片列表
- `parse_note_detail(state, note_id)`：只在该条要推送时补一次无水印图集/视频
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import constants as C
from ..types import SnsPost

INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.S)
_PROFILE_RE = re.compile(r"xiaohongshu\.com/user/profile/([0-9a-fA-F]{24})", re.I)
_EXPLORE_RE = re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-zA-Z]{16,32})", re.I)
_SHORT_RE = re.compile(r"xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+", re.I)

# 风控错误码只认字段形态（`error_code=300031` / `error-code: "300031"`）。
# 裸的 6 位数字匹配太宽：正文、统计数字、ID 里出现一次 300031 就会误判成验证码。
_ERROR_300031_RE = re.compile(r"error[_-]?code\s*[=:]\s*\"?300031\b")

NOTE_URL = "https://www.xiaohongshu.com/explore/{note_id}"


def account_id_from_text(text: str) -> str | None:
    """从主页链接里取 24 位 user_id；裸 24 位 id 也接受。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{24}", raw):
        return raw
    matched = _PROFILE_RE.search(raw)
    if matched:
        return matched.group(1)
    return None


def note_id_from_text(text: str) -> str | None:
    matched = _EXPLORE_RE.search(str(text or ""))
    return matched.group(1) if matched else None


def is_short_link(text: str) -> bool:
    return bool(_SHORT_RE.search(str(text or "")))


def extract_initial_state(html: str) -> dict[str, Any] | None:
    """从 SSR HTML 里抠 `window.__INITIAL_STATE__`。

    `undefined` 必须换成 `null` 才能被 `json.loads` 接受：小红书的 SSR 里大量
    出现 `xxx:undefined`，这是它自己的序列化瑕疵，不是我们解析错了。
    """
    text = str(html or "")
    matched = INITIAL_STATE_RE.search(text)
    if not matched:
        return None
    raw = matched.group(1).strip().rstrip(";")
    raw = raw.replace("undefined", "null")
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def unwrap(node: Any) -> Any:
    """小红书把部分字段包成 `{"_rawValue": ..., "_value": ...}`，统一拆一层。"""
    if isinstance(node, dict):
        if "_rawValue" in node:
            return node["_rawValue"]
        if "_value" in node:
            return node["_value"]
    return node


def _pick(node: Any, *keys: str) -> Any:
    if not isinstance(node, dict):
        return None
    for key in keys:
        if node.get(key):
            return node[key]
    return None


def _image_url(card: Any) -> str:
    """笔记卡片封面：`cover.infoList[]` 里最后一项通常是无裁剪原图。"""
    cover = _pick(card, "cover", "coverInfo")
    if isinstance(cover, str):
        return cover
    if not isinstance(cover, dict):
        return ""
    info = cover.get("infoList") or []
    if isinstance(info, list):
        for node in reversed(info):
            if isinstance(node, dict) and node.get("url"):
                return str(node["url"])
    return str(cover.get("url") or cover.get("urlDefault") or "")


def _note_from_card(card: Any, *, account_id: str = "") -> SnsPost | None:
    """主页笔记卡片 -> `SnsPost`（只有标题/封面，图集要再看详情）。"""
    if not isinstance(card, dict):
        return None
    note_id = str(_pick(card, "noteId", "id", "note_id") or "")
    if not note_id:
        return None
    title = str(_pick(card, "displayTitle", "title", "desc") or "")
    user = card.get("user") if isinstance(card.get("user"), dict) else {}
    cover = _image_url(card)
    return SnsPost(
        platform="xhs",
        account_id=str(account_id or ""),
        post_id=note_id,
        title=title[:80],
        text=title,
        link=NOTE_URL.format(note_id=note_id),
        ts=0.0,
        author=str(user.get("nickname") or user.get("nickName") or ""),
        images=[cover] if cover else [],
        kind=str(_pick(card, "type") or ""),
    )


def parse_user_notes(state: dict[str, Any]) -> tuple[str, str, list[SnsPost]]:
    """博主主页 SSR -> (昵称, 头像, 笔记列表)。

    兼容 `_rawValue` 包装：RSSHub 的 `getUser()` 里就是 `userPageData._rawValue
    || userPageData`、`notes._rawValue || notes`，两种形状都可能出现。
    """
    user = unwrap(state.get("user")) if isinstance(state, dict) else None
    if not isinstance(user, dict):
        return "", "", []
    page = unwrap(user.get("userPageData"))
    page = page if isinstance(page, dict) else {}
    basic = unwrap(page.get("basicInfo"))
    basic = basic if isinstance(basic, dict) else {}
    nickname = str(basic.get("nickname") or basic.get("nickName") or "")
    avatar = str(basic.get("images") or basic.get("avatar") or "")
    notes = unwrap(user.get("notes"))
    rows: list[Any] = []
    if isinstance(notes, list):
        rows = notes
    elif isinstance(notes, dict):
        for value in notes.values():
            if isinstance(value, list):
                rows.extend(value)
    # 小红书的 SSR 里 `notes` 有两种形状：卡片平铺，或再套一层列表
    # （`[[card, ...]]`）。套一层时不摊平，整条时间线会被判成"没有作品"。
    flattened: list[Any] = []
    for row in rows:
        if isinstance(row, list):
            flattened.extend(row)
        else:
            flattened.append(row)
    rows = flattened
    posts: list[SnsPost] = []
    for card in rows[: C.FETCH_POST_LIMIT]:
        item = _note_from_card(card)
        if item is not None:
            posts.append(item)
    return nickname, avatar, posts


def parse_note_detail(state: dict[str, Any], note_id: str) -> dict[str, Any]:
    """笔记详情 SSR -> {images, videos, text, title, ts}（无水印优先）。

    `noteDetailMap[note_id].note` 里：
    - `imageList[].urlDefault` 无水印
    - 视频走 `video.media.stream`，**h265 无水印，h264 有水印**，所以顺序是
      h265 → h264 → av1 → h266，取每档 `masterUrl` 的第一条
    """
    empty = {"images": [], "videos": [], "text": "", "title": "", "ts": 0.0}
    if not isinstance(state, dict):
        return empty
    note_map = state.get("note") if isinstance(state.get("note"), dict) else {}
    detail_map = note_map.get("noteDetailMap") if isinstance(note_map, dict) else None
    if not isinstance(detail_map, dict):
        return empty
    entry = detail_map.get(note_id)
    entry = entry if isinstance(entry, dict) else {}
    note = entry.get("note") if isinstance(entry.get("note"), dict) else None
    if note is None:
        return empty
    images: list[str] = []
    for node in note.get("imageList") or []:
        if isinstance(node, dict):
            url = str(node.get("urlDefault") or node.get("url") or "")
            if url:
                images.append(url)
        elif isinstance(node, str) and node:
            images.append(node)
    videos: list[str] = []
    video = note.get("video") if isinstance(note.get("video"), dict) else {}
    media = video.get("media") if isinstance(video.get("media"), dict) else {}
    stream = media.get("stream") if isinstance(media.get("stream"), dict) else {}
    for codec in ("h265", "h264", "av1", "h266"):
        rows = stream.get(codec)
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, dict) and first.get("masterUrl"):
                videos.append(str(first["masterUrl"]))
                break
    try:
        ts = float(note.get("time") or note.get("lastUpdateTime") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return {
        "images": images,
        "videos": videos,
        "text": str(note.get("desc") or ""),
        "title": str(note.get("title") or ""),
        "ts": ts / 1000.0 if ts > 1e11 else ts,
    }


def detect_signal(html: str, final_url: str = "", *, http_status: int = 200) -> str:
    """判风控/登录态信号；空串表示"页面看起来正常"。

    按严重程度从"肯定要停"往"可能只是这次没抓到"排：
    `.fe-verify-box`（验证码盒）> 302 到登录页 > `error_code=300031` >
    406 `code=-1` > SSR 里根本没有 `__INITIAL_STATE__`。
    """
    if http_status in (401, 403):
        return "xhs.redirect_login"
    if http_status == 406:
        return "xhs.http_406"
    url = str(final_url or "")
    if "/login" in url or "/website-login" in url:
        return "xhs.redirect_login"
    text = str(html or "")
    if "fe-verify-box" in text:
        return "xhs.verify_box"
    if _ERROR_300031_RE.search(text):
        return "xhs.error_300031"
    if "代码 -1" in text or '"code":-1' in text:
        return "xhs.http_406"
    if INITIAL_STATE_RE.search(text) is None:
        return "xhs.empty_state"
    return ""


def has_user_notes(state: dict[str, Any]) -> bool:
    """SSR 结构完整性：`user.notes` 在不在（在但为空 = 真的没有笔记）。"""
    if not isinstance(state, dict):
        return False
    user = unwrap(state.get("user"))
    if not isinstance(user, dict):
        return False
    return "notes" in user


def covered_by_card(card: SnsPost) -> bool:
    """卡片带了的字段是否够发一条消息（不够就得去拿详情）。"""
    return bool(card.text or card.title)
