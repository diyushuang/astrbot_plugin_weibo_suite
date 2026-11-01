"""RSS 兜底适配器（M3）：把任意 RSS/Atom 源解析成 `SnsPost`。

它是给"某平台实在取不动了"准备的退路：很多 GitHub 上的自建方案最终都落在
RSSHub（`/douyin/user/:uid`、`/xiaohongshu/user/:id/:type`、`/instagram/:user`）。
我们不在插件里跑 RSSHub，只要求用户给一个能返回 RSS 的地址（可以是自建
RSSHub、也可以是别的镜像），然后用 M2 的 HTTP 层去取——风控压力落在用户自己
那套服务上，插件的登录态完全不参与。

只依赖标准库 `xml.etree`：为了一条兜底路径多装一个 feedparser 不划算。
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from .. import constants as C
from ..types import SnsPost

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def _text(node: Any) -> str:
    if node is None:
        return ""
    text = "".join(node.itertext()) if hasattr(node, "itertext") else str(node)
    return " ".join(text.split())


def _strip_html(raw: str) -> str:
    return " ".join(_TAG_RE.sub(" ", str(raw or "")).split())


def _images_from(raw: str) -> list[str]:
    """从 description/content 的 HTML 片段里捞 <img src>（RSSHub 的图集就这样带）。"""
    return [url for url in _IMG_RE.findall(str(raw or ""))][: C.FETCH_POST_LIMIT]


def _link_of(node: Any) -> str:
    """RSS: `<link>url</link>`；Atom: `<link href=... rel=alternate>`。"""
    for child in node:
        # Atom 的标签带命名空间（`{http://www.w3.org/2005/Atom}link`），
        # 直接和 "link" 比会永远不相等，Atom 源就会丢链接
        if str(child.tag).split("}")[-1] != "link":
            continue
        href = child.get("href")
        if href and child.get("rel") in (None, "", "alternate"):
            return str(href)
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _ts_of(node: Any) -> float:
    from email.utils import parsedate_to_datetime

    for tag in ("pubDate", "published", "updated", f"{_ATOM}published", f"{_ATOM}updated"):
        child = node.find(tag)
        if child is None or not (child.text or "").strip():
            continue
        raw = child.text.strip()
        try:
            return parsedate_to_datetime(raw).timestamp()
        except Exception:
            pass
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return 0.0


def parse_feed(xml_text: str, *, account_id: str = "", platform: str = "") -> list[SnsPost]:
    """RSS/Atom 文本 -> `[SnsPost]`；解析不出来返回空列表（由调用方判信号）。"""
    text = str(xml_text or "").strip()
    if not text or not text.startswith("<"):
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    posts: list[SnsPost] = []
    for node in items[: C.FETCH_POST_LIMIT]:
        title = _text(node.find("title"))
        if not title:
            title = _text(node.find(f"{_ATOM}title"))
        link = _link_of(node)
        raw_body = ""
        for tag in ("description", "summary", "content", f"{_ATOM}summary", f"{_ATOM}content"):
            child = node.find(tag)
            if child is not None:
                raw_body = "".join(child.itertext()) if child.text is None else str(child.text)
                if raw_body:
                    break
        images = _images_from(raw_body)
        body = _strip_html(raw_body)
        post_id = _text(node.find("guid")) or _text(node.find(f"{_ATOM}id")) or link
        if not post_id:
            # 没有 guid/link 时绝不能用下标当 id：源里条目一改顺序，整份 feed
            # 都会变成"新内容"重推。按标题+发布时间取稳定哈希。
            published = _ts_of(node)
            digest = hashlib.sha1(f"{title}|{published}".encode()).hexdigest()[:16]
            post_id = f"hp{digest}"
        posts.append(
            SnsPost(
                platform=platform,
                account_id=str(account_id or ""),
                post_id=str(post_id),
                title=title[:80],
                text=body or title,
                link=link,
                ts=_ts_of(node),
                images=images,
                kind="rss",
            )
        )
    return posts


def detect_signal(xml_text: str) -> str:
    """RSS 源本身的风控信号：绝大多数情况是 429/403 的 HTML 错误页混进来。"""
    text = str(xml_text or "")
    lowered = text.lower()
    if "429" in lowered and "<html" in lowered:
        return "net.error"
    if "too many requests" in lowered or "rate limit" in lowered:
        return "net.error"
    if not text.strip().startswith("<"):
        return "net.error"
    # RSS 2.0 `<rss>`、Atom `<feed>`、RSS 1.0/RDF `<rdf:RDF>`（lower 后是 `<rdf:rdf`）
    if "<rss" not in lowered and "<feed" not in lowered and "<rdf:rdf" not in lowered:
        return "net.error"
    return ""
