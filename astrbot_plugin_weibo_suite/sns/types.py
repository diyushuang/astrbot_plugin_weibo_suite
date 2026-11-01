"""多平台订阅域的数据形状。

三种对象各自负责一件事：

- `SnsAccount`：一个被订阅的博主（平台 + 平台内 id + 节奏/健康状态）
- `SnsPost`：抓回来的一条更新（正文、配图、视频、链接）
- `FetchResult`：一次抓取的结论（结果类别 + 信号 + 若干条）

全部用 dataclass 而不是裸 dict：解析失败时字段缺失会当场抛 TypeError，比
`payload.get("x") or {}` 一路空下去要好查得多。落盘用 `to_dict()`，读回用
`from_dict()`（做旧字段补全，不做严格校验）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import constants as C


def account_key(platform: str, account_id: str) -> str:
    """账号唯一键：`平台:id`。跨平台同名博主（比如都叫"小王"）不会串。"""
    return f"{platform}:{account_id}"


def platform_label(platform: str) -> str:
    return C.PLATFORM_LABEL.get(platform, platform)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """手工编辑坏的状态文件不该让整个订阅域起不来：解析失败就回落默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


@dataclass
class SnsPost:
    """一条博主更新。"""

    platform: str
    account_id: str
    post_id: str
    title: str = ""
    text: str = ""
    link: str = ""
    ts: float = 0.0
    author: str = ""
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    # 平台原始载荷里的类型标记：抖音 video/note、小红书 normal/video、IG image/video
    kind: str = ""

    @property
    def dedupe_key(self) -> str:
        return f"{self.platform}:{self.account_id}:{self.post_id}"

    def body(self, *, text_max: int = 300) -> str:
        """推送正文：优先正文，没有正文就用标题，统一截断。"""
        raw = str(self.text or "").strip() or str(self.title or "").strip()
        raw = " ".join(raw.split())
        if text_max and len(raw) > text_max:
            raw = raw[:text_max].rstrip() + "…"
        return raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "post_id": self.post_id,
            "title": self.title,
            "text": self.text,
            "link": self.link,
            "ts": self.ts,
            "author": self.author,
            "images": list(self.images),
            "videos": list(self.videos),
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnsPost | None:
        if not isinstance(data, dict):
            return None
        platform = str(data.get("platform") or "")
        account_id = str(data.get("account_id") or "")
        post_id = str(data.get("post_id") or "")
        if not (platform and account_id and post_id):
            return None
        return cls(
            platform=platform,
            account_id=account_id,
            post_id=post_id,
            title=str(data.get("title") or ""),
            text=str(data.get("text") or ""),
            link=str(data.get("link") or ""),
            ts=float(data.get("ts") or 0),
            author=str(data.get("author") or ""),
            images=[str(i) for i in (data.get("images") or []) if i],
            videos=[str(v) for v in (data.get("videos") or []) if v],
            kind=str(data.get("kind") or ""),
        )


@dataclass
class FetchResult:
    """一次抓取的结论。

    `posts` 只在 `outcome == OUTCOME_OK` 时有意义（可能为空列表 = 确实没内容）；
    风控/网络/登录态失败时 `posts` 一律为空，由 `signal` 说明原因——这条区分
    是硬规则：空响应绝不等于"没有更新"。
    """

    outcome: str
    signal: str = ""
    detail: str = ""
    posts: list[SnsPost] = field(default_factory=list)
    nickname: str = ""
    avatar: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (C.OUTCOME_OK, C.OUTCOME_EMPTY)

    @property
    def label(self) -> str:
        if self.signal:
            return C.signal_label(self.signal)
        return self.detail or self.outcome

    @classmethod
    def success(cls, posts: list[SnsPost], **kw: Any) -> FetchResult:
        return cls(
            outcome=C.OUTCOME_OK if posts else C.OUTCOME_EMPTY,
            signal="ok.empty" if not posts else "",
            posts=posts,
            **kw,
        )

    @classmethod
    def failure(cls, signal: str, detail: str = "", **kw: Any) -> FetchResult:
        return cls(
            outcome=C.signal_outcome(signal),
            signal=signal,
            detail=detail or C.signal_label(signal),
            **kw,
        )


