"""抖音适配器：账号 id 解析、签名、作品列表解析与风控信号判定。

取数有两条路（`fetch_m1` 走浏览器拦 XHR，`fetch_m2` 走带 Cookie 的 HTTP）：

- M1 不需要这里的签名：抖音前端自己算 `a_bogus`/`Uifid`，我们只截获它发出的
  `/web/aweme/post/` 响应。
- M2 需要签名。这里保留了 `x-secsdk-web-signature` 的纯 Python 实现（与
  `astrbot_plugin_douyin_subscribe` 的 `douyin_api.py` 同口径），但它依赖
  Cookie 里的 `UIFID`——**没有登录态就签不出有效签名**，这也是"抖音不可能免登录
  拿时间线"的根因。

解析函数全部是纯函数，回放测试直接喂 `tests/fixtures/sns_douyin_post.json`。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from .. import constants as C
from ..types import SnsPost

# 平台签名盐（与上游插件常量一致）。抖音改版时会换盐；换了就更新这里。
SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"

UIFID_COOKIE_KEYS = ("UIFID", "UIFID_TEMP", "uifid", "uifid_temp", "uifidtemp", "UIFIDTEMP")

POST_API_PATH = "/aweme/v1/web/aweme/post/"
USER_API_PATH = "/aweme/v1/web/user/profile/other/"

# 验证码判定只看 <title> 与页面顶部可见文案，不扫整页：打包 JS 里的
# `verifyStatus` / `captchaBypass` 之类的常量几乎每页都有，按子串匹配会把
# 正常页面判成 RISK，进而指数冷却、最终把平台推到熔断。
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CAPTCHA_BODY_RE = re.compile(
    r"请完成下列验证|拖动滑块|向右滑动|安全验证|verify-center|captcha_container",
    re.I,
)

# 主页链接：www.douyin.com/user/<sec_uid>、iesdouyin.com/share/user/<sec_uid>
_USER_URL_RE = re.compile(r"douyin\.com/(?:share/)?user/(MS4wLjABAAAA[A-Za-z0-9_\-]+)", re.I)
_SEC_UID_RE = re.compile(r"^(MS4wLjABAAAA[A-Za-z0-9_\-]+)$")


def account_id_from_text(text: str) -> str | None:
    """从主页链接或裸 sec_uid 里取抖音账号 id（sec_uid）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if _SEC_UID_RE.match(raw):
        return raw
    matched = _USER_URL_RE.search(raw)
    if matched:
        return matched.group(1)
    return None


def extract_uifid(cookie: str) -> str:
    """从 Cookie 串里取 UIFID（签名必需）。"""
    for part in str(cookie or "").split(";"):
        piece = part.strip()
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        if key.strip() in UIFID_COOKIE_KEYS:
            value = value.strip()
            if value:
                return value
    return ""


def build_signature(params: dict[str, str], uifid: str, timestamp: str | int) -> str:
    """`md5(uifid_timestamp_SALT_query)`，query 用 `&` 拼接且不含签名参数本身。"""
    query = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] is not None)
    payload = f"{uifid}_{timestamp}_{SIGN_SALT}_{query}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def signed_params(
    params: dict[str, Any],
    *,
    uifid: str,
    timestamp: str | int | None = None,
) -> dict[str, str]:
    """补齐签名三件套：`uifid` / `timestamp` / `x-secsdk-web-signature`。"""
    flat = {str(k): str(v) for k, v in params.items() if v is not None}
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    flat["uifid"] = uifid
    flat["timestamp"] = stamp
    flat["x-secsdk-web-signature"] = build_signature(
        {k: v for k, v in flat.items() if k != "x-secsdk-web-signature"}, uifid, stamp
    )
    return flat


def post_params(sec_uid: str, *, count: int = C.FETCH_POST_LIMIT) -> dict[str, str]:
    """作品列表接口的查询参数（未签名）。

    单独拆出来是给 M2 用的：签名必须对着"最终的参数集合"算，先拼成 URL 再拆回来
    容易在多一层编码时算错——这类签名错一位就是 403，且看不出原因。
    """
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": str(sec_uid),
        "max_cursor": "0",
        "locate_query": "false",
        "show_live_replay_strategy": "1",
        "count": str(int(count)),
        "publish_video_strategy_type": "2",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "platform": "PC",
        "downlink": "10",
    }


