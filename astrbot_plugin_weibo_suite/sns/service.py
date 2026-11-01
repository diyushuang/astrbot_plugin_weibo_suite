"""订阅服务：把"取数 → 判新旧 → 守卫 → 投递"串成一条闭环。

职责边界刻意划得很清：

- **取数**只通过 `fetch_m1` / `fetch_m2` / RSS 三条通道，服务层不碰网络细节；
- **判新旧**只认 `seen_ids`（基线期只记录不推送，永不补推历史）；
- **守卫**（冷却、平台熔断、健康度暂停）全部委托 `guard`，服务层不自己发明退避；
- **投递**通过 `plugin.deliver_sns_payload()` 走微博域已有的发送链（超时、@全体
  配额、媒体被拒降级都在那边），这样两个域只有一条发送路径，不会各写一套。

一条铁律写在这里而不是散在各处：**空响应/结构不符一律按错误处理**。抖音风控就是
"HTTP 200 + status_code 0 + 空列表"，把它当"没有更新"会让用户以为插件正常，实际
账号已经在冷却里躺着。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import aiohttp

from . import constants as C
from . import fetch_m1, fetch_m2, scheduler
from .adapters import parse_account_id, parse_posts
from .adapters import rss as rss_adapter
from .browser import BrowserSession, BrowserUnavailable
from .guard import GuardConfig, apply_result, platform_tripped
from .identity import IdentityStore
from .state import SnsState
from .types import FetchResult, SnsAccount, SnsPost, account_key

_MISSING = object()


def _logger():
    try:
        from astrbot.api import logger  # type: ignore

        return logger
    except Exception:
        import logging

        return logging.getLogger("astrbot_plugin_weibo_suite.sns")


class SnsService:
    """三平台订阅的调度与守卫。由插件在 `initialize()` 里创建并 `start()`。"""

    def __init__(self, plugin: Any, *, logger: Any = None):
        self.p = plugin
        self.log = logger or _logger()
        data_dir = getattr(plugin, "_data_dir", "") or ""
        self.data_dir = Path(data_dir) if data_dir else Path(".")
        self.state = SnsState(self.data_dir)
        self.identity = IdentityStore(self.data_dir)
        self._browser: BrowserSession | None = None
        self._browser_error = ""
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._running_round = False
        self._running = False
        self._last_round_ts = 0.0
        self._last_round: dict[str, Any] = {}

    # ------------------------------------------------------------ 配置

    def cfg(self, key: str, default: Any = _MISSING) -> Any:
        """读 `sns` 配置组；缺省回落到 `constants.CFG_KEYS`。"""
        group = self.p.config.get(C.CFG_GROUP) if isinstance(self.p.config, dict) else None
        if isinstance(group, dict) and key in group:
            value = group[key]
            if value is not None:
                return value
        if default is not _MISSING:
            return default
        return C.CFG_KEYS.get(key)

    def _int(self, key: str, fallback: int) -> int:
        try:
            return int(self.cfg(key, fallback))
        except (TypeError, ValueError):
            return int(fallback)

    def _float(self, key: str, fallback: float) -> float:
        try:
            return float(self.cfg(key, fallback))
        except (TypeError, ValueError):
            return float(fallback)

    def _bool(self, key: str, fallback: bool) -> bool:
        value = self.cfg(key, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    @property
    def enabled(self) -> bool:
        return self._bool("enabled", True)

    @property
    def fetch_mode(self) -> str:
        mode = str(self.cfg("fetch_mode", "browser") or "browser").strip().lower()
        return mode if mode in ("browser", "http", "rss") else "browser"

    def base_interval(self, platform: str) -> int:
        floor = C.MIN_BASE_INTERVAL.get(platform, 300)
        key = f"base_interval_{platform}"
        if self._bool("aggressive", False):
            cap = C.AGGRESSIVE_BASE_INTERVAL.get(platform, floor)
            return max(60, int(min(self._int(key, floor), cap)))
        return max(floor, self._int(key, floor))

    def concurrency(self, platform: str) -> int:
        explicit = self._int("platform_concurrency", 0)
        if explicit > 0:
            return max(1, explicit)
        return max(1, C.PLATFORM_CONCURRENCY.get(platform, 1))

    def guard_config(self) -> GuardConfig:
        return GuardConfig(
            base_cooldown=self._int("cooldown_base_seconds", C.COOLDOWN_BASE_DEFAULT),
            cooldown_cap=self._int("cooldown_cap_seconds", C.COOLDOWN_CAP_DEFAULT),
            health_degrade=self._float("health_degrade_threshold", C.HEALTH_DEGRADE_DEFAULT),
            health_pause=min(
                self._float("health_pause_threshold", C.HEALTH_PAUSE_DEFAULT),
                self._float("health_degrade_threshold", C.HEALTH_DEGRADE_DEFAULT),
            ),
        )

    def sessions(self) -> list[str]:
        own = [str(s) for s in (self.cfg("push_sessions") or []) if str(s).strip()]
        if own:
            return own
        # 留空就复用微博域的推送目标：用户只配一次群，两个域都推到这里
        getter = getattr(self.p, "_sessions", None)
        if callable(getter):
            try:
                return [str(s) for s in (getter() or [])]
            except Exception:
                return []
        return []

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self.state.load()
        self._running = True
        if self.enabled and self.fetch_mode == "browser":
            # 启动时不主动拉起浏览器：用户没点"打开登录"之前，开窗口只会吓人。
            self._browser = self._make_browser()
        self._task = asyncio.create_task(self._poll_loop())
        self.log.info(
            f"[sns] 订阅服务已启动：{len(self.state.accounts)} 个订阅，取数模式={self.fetch_mode}"
        )

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        browser, self._browser = self._browser, None
        if browser is not None:
            await browser.stop()
        with contextlib.suppress(Exception):
            self.state.save()

    def _make_browser(self) -> BrowserSession:
        root = str(self.cfg("browser_profile_dir") or "").strip()
        profile = Path(root) if root else self.identity.profile_root
        return BrowserSession(
            profile,
            headed=self._bool("browser_headed", True),
            logger=self.log,
        )

    @property
    def browser(self) -> BrowserSession:
        """惰性创建：即使启动时是 http 模式，用户中途切回浏览器也能用。"""
        if self._browser is None:
            self._browser = self._make_browser()
        return self._browser

    # ------------------------------------------------------------ 订阅管理

    def add_account(self, platform: str, text: str) -> tuple[bool, str, SnsAccount | None]:
        """按链接/裸 id 新增订阅；返回 (成功, 提示, 账号)。"""
        platform = str(platform or "").strip().lower()
        if platform not in C.PLATFORMS:
            return False, f"不支持的平台：{platform}", None
        account_id = parse_account_id(platform, text)
        if not account_id:
            label = C.PLATFORM_LABEL.get(platform, platform)
            return False, f"没能从「{text}」里认出{label}账号", None
        key = account_key(platform, account_id)
        existing = self.state.get(key)
        if existing is not None:
            return False, f"已经订阅过：{existing.label}", existing
        now = time.time()
        account = SnsAccount(
            platform=platform,
            account_id=account_id,
            url=C.PLATFORM_ACCOUNT_URL.get(platform, "").format(account_id=account_id),
            # 首次检查仍很快，但在一整个节奏内与同平台其它新号错开：
            # 一次连加 N 个号全在同一秒开火，本身就是最明显的爬虫特征。
            next_check_ts=self._staggered_check_ts(platform, now),
            created_ts=now,
        )
        self.state.accounts[key] = account
        self.state.save()
        return True, f"已订阅{C.PLATFORM_LABEL.get(platform, platform)}：{account.label}", account

    async def add_feed(self, url: str, name: str = "") -> tuple[bool, str, SnsAccount | None]:
        """M3：直接订阅一个 RSS 源（给"平台实在取不动"时留的退路）。"""
        raw = str(url or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            return False, "RSS 订阅需要一个 http(s) 地址", None
        key = account_key("rss", raw)
        if self.state.get(key) is not None:
            return False, "这个 RSS 源已经在订阅里了", None
        now = time.time()
        account = SnsAccount(
            platform="rss",
            account_id=raw,
            name=name or raw,
            url=raw,
            feed_url=raw,
            next_check_ts=self._staggered_check_ts("rss", now),
            created_ts=now,
        )
        self.state.accounts[key] = account
        self.state.save()
        return True, f"已订阅 RSS：{account.label}", account

    def _staggered_check_ts(self, platform: str, now: float) -> float:
        """新订阅的首检时刻：在窗口内错开，而不是全部等于 `now`。

        一次连加 N 个号时，它们分别落在窗口的 0、1/N、2/N…… 位置：既不会同一
        秒全部开火（那本身就是最明显的爬虫特征），也不会让用户加完订阅后等
        好几分钟才看到第一次结果——窗口上限 60s。
        """
        interval = float(self.base_interval(platform) or 300)
        count = max(1, len(self.state.by_platform(platform)) + 1)
        window = max(1.0, min(interval, 60.0))
        slots = scheduler.stagger_offsets(count, window, now=now)
        return slots[-1] if slots else now

    def remove_account(self, platform: str, account_id: str) -> tuple[bool, str]:
        raw = str(account_id or "").strip()
        # 面板传的是复合键 "platform:id"，指令传的是裸 id/链接：两种都要认
        prefix = f"{platform}:"
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
        account_id = raw
        key = account_key(platform, account_id)
        if self.state.get(key) is None:
            # 允许用户直接拿 id/链接来删（省得先查列表）
            resolved = parse_account_id(platform, account_id)
            if resolved:
                key = account_key(platform, resolved)
        account = self.state.remove(key)
        if account is None:
            return False, "订阅里没有这一项"
        self.state.save()
        return True, f"已取消订阅：{account.label}"

    async def open_login(self, platform: str) -> tuple[bool, str]:
        """打开持久化窗口并导航到登录页（用户在里面手动登录一次）。"""
        if platform not in C.PLATFORMS:
            return False, f"不支持的平台：{platform}"
        try:
            url = await self.browser.open_login(platform)
        except BrowserUnavailable as exc:
            self._browser_error = str(exc)
            return False, str(exc)
        except Exception as exc:
            self._browser_error = f"{exc.__class__.__name__}: {exc}"
            return False, self._browser_error
        self._browser_error = ""
        return True, f"已打开{C.PLATFORM_LABEL.get(platform, platform)}登录窗口：{url}"

    def mark_logged_in(self, platform: str, note: str = "") -> None:
        self.identity.mark_login(platform, True, note=note)

    # ------------------------------------------------------------ 主循环

    async def _poll_loop(self) -> None:
        await asyncio.sleep(8)  # 让插件初始化、配置同步先跑完
        while self._running:
            try:
                if self.enabled and self.state.accounts:
                    await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(f"[sns] 轮询异常：{exc.__class__.__name__}: {exc}")
                self.state.record(
                    "error", text=f"轮询异常 {exc.__class__.__name__}", detail=str(exc)[:200]
                )
            await asyncio.sleep(self._next_sleep())

    def _next_sleep(self) -> float:
        """下一次醒来的间隔：贴着"最近一个到点账号"睡，而不是死等固定值。"""
        now = time.time()
        paused_platforms = {
            str(p)
            for p, until in self.state.platform_paused_until.items()
            if float(until or 0.0) > now
        }
        candidates: list[float] = []
        for account in self.state.accounts.values():
            if not account.enabled or account.platform in paused_platforms:
                continue
            if account.paused:
                # 暂停账号只在冷却结束后探活一次，用冷却截止时间当闹钟
                candidates.append(float(account.cooldown_until or 0.0))
            else:
                candidates.append(
                    max(float(account.next_check_ts or 0.0), float(account.cooldown_until or 0.0))
                )
        # 熔断到点也要醒一次，否则平台永远没机会回到调度里
        candidates.extend(
            float(until or 0.0)
            for until in self.state.platform_paused_until.values()
            if float(until or 0.0) > now
        )
        if not candidates:
            return 30.0
        delta = min(candidates) - now
        # 30s 下限防止空转；300s 上限保证"改完配置/风控恢复"能较快被感知
        return max(5.0, min(300.0, delta if delta > 0 else 5.0))

    async def check_all(self, platform: str = "", *, force: bool = False) -> dict[str, Any]:
        """跑一轮：按调度挑出到点的账号，逐个（受并发上限约束）检查。

        `force=True` 供"立即检查"用：不看 `next_check_ts`，但冷却/暂停的账号
        照旧不动（手动操作也不该把风控中的账号提前叫醒）。
        """
        if self._running_round:
            return {"ok": False, "error": "正在检查中：上一轮还没跑完"}
        try:
            # 短等锁，而不是"看一眼 locked() 就返回"：并发调用不会双跑，
            # 也不会把"上一轮刚好跑完"误报成 busy。
            await asyncio.wait_for(self._lock.acquire(), timeout=0.5)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "正在检查中：上一轮还没跑完"}
        self._running_round = True
        try:
            now = time.time()
            # 熔断到期就清掉，避免"过期的暂停"一直挂在状态里（也顺手修历史遗留的旧值）
            for p in [
                key
                for key, until in list(self.state.platform_paused_until.items())
                if float(until or 0.0) <= now
            ]:
                self.state.platform_paused_until.pop(p, None)
            accounts = (
                self.state.by_platform(platform) if platform else list(self.state.accounts.values())
            )
            planned = scheduler.plan_round(
                accounts,
                now,
                per_platform_limit={p: self.concurrency(p) for p in C.PLATFORMS},
                platform_paused={
                    p: self.state.platform_paused_until.get(p, 0.0) for p in C.PLATFORMS
                },
                force=force,
            )
            if not planned:
                # `detail` 永远保持列表：调用方（指令层/面板）按列表遍历，
                # 这里换成字符串会让 `row.get(...)` 直接炸掉
                return {
                    "ok": True,
                    "checked": 0,
                    "pushed": 0,
                    "detail": [],
                    "note": "没有需要检查的订阅（可能都在冷却或未到点）",
                }
            results = []
            for account in planned:
                results.append(await self._check_account(account))
                await asyncio.sleep(0.8)  # 同一轮里也别把请求拍成一串（人类会停顿）
            self.state.save()
            self._last_round_ts = now
            self._last_round = {
                "checked": len(results),
                "pushed": sum(1 for r in results if r.get("pushed")),
                "detail": results,
            }
            return {"ok": True, **self._last_round}
        finally:
            self._running_round = False
            self._lock.release()

    async def _check_account(self, account: SnsAccount) -> dict[str, Any]:
        """检查单个订阅：取数 → 比对 → 更新状态 → 决定是否推送。"""
        now = time.time()
        account.last_check_ts = now
        self.state.bump(account.platform, "check")
        result = await self._fetch_with_deadline(account)
        base = self.base_interval(account.platform)
        warmup = scheduler.warmup_active(
            account.created_ts, now, minutes=self._int("warmup_minutes", C.WARMUP_MINUTES_DEFAULT)
        )
        night = scheduler.night_factor(
            now,
            enabled=self._bool("night_slowdown", True),
            start_hour=C.NIGHT_START_HOUR,
            end_hour=C.NIGHT_END_HOUR,
        )
        cfg = self.guard_config()

        if not result.ok:
            applied = apply_result(account, result, cfg, now=now)
            account.last_signal = result.signal
            account.last_error = result.label
            self._annotate_history(account, result)
            self.state.record(
                "risk" if result.outcome == C.OUTCOME_RISK else "error",
                platform=account.platform,
                name=account.label,
                text=result.label,
                detail=result.signal,
            )
            self._maybe_trip_platform(account.platform, result, now)
            interval = self._interval_for(account, base, warmup=warmup, night=night)
            account.next_check_ts = scheduler.next_check_time(account, interval, now=now)
            return {
                "key": account.key,
                "ok": False,
                "signal": result.signal,
                "label": result.label,
                "paused": applied.get("paused", False),
                "cooldown": applied.get("cooldown", 0),
            }

        # ---- 成功（含"正常但没有内容"） ----
        applied = apply_result(account, result, cfg, now=now)
        account.last_signal = result.signal
        self._annotate_history(account, result)
        if result.nickname and (not account.name or account.name == account.account_id):
            account.name = result.nickname

        if not account.baseline_done:
            # 基线：把现有内容全记成已读，一条都不推（"只建立基线"是防封的硬规矩）
            for post in result.posts:
                account.mark_seen(post.post_id)
            account.baseline_done = True
            self.state.bump(account.platform, "baseline")
            self.state.record(
                "baseline",
                platform=account.platform,
                name=account.label,
                text=f"已建立基线，{len(result.posts)} 条历史不推送",
            )
            interval = self._interval_for(account, base, warmup=warmup, night=night)
            account.next_check_ts = scheduler.next_check_time(account, interval, now=now)
            return {"key": account.key, "ok": True, "baseline": True, "new": 0, "pushed": False}

        fresh = [p for p in result.posts if not account.seen(p.post_id)]
        fresh.sort(key=lambda p: (p.ts or 0.0, p.post_id))
        if not fresh:
            interval = self._interval_for(account, base, warmup=warmup, night=night)
            account.next_check_ts = scheduler.next_check_time(account, interval, now=now)
            return {"key": account.key, "ok": True, "new": 0, "pushed": False}

        limit = max(1, C.MAX_PUSH_PER_ROUND)
        # `fresh` 是按时间升序的：取尾部才是最新的几条。取头部会把最新的悄悄
        # 标成已读，用户永远看不到刚发的内容。
        batch = fresh[-limit:]
        if len(fresh) > limit:
            # 一轮只推最新的几条，更早的直接记成已读：宁可漏推历史，也不给平台
            # "批量大扫"的机会；用户真要补，用「订阅 检测」逐轮跑。
            for post in fresh[:-limit]:
                account.mark_seen(post.post_id)
            self.state.record(
                "info",
                platform=account.platform,
                name=account.label,
                text=f"本轮 {len(fresh)} 条更新，只推最新 {limit} 条",
            )
        delivered = await self._deliver(account, batch, warmup=warmup)
        # 只把"真的送出去了"的标记成已读：投递失败（没绑群、通道异常）时留着下一轮重试
        delivered_ids = {str(pid) for pid in (delivered.get("delivered") or ())}
        for post in batch:
            if post.post_id in delivered_ids:
                account.mark_seen(post.post_id)
        account.push_ok += delivered.get("sent", 0)
        interval = self._interval_for(account, base, warmup=warmup, night=night)
        account.next_check_ts = scheduler.next_check_time(account, interval, now=now)
        return {
            "key": account.key,
            "ok": True,
            "new": len(fresh),
            "pushed": bool(delivered.get("sent")),
            "sent": delivered.get("sent", 0),
            "failed": delivered.get("failed", 0),
        }

    def _trim_history(self, account: SnsAccount) -> None:
        if len(account.history) > C.HISTORY_LIMIT:
            del account.history[: len(account.history) - C.HISTORY_LIMIT]

    def _annotate_history(self, account: SnsAccount, result: FetchResult) -> None:
        """给 `apply_result` 刚写下的那条历史补上信号/细节。

        `apply_result` 已经 append 过一条 `{"ts", "outcome"}`；这里**不再**追加
        第二条，否则 `HISTORY_LIMIT` 实际只覆盖一半的检查，健康度/成功率样本被腰斩。
        """
        if not account.history:
            return
        last = account.history[-1]
        if isinstance(last, dict):
            last.setdefault("signal", result.signal)
            last["detail"] = (result.label or "")[:120]
        self._trim_history(account)

    def _interval_for(self, account: SnsAccount, base: int, *, warmup: bool, night: float) -> float:
        rng = None
        ratio = self._float("jitter_ratio", 0.0)
        if ratio <= 0:
            ratio = C.JITTER_RATIO.get(account.platform, 0.2)
        protect = now_within(account.created_ts, self._int("protect_mode_hours", 0) * 3600)
        return scheduler.effective_interval(
            base,
            account_count=max(1, len(self.state.by_platform(account.platform))),
            concurrency=self.concurrency(account.platform),
            health=account.health,
            degrade_threshold=self._float("health_degrade_threshold", C.HEALTH_DEGRADE_DEFAULT),
            # 热身期与保号期都翻倍：新订阅刚加进来就高频扫，是最容易被盯上的时刻
            warmup=warmup or protect,
            night=night,
            rng=rng,
            jitter_ratio=ratio,
            platform=account.platform,
        )

    def _maybe_trip_platform(self, platform: str, result: FetchResult, now: float) -> None:
        """单平台 3 次风控信号 / 10 分钟 → 该平台整体暂停 30 分钟。"""
        if result.outcome != C.OUTCOME_RISK:
            return
        stamps = self.state.platform_risk.setdefault(platform, [])
        stamps.append(now)
        del stamps[:-50]
        if platform_tripped(stamps, now, hits=C.PLATFORM_TRIP_HITS, window=C.PLATFORM_TRIP_WINDOW):
            until = now + C.PLATFORM_PAUSE_SECONDS
            self.state.platform_paused_until[platform] = until
            self.state.record(
                "risk",
                platform=platform,
                text=f"平台熔断：{C.PLATFORM_LABEL.get(platform, platform)}暂停 30 分钟",
                detail=f"{C.PLATFORM_TRIP_HITS} 次风控信号落在 {C.PLATFORM_TRIP_WINDOW}s 内",
            )

    # ------------------------------------------------------------ 取数

    def _cookie_for(self, platform: str) -> str:
        configured = str(self.cfg(f"{platform}_cookie", "") or "").strip()
        if configured:
            return configured
        return self.identity.stored_cookie(platform)

    async def _fetch_with_deadline(self, account: SnsAccount) -> FetchResult:
        """给单账号取数加一层硬截止，避免一个卡死的源拖住整轮检查。

        浏览器通道的 goto + XHR 各自已有超时，这里只做"整条链路"的兜底，
        取值必须大于单步超时之和，否则正常慢页面会被误判超时。
        """
        deadline = self._fetch_deadline(account)
        try:
            return await asyncio.wait_for(self._fetch(account), timeout=deadline)
        except asyncio.TimeoutError:
            return FetchResult.failure("net.timeout", f"抓取超过 {int(deadline)}s 未完成（已中止）")

    def _fetch_deadline(self, account: SnsAccount) -> float:
        if account.platform == "rss":
            return float(C.HTTP_TIMEOUT_SECONDS) + 5.0
        if self.fetch_mode == "http":
            return float(C.HTTP_TIMEOUT_SECONDS) * 2 + 10.0
        # 浏览器：单个页面 goto 与 XHR 各有一次 BROWSER_TIMEOUT_MS 预算
        return float(C.BROWSER_TIMEOUT_MS) / 1000.0 * 2 + 5.0

    async def _fetch(self, account: SnsAccount) -> FetchResult:
        platform = account.platform
        mode = self.fetch_mode
        paused_until = self.state.platform_paused_until.get(platform, 0.0)
        if paused_until > time.time():
            return FetchResult.failure(
                "env.unavailable",
                f"{C.PLATFORM_LABEL.get(platform, platform)}处于熔断冷却中",
            )
        if mode == "rss" or platform == "rss":
            return await self._fetch_rss(account)
        if mode == "http":
            return await fetch_m2.fetch(
                account,
                session=getattr(self.p, "_http", None),
                cookie=self._cookie_for(platform),
                post_limit=C.FETCH_POST_LIMIT,
            )
        return await fetch_m1.fetch(account, browser=self.browser, post_limit=C.FETCH_POST_LIMIT)

    async def _fetch_rss(self, account: SnsAccount) -> FetchResult:
        url = account.feed_url or account.url or account.account_id
        session = getattr(self.p, "_http", None)
        if session is None:
            return FetchResult.failure("env.unavailable", "HTTP 会话未就绪")
        try:
            from ..shared.http import guarded_request, read_limited_text

            # 显式总超时：不传的话走 aiohttp 的 300s 默认值，下面那个
            # `except asyncio.TimeoutError` 永远等不到，串行轮次会被一个
            # 卡死的源拖住几分钟。
            timeout = aiohttp.ClientTimeout(total=float(C.HTTP_TIMEOUT_SECONDS))
            async with await guarded_request(
                session,
                "GET",
                url,
                prepare=lambda u, m, cross: {"timeout": timeout},
            ) as resp:
                status = int(resp.status)
                text = await read_limited_text(resp)
        except asyncio.TimeoutError:
            return FetchResult.failure("net.timeout", "RSS 请求超时")
        except Exception as exc:
            return FetchResult.failure("net.error", f"{exc.__class__.__name__}: {exc}")
        if status in (401, 403, 429):
            return FetchResult.failure("net.error", f"RSS 源返回 {status}")
        signal = rss_adapter.detect_signal(text)
        if signal:
            return FetchResult.failure(signal, "RSS 源返回的不是合法 feed")
        posts = parse_posts("rss", text, account_id=account.account_id)
        return FetchResult.success(posts, nickname=account.name)

    async def _enrich(self, account: SnsAccount, post: SnsPost) -> None:
        """推送前补全媒体（目前只有小红书需要：卡片只有一张带水印封面）。"""
        if post.platform != "xhs" or len(post.images) > 1:
            return
        detail: dict[str, Any] = {}
        try:
            if self.fetch_mode == "http":
                detail = await fetch_m2.fetch_note_detail(
                    getattr(self.p, "_http", None),
                    post.post_id,
                    self._cookie_for("xhs"),
                )
            else:
                detail = await fetch_m1.fetch_note_detail(self.browser, post.post_id)
        except Exception as exc:
            self.log.info(f"[sns] 补全笔记详情失败（不影响推送）：{exc}")
            return
        if not isinstance(detail, dict) or detail.get("signal"):
            return
        images = [u for u in (detail.get("images") or []) if u]
        if images:
            post.images = images[: self._int("max_images", C.DELIVER_MAX_IMAGES)]
        videos = [u for u in (detail.get("videos") or []) if u]
        if videos and not post.videos:
            post.videos = videos
        if not post.text and detail.get("text"):
            post.text = str(detail.get("text") or "")

    # ------------------------------------------------------------ 投递

    async def _deliver(
        self, account: SnsAccount, posts: list[SnsPost], *, warmup: bool
    ) -> dict[str, Any]:
        sessions = self.sessions()
        if not sessions:
            self.state.record(
                "error",
                platform=account.platform,
                name=account.label,
                text="没有推送目标，本轮不标记已读",
            )
            return {"sent": 0, "failed": len(posts), "delivered": []}
        deliver = getattr(self.p, "deliver_sns_payload", None)
        if not callable(deliver):
            self.state.record(
                "error",
                platform=account.platform,
                name=account.label,
                text="投递通道未绑定（deliver_sns_payload 缺失）",
            )
            return {"sent": 0, "failed": len(posts), "delivered": []}

        sent = failed = 0
        delivered: list[str] = []
        for post in posts:
            await self._enrich(account, post)
            payload = self._payload(account, post)
            # `sent` 以"帖子"为粒度计数：下面按会话循环，多群时不能按会话数累加，
            # 否则面板上的推送量与指令回显都会翻倍。
            post_ok = False
            for session in sessions:
                try:
                    state = await deliver(payload, session)
                except Exception as exc:
                    self.log.warning(f"[sns] 投递失败：{exc.__class__.__name__}: {exc}")
                    state = "failed"
                if state in ("sent", "unknown"):
                    post_ok = True
                    self.state.bump(account.platform, "push_ok")
                    self.state.record(
                        "push",
                        platform=account.platform,
                        name=account.label,
                        text=post.title or post.post_id,
                        detail=session,
                    )
                else:
                    self.state.bump(account.platform, "push_fail")
                    self.state.record(
                        "push_fail",
                        platform=account.platform,
                        name=account.label,
                        text=post.title or post.post_id,
                        detail=session,
                    )
            if post_ok:
                sent += 1
                delivered.append(str(post.post_id))
            else:
                failed += 1
            if warmup:
                # 热身期每条之间留出间隔，避免"刚上线就一串消息"
                await asyncio.sleep(1.5)
        return {"sent": sent, "failed": failed, "delivered": delivered}

    def _payload(self, account: SnsAccount, post: SnsPost) -> dict[str, Any]:
        max_images = max(1, self._int("max_images", C.DELIVER_MAX_IMAGES))
        text_max = max(40, self._int("text_max_length", 300))
        return {
            "platform": C.PLATFORM_LABEL.get(post.platform, post.platform),
            "name": account.name or post.author or account.account_id,
            "title": post.title,
            "text": post.body(text_max=text_max),
            "link": post.link,
            "ts": post.ts or time.time(),
            "images": list(post.images)[:max_images],
            "videos": list(post.videos),
            "kind": post.kind,
            "at_all_keys": [account.account_id, account.name, account.platform],
            "dedupe_key": post.dedupe_key,
        }

    # ------------------------------------------------------------ 面板数据

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        rows = []
        for account in sorted(self.state.accounts.values(), key=lambda a: (a.platform, a.label)):
            rows.append(
                {
                    "key": account.key,
                    "platform": account.platform,
                    "platform_label": C.PLATFORM_LABEL.get(account.platform, account.platform),
                    "account_id": account.account_id,
                    "name": account.label,
                    "url": account.url,
                    "enabled": account.enabled,
                    "baseline_done": account.baseline_done,
                    "health": round(account.health, 3),
                    "paused": account.paused,
                    "paused_reason": account.paused_reason,
                    "cooling": account.cooling(now),
                    "cooldown_until": account.cooldown_until,
                    "next_check_ts": account.next_check_ts,
                    "last_check_ts": account.last_check_ts,
                    "last_ok_ts": account.last_ok_ts,
                    "last_error": account.last_error,
                    "last_signal": account.last_signal,
                    "fail_streak": account.fail_streak,
                    "risk_streak": account.risk_streak,
                    "push_ok": account.push_ok,
                    "seen_count": len(account.seen_ids),
                }
            )
        platforms = []
        for platform in C.PLATFORMS:
            items = [a for a in self.state.accounts.values() if a.platform == platform]
            platforms.append(
                {
                    "platform": platform,
                    "label": C.PLATFORM_LABEL.get(platform, platform),
                    "accounts": len(items),
                    "base_interval": self.base_interval(platform),
                    "concurrency": self.concurrency(platform),
                    "paused_until": self.state.platform_paused_until.get(platform, 0.0),
                    "risk_hits": len(self.state.platform_risk.get(platform, [])),
                    "login": self.identity.summary(platform, self._cookie_for(platform)),
                }
            )
        return {
            "enabled": self.enabled,
            "fetch_mode": self.fetch_mode,
            "aggressive": self._bool("aggressive", False),
            "sessions": self.sessions(),
            "accounts": rows,
            "platforms": platforms,
            "browser": self.browser.status(),
            "browser_error": self._browser_error,
            "stats": dict(self.state.stats),
            "events": list(reversed(self.state.events))[:30],
            "last_round": self._last_round,
            "last_round_ts": self._last_round_ts,
            "guard": {
                "base_cooldown": self.guard_config().base_cooldown,
                "cooldown_cap": self.guard_config().cooldown_cap,
                "health_pause": self.guard_config().health_pause,
                "health_degrade": self.guard_config().health_degrade,
                "warmup_minutes": self._int("warmup_minutes", C.WARMUP_MINUTES_DEFAULT),
                "protect_mode_hours": self._int("protect_mode_hours", C.PROTECT_MODE_HOURS_DEFAULT),
                "night_slowdown": self._bool("night_slowdown", True),
                "trip_hits": C.PLATFORM_TRIP_HITS,
                "trip_window": C.PLATFORM_TRIP_WINDOW,
                "platform_pause": C.PLATFORM_PAUSE_SECONDS,
            },
            "latency": self.latency_estimate(),
            "checked_ts": now,
        }

    def latency_estimate(self) -> dict[str, Any]:
        """如实给用户一个"最坏要等多久"的估算，而不是含糊说"实时"。"""
        out: dict[str, Any] = {}
        for platform in C.PLATFORMS:
            base = self.base_interval(platform)
            items = [a for a in self.state.accounts.values() if a.platform == platform]
            if not items:
                out[platform] = {"accounts": 0}
                continue
            interval = scheduler.effective_interval(
                base,
                account_count=len(items),
                concurrency=self.concurrency(platform),
                jitter_ratio=C.JITTER_RATIO.get(platform, 0.2),
                platform=platform,
            )
            out[platform] = {
                "accounts": len(items),
                "interval": round(interval, 1),
                "p50": round(interval / 2, 1),
                "p95": round(interval, 1),
            }
        return out

    # ------------------------------------------------------------ 演练

    async def drill(self, platform: str = "") -> dict[str, Any]:
        """演练：取一次数但**不推送、不记已读、不动冷却**。

        用途很实在——刚登录完想知道"到底通不通"，又不想因为一次试验就被平台
        记上一笔。所以这里连 `last_check_ts` 都不写。
        """
        accounts = (
            self.state.by_platform(platform) if platform else list(self.state.accounts.values())
        )
        if not accounts:
            return {"ok": False, "error": "没有订阅可以演练"}
        account = accounts[0]
        started = time.time()
        result = await self._fetch(account)
        elapsed = round(time.time() - started, 2)
        return {
            "ok": result.ok,
            "platform": account.platform,
            "name": account.label,
            "outcome": result.outcome,
            "signal": result.signal,
            "label": result.label,
            "posts": len(result.posts),
            "nickname": result.nickname,
            "elapsed": elapsed,
            "preview": [p.title or p.post_id for p in result.posts[:3]],
        }

    def status_text(self) -> str:
        snap = self.snapshot()
        lines = [
            f"多平台订阅：{'开' if snap['enabled'] else '关'}（取数：{snap['fetch_mode']}）",
            f"订阅数：{len(snap['accounts'])}，推送目标：{len(snap['sessions'])}",
        ]
        for platform in snap["platforms"]:
            login = platform["login"]
            state = "已登录" if login.get("logged_in") else "未登录"
            pause = " · 熔断中" if platform["paused_until"] > time.time() else ""
            lines.append(
                f"{platform['label']}：{platform['accounts']} 个订阅 · {state}"
                f" · 基础间隔 {platform['base_interval']}s{pause}"
            )
        if snap["browser"].get("error"):
            lines.append(f"浏览器：{snap['browser']['error']}")
        if any(not row["login"].get("logged_in") for row in snap["platforms"]):
            # 打开登录窗口本身不写入"已登录"：真伪只认下一轮检查的结果
            lines.append("提示：登录以「下一轮检查是否通」为准，窗口打开不等于已登录。")
        return "\n".join(lines)


def now_within(start_ts: float, window: float) -> bool:
    """`start_ts` 起 `window` 秒之内（window<=0 视为不启用）。"""
    if not start_ts or window <= 0:
        return False
    return time.time() < float(start_ts) + float(window)


def dumps(payload: Any) -> str:
    """给指令/面板用的稳定 JSON（中文不转义，方便用户直接看）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
