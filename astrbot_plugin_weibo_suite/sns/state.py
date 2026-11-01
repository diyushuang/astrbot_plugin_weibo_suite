"""多平台订阅状态落盘：`<插件数据目录>/sns_state.json`。

单独一个文件、不和微博的 `state.json` 混在一起，理由是两者生命周期不同：
微博的 state 里装待推送队列与游客身份，SNS 的装订阅名单、节奏与风控台账。
混在一个文件里，任何一侧的字段结构升级都会逼另一侧跟着迁移。

写盘一律"先写 `.tmp` 再 `replace`"：进程死在写入中途不会留下截断的 JSON
（截断意味着下次启动丢掉全部订阅名单）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import constants as C
from .types import SnsAccount

logger = logging.getLogger("astrbot_plugin_weibo_suite.sns")

# 已知的标量统计键。load() 只保留这些键：面板/服务层只读它们，
# 手工编辑混进来的未知字段一律丢掉，免得 bump() 每轮都炸。
_STAT_KEYS = ("check", "push_ok", "push_fail", "risk", "baseline")


class SnsState:
    """订阅名单 + 节奏 + 风控台账 + 最近动态。

    只做读写，不做业务判断：谁该暂停、谁该冷却由 `guard` / `service` 决定，
    这样状态文件永远可以被手工编辑修坏再修回来。
    """

    VERSION = 1

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.accounts: dict[str, SnsAccount] = {}
        self.events: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {
            "check": 0,
            "push_ok": 0,
            "push_fail": 0,
            "risk": 0,
            "baseline": 0,
            "by_platform": {},  # platform -> {"check","push_ok","risk"}
        }
        # 平台级风控时间戳：熔断判定与面板展示共用
        self.platform_risk: dict[str, list[float]] = {p: [] for p in C.PLATFORMS}
        # 平台级暂停到什么时候（熔断）
        self.platform_paused_until: dict[str, float] = {p: 0.0 for p in C.PLATFORMS}
        # 首次启动时间：保号模式从这里开始计时
        self.created_ts = time.time()

    # ------------------------------------------------------------ 路径

    @property
    def path(self) -> Path:
        return self.data_dir / "sns_state.json"

    # ------------------------------------------------------------ 读写

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as exc:
            # 坏文件绝不能静默吞掉：下一次 save 会用空状态盖掉用户的订阅名单。
            # 先把坏文件原样留档，重建空状态，并在日志里点名路径。
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
            logger.warning(
                f"[sns] 状态文件解析失败（{self.path}）：{exc}；已备份到 {backup}，本次按空状态重建"
            )
            return
        if not isinstance(data, dict):
            return
        accounts: dict[str, SnsAccount] = {}
        for row in data.get("accounts") or []:
            account = SnsAccount.from_dict(row)
            if account is not None:
                accounts[account.key] = account
        self.accounts = accounts
        events = data.get("events")
        if isinstance(events, list):
            self.events = [e for e in events if isinstance(e, dict)][-C.EVENTS_LIMIT :]
        stats = data.get("stats")
        if isinstance(stats, dict):
            self.stats = _normalize_stats(stats)
        risk = data.get("platform_risk")
        if isinstance(risk, dict):
            for platform in C.PLATFORMS:
                rows = risk.get(platform)
                if isinstance(rows, list):
                    self.platform_risk[platform] = [
                        float(ts) for ts in rows if isinstance(ts, (int, float))
                    ][-50:]
        paused = data.get("platform_paused_until")
        if isinstance(paused, dict):
            for platform in C.PLATFORMS:
                with contextlib.suppress(TypeError, ValueError):
                    self.platform_paused_until[platform] = float(paused.get(platform) or 0)
        with contextlib.suppress(TypeError, ValueError):
            self.created_ts = float(data.get("created_ts") or self.created_ts)

    def save(self) -> None:
        state = {
            "version": self.VERSION,
            "created_ts": self.created_ts,
            "accounts": [a.to_dict() for a in self.accounts.values()],
            "events": self.events[-C.EVENTS_LIMIT :],
            "stats": self.stats,
            "platform_risk": self.platform_risk,
            "platform_paused_until": self.platform_paused_until,
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            _restrict(self.path)
        except Exception:
            with contextlib.suppress(OSError):
                self.path.with_suffix(".json.tmp").unlink(missing_ok=True)

    # ------------------------------------------------------------ 便捷访问

    def get(self, platform: str, account_id: str = "") -> SnsAccount | None:
        """按 `(platform, account_id)` 或直接按 `platform:id` 取号。"""
        return self.accounts.get(_key(platform, account_id))

    def add(self, account: SnsAccount) -> SnsAccount:
        self.accounts[account.key] = account
        return account

    def remove(self, platform: str, account_id: str = "") -> SnsAccount | None:
        return self.accounts.pop(_key(platform, account_id), None)

    def by_platform(self, platform: str = "") -> list[SnsAccount]:
        rows = list(self.accounts.values())
        if platform:
            rows = [a for a in rows if a.platform == platform]
        return sorted(rows, key=lambda a: (a.platform, a.created_ts))

    def record(
        self,
        kind: str,
        *,
        platform: str = "",
        name: str = "",
        text: str = "",
        detail: str = "",
    ) -> None:
        """记一条最近动态（面板"最近事件"卡片用），自带截断防膨胀。"""
        self.events.append(
            {
                "ts": time.time(),
                "kind": str(kind),
                "platform": str(platform),
                "name": str(name)[:60],
                "text": str(text)[:120],
                "detail": str(detail)[:200],
            }
        )
        if len(self.events) > C.EVENTS_LIMIT:
            del self.events[: len(self.events) - C.EVENTS_LIMIT]

    def bump(self, platform: str, key: str, delta: int = 1) -> None:
        """累加统计：总量与分平台各记一份。

        全程防御式取值：统计字段是"饰品"，被手工改坏也不该让整轮检查抛错。
        """
        key = str(key)
        self.stats[key] = _stat_int(self.stats.get(key)) + delta
        buckets = self.stats.get("by_platform")
        if not isinstance(buckets, dict):
            buckets = {}
            self.stats["by_platform"] = buckets
        bucket = buckets.get(platform or "-")
        if not isinstance(bucket, dict):
            bucket = {}
            buckets[platform or "-"] = bucket
        bucket[key] = _stat_int(bucket.get(key)) + delta


def _stat_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_stats(raw: dict[str, Any]) -> dict[str, Any]:
    """把落盘的 stats 规整成"已知键 -> int + by_platform -> {平台: {键: int}}"。

    只收 _STAT_KEYS 里的标量键与 by_platform 里的整数桶：未知键、字符串、
    None、嵌套怪结构全部丢弃，读回来永远是可以安全 `+ 1` 的形状。
    """
    stats: dict[str, Any] = {key: 0 for key in _STAT_KEYS}
    stats["by_platform"] = {}
    for key in _STAT_KEYS:
        stats[key] = _stat_int(raw.get(key))
    buckets = raw.get("by_platform")
    if isinstance(buckets, dict):
        for platform, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            clean = {key: _stat_int(bucket.get(key)) for key in _STAT_KEYS}
            stats["by_platform"][str(platform)] = clean
    return stats


def _key(platform: str, account_id: str = "") -> str:
    """服务层手里常常只有 `platform:id` 这个合成键，这里两种入参都接受。"""
    text = str(platform or "")
    if account_id:
        return f"{text}:{account_id}"
    return text


def _restrict(path: Path) -> None:
    """尽量把状态文件收紧到仅属主可读写：里面虽不存 Cookie，但有订阅名单。"""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)