@dataclass
class SnsAccount:
    """一个被订阅的博主及其节奏状态（落 `sns_state.json`）。"""

    platform: str
    account_id: str
    name: str = ""
    url: str = ""
    # M3（RSS 兜底）用的订阅地址：只有用户在 `订阅 抖音 <链接> feed=<url>` 里
    # 显式给了才用，正常情况下留空（走浏览器/HTTP 直取）
    feed_url: str = ""
    enabled: bool = True
    # 首次成功抓取只建基线，不推历史内容
    baseline_done: bool = False
    seen_ids: list[str] = field(default_factory=list)
    # 节奏
    next_check_ts: float = 0.0
    last_check_ts: float = 0.0
    last_ok_ts: float = 0.0
    last_risk_ts: float = 0.0
    last_error: str = ""
    last_signal: str = ""
    # 健康度与冷却
    fail_streak: int = 0
    ok_streak: int = 0
    risk_streak: int = 0
    cooldown_until: float = 0.0
    health: float = 1.0
    paused: bool = False
    paused_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)
    # 统计
    push_ok: int = 0

    @property
    def key(self) -> str:
        return account_key(self.platform, self.account_id)

    @property
    def label(self) -> str:
        return self.name or self.account_id

    def cooling(self, now: float | None = None) -> bool:
        return float(self.cooldown_until or 0) > (time.time() if now is None else now)

    def due(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return (
            self.enabled
            and not self.paused
            and not self.cooling(moment)
            and float(self.next_check_ts or 0) <= moment
        )

    def mark_seen(self, post_id: str) -> None:
        pid = str(post_id)
        if not pid:
            return
        if pid in self.seen_ids:
            self.seen_ids.remove(pid)
        self.seen_ids.append(pid)
        if len(self.seen_ids) > C.SEEN_IDS_LIMIT:
            del self.seen_ids[: len(self.seen_ids) - C.SEEN_IDS_LIMIT]

    def seen(self, post_id: str) -> bool:
        return str(post_id) in self.seen_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "name": self.name,
            "url": self.url,
            "feed_url": self.feed_url,
            "enabled": self.enabled,
            "baseline_done": self.baseline_done,
            "seen_ids": list(self.seen_ids),
            "next_check_ts": self.next_check_ts,
            "last_check_ts": self.last_check_ts,
            "last_ok_ts": self.last_ok_ts,
            "last_risk_ts": self.last_risk_ts,
            "last_error": self.last_error,
            "last_signal": self.last_signal,
            "fail_streak": self.fail_streak,
            "ok_streak": self.ok_streak,
            "risk_streak": self.risk_streak,
            "cooldown_until": self.cooldown_until,
            "health": self.health,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "history": list(self.history[-C.HISTORY_LIMIT :]),
            "created_ts": self.created_ts,
            "push_ok": self.push_ok,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnsAccount | None:
        if not isinstance(data, dict):
            return None
        platform = str(data.get("platform") or "")
        account_id = str(data.get("account_id") or "")
        if not platform or not account_id:
            return None
        created = _safe_float(data.get("created_ts"), time.time())
        history: list[dict[str, Any]] = []
        for row in data.get("history") or []:
            if isinstance(row, dict) and row.get("outcome"):
                history.append(
                    {
                        "ts": _safe_float(row.get("ts"), 0.0),
                        "outcome": str(row.get("outcome") or ""),
                    }
                )
        return cls(
            platform=platform,
            account_id=account_id,
            name=str(data.get("name") or ""),
            url=str(data.get("url") or ""),
            feed_url=str(data.get("feed_url") or ""),
            enabled=bool(data.get("enabled", True)),
            baseline_done=bool(data.get("baseline_done", False)),
            seen_ids=[str(i) for i in (data.get("seen_ids") or []) if i][-C.SEEN_IDS_LIMIT :],
            next_check_ts=_safe_float(data.get("next_check_ts"), 0.0),
            last_check_ts=_safe_float(data.get("last_check_ts"), 0.0),
            last_ok_ts=_safe_float(data.get("last_ok_ts"), 0.0),
            last_risk_ts=_safe_float(data.get("last_risk_ts"), 0.0),
            last_error=str(data.get("last_error") or ""),
            last_signal=str(data.get("last_signal") or ""),
            fail_streak=_safe_int(data.get("fail_streak"), 0),
            ok_streak=_safe_int(data.get("ok_streak"), 0),
            risk_streak=_safe_int(data.get("risk_streak"), 0),
            cooldown_until=_safe_float(data.get("cooldown_until"), 0.0),
            health=_safe_float(data.get("health"), 1.0),
            paused=bool(data.get("paused", False)),
            paused_reason=str(data.get("paused_reason") or ""),
            history=history[-C.HISTORY_LIMIT :],
            created_ts=created,
            push_ok=_safe_int(data.get("push_ok"), 0),
        )
