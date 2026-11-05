"""多平台订阅状态落盘：`<插件数据目录>/sns_state.json`。

单独一个文件、不和微博的 `state.json` 混在一起，理由是两者生命周期不同：
微博的 state 里装待推送队列与游客身份，SNS 的装订阅名单、节奏与风控台账。
混在一个文件里，任何一侧的字段结构升级都会逼另一侧跟着迁移。

写盘一律"先写 `.tmp` 再 `replace`"：进程死在写入中途不会留下截断的 JSON
（截断意味着下次启动丢掉全部订阅名单）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import constants as C
from .types import SnsAccount

logger = logging.getLogger("astrbot_plugin_weibo_suite.sns")

# 已知的标量统计键。load() 只保留这些键：面板/服务层只读它们，
# 手工编辑混进来的未知字段一律丢掉，免得 bump() 每轮都炸。
# push_unknown 单列：unknown 是"可能已送达"，既不能算失败重试（会重复推送），
# 也不能记成成功（面板上的推送量会虚高）。
_STAT_KEYS = ("check", "push_ok", "push_fail", "push_unknown", "risk", "baseline")


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
        # 投递补发队列（随状态文件落盘）：每条是 {key, platform, account_id,
        # post_id, session, payload, attempts, ts}，只装"明确失败"的会话。
        self.retry_queue: list[dict[str, Any]] = []
        # 是否成功读过盘。没读过就写，等于用一个空状态盖掉用户的订阅名单——
        # 这是本文件最不可接受的失败，所以 save() 会拒绝。
        self._loaded = False
        # save_async 把落盘丢线程池后，同步 save()（add_account/remove_account
        # 这类同步入口）可能与线程里的写并发；两处都写同一个 .tmp，必须串行
        self._file_lock = threading.Lock()
        # 构造即加载：状态对象在任何使用点都应该是"磁盘上的那个状态"，而不是
        # 一个空壳。以前只有 `SnsService.start()` 会 load，于是
        # `sns.enabled=false` 时面板/指令拿到的是空状态，一次 save 就把用户的
        # 订阅名单清空了（P1-1）。把加载放进构造函数后，这个坑从结构上消失。
        self.load()

    # ------------------------------------------------------------ 路径

    @property
    def path(self) -> Path:
        return self.data_dir / "sns_state.json"

    # ------------------------------------------------------------ 读写

    def load(self, *, force: bool = False) -> None:
        """从磁盘读回状态。幂等；读失败时**禁止**后续 save 覆盖原文件。

        `force=True` 给"别的实例刚写过盘"的场景用（正常路径不需要）。
        """
        if self._loaded and not force:
            return
        # 先假设"没读到、也不能写"，下面每个成功分支再置 True。
        self._loaded = False
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            # 没有文件 = 本来就没什么可丢的，允许之后写盘
            self._loaded = True
            return
        except Exception as exc:
            # 坏文件绝不能静默吞掉：下一次 save 会用空状态盖掉用户的订阅名单。
            # 先原样留档，再决定能不能重建（备份失败时连写盘都禁止）。
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            moved = False
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
                moved = True
            logger.warning(
                f"[sns] 状态文件解析失败（{exc.__class__.__name__}）："
                f"{'已备份为 ' + backup.name if moved else '备份失败，已禁止写盘'}，"
                "本次按空状态运行"
            )
            self._loaded = moved
            return
        if not isinstance(data, dict):
            self._loaded = True
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
        retry = data.get("retry_queue")
        if isinstance(retry, list):
            self.retry_queue = [row for row in retry if _valid_retry(row)][-C.RETRY_QUEUE_LIMIT :]
        self._loaded = True

    def _snapshot(self) -> dict:
        return {
            "version": self.VERSION,
            "created_ts": self.created_ts,
            "accounts": [a.to_dict() for a in self.accounts.values()],
            "events": self.events[-C.EVENTS_LIMIT :],
            "stats": self.stats,
            "platform_risk": self.platform_risk,
            "platform_paused_until": self.platform_paused_until,
            "retry_queue": self.retry_queue[-C.RETRY_QUEUE_LIMIT :],
        }

    def _write_file(self, state: dict) -> bool:
        """把快照原子落盘（含 fsync）；纯阻塞 I/O，供同步/线程路径复用。"""
        tmp = self.path.with_suffix(".json.tmp")
        with self._file_lock:
            return self._write_file_locked(state, tmp)

    def _write_file_locked(self, state: dict, tmp: Path) -> bool:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(state, ensure_ascii=False, indent=2)
            # fsync 之后再 replace：断电时宁可留下旧的完整文件，也不要"rename 成功
            # 但内容还在页缓存里"的半截状态。
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
            _restrict(self.path)
            return True
        except Exception as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            logger.warning(f"[sns] 状态写入失败（{exc.__class__.__name__}: {exc}），本次改动未落盘")
            return False

    def save(self) -> bool:
        """原子落盘，返回是否真的写下去了。

        护栏：从没成功 load() 过（例如状态文件读坏且备份失败）时拒绝写——
        用空状态盖掉用户的订阅名单，比"这次没保存"严重得多。写失败也不再
        静默：日志里必须留痕，否则用户以为订阅存下了。
        """
        if not self._loaded:
            logger.error("[sns] 状态尚未成功加载，拒绝写盘（否则会覆盖用户已有订阅）")
            return False
        return self._write_file(self._snapshot())

    async def save_async(self) -> bool:
        """事件循环专用：快照在当前线程取，fsync/rename 丢线程池。

        快照只读一次内存结构，线程里拿到的已经是纯 dict，不会与后续订阅
        变更抢同一个对象；写盘本身串行化交给磁盘。
        """
        if not self._loaded:
            logger.error("[sns] 状态尚未成功加载，拒绝写盘（否则会覆盖用户已有订阅）")
            return False
        state = self._snapshot()
        return await asyncio.to_thread(self._write_file, state)

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


def _valid_retry(row: Any) -> bool:
    """补发队列的行校验：缺字段/类型不对的行直接丢，别让坏数据卡住整轮补发。"""
    if not isinstance(row, dict):
        return False
    if not isinstance(row.get("payload"), dict):
        return False
    return all(str(row.get(key) or "") for key in ("platform", "account_id", "post_id", "session"))


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