def _first_url(node: Any) -> str:
    """抖音的媒体字段几乎都是 `{url_list: [...]}`，取最后一个（通常最清晰）。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        # 允许直接传 url 列表：调用方手滑传错层级时不要静默返回空串
        for url in reversed(node):
            if isinstance(url, str) and url:
                return url
        return ""
    if not isinstance(node, dict):
        return ""
    urls = node.get("url_list") or node.get("urlList") or []
    if isinstance(urls, list):
        for url in reversed(urls):
            if isinstance(url, str) and url:
                return url
    return ""


def _no_watermark(url: str) -> str:
    """把 `playwm` 换成 `play` 去掉抖音视频水印（公开作品有效）。"""
    return str(url or "").replace("/playwm/", "/play/")


def parse_aweme(
    item: dict[str, Any],
    *,
    fallback_author: str = "",
    account_id: str = "",
) -> SnsPost | None:
    """一条 aweme -> `SnsPost`；缺少 id 时返回 None（宁可丢也不推错东西）。

    `account_id` 由调用方给（订阅名单里的 sec_uid）：aweme 里没有 sec_uid 字段，
    拿昵称当 id 会让同一博主的条目落到不同的账号键上，去重直接失效。
    """
    if not isinstance(item, dict):
        return None
    aweme_id = str(item.get("aweme_id") or item.get("awemeId") or "").strip()
    if not aweme_id:
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    nickname = str(author.get("nickname") or fallback_author or "")
    desc = str(item.get("desc") or "")
    created = item.get("create_time") or item.get("createTime") or 0
    try:
        ts = float(created)
    except (TypeError, ValueError):
        ts = 0.0
    images: list[str] = []
    for node in item.get("images") or []:
        # 传整个 node：`_first_url` 自己会取 `url_list`，这里再取一次会变成
        # "把列表当 dict"，图集帖会解析出 0 张图（真实踩过的坑）
        url = _first_url(node)
        if url:
            images.append(url)
    videos: list[str] = []
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    play = _first_url(video.get("play_addr") or video.get("playAddr"))
    if play:
        videos.append(_no_watermark(play))
    if not images:
        # 视频帖：投递侧只发图片段，没有封面就会推出一条空消息
        cover = _first_url(video.get("cover")) or _first_url(video.get("origin_cover"))
        if cover:
            images.append(cover)
    kind = "video" if videos else ("note" if images else "")
    return SnsPost(
        platform="douyin",
        account_id=str(account_id or ""),
        post_id=aweme_id,
        title=desc[:80],
        text=desc,
        link=f"https://www.douyin.com/video/{aweme_id}",
        ts=ts,
        author=nickname,
        images=images,
        videos=videos,
        kind=kind,
    )


def author_avatar(item: dict[str, Any]) -> str:
    author = item.get("author") if isinstance(item, dict) else {}
    if not isinstance(author, dict):
        return ""
    for key in ("avatar_thumb", "avatar_medium", "avatar_larger", "avatarThumb"):
        url = _first_url(author.get(key))
        if url:
            return url
    return ""


def detect_signal(payload: Any, *, status: int = 200) -> str:
    """判抖音响应的风控/登录态信号；返回空串表示"看起来正常"。

    关键教训（上游 README 明确写过）：抖音风控最典型的表现是 **HTTP 200 +
    `status_code: 0` + 空内容**。所以"空列表"在这里不是"没有更新"，而是
    `douyin.empty_silent`——这条判定必须由调用方当作错误处理。
    """
    if status in (401, 403):
        return "douyin.http_403"
    if status == 429:
        return "douyin.http_429"
    if isinstance(payload, str):
        text = payload
        if "Blocked by" in text or "ArgusSecurityPlugin" in text:
            return "douyin.argus_blocked"
        # 只在"页面里就是验证码页"时判 captcha：整页 HTML 里 `verifyStatus`、
        # `captchaBypass` 这类 JS 常量几乎每页都有，按子串匹配会把正常页面
        # 判成 RISK，进而把平台一路推到熔断。
        title = _HTML_TITLE_RE.search(text)
        if title and any(k in title.group(1).lower() for k in ("验证", "verify", "captcha")):
            return "douyin.captcha"
        if _CAPTCHA_BODY_RE.search(text):
            return "douyin.captcha"
        return ""
    if not isinstance(payload, dict):
        return ""
    code = payload.get("status_code", payload.get("statusCode"))
    if code == 8:
        return "douyin.status_code_8"
    message = str(payload.get("status_msg") or payload.get("message") or "")
    if "blocked" in message.lower():
        return "douyin.argus_blocked"
    return ""


def parse_aweme_list(
    payload: Any, *, fallback_author: str = "", account_id: str = ""
) -> list[SnsPost]:
    """`aweme_list` -> `[SnsPost]`（顺序保持平台原样，服务层自己按时间排序）。"""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("aweme_list") or payload.get("awemeList") or []
    if not isinstance(rows, list):
        return []
    posts: list[SnsPost] = []
    for row in rows[: C.FETCH_POST_LIMIT]:
        post = parse_aweme(row, fallback_author=fallback_author, account_id=account_id)
        if post is not None:
            posts.append(post)
    return posts


def signal_for_empty(payload: Any) -> str:
    """拿到"正常但列表为空"时该记什么信号。

    抖音的空列表一律按风控处理（`douyin.empty_silent`）；只有明确带了
    `has_more`/`cursor` 这类结构字段的空响应才可能是真的没有内容。
    """
    if isinstance(payload, dict) and ("has_more" in payload or "cursor" in payload):
        return ""
    return "douyin.empty_silent"
