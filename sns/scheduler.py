"""节奏调度：谁本轮该被检查、间隔怎么算、新账号怎么错峰。

所有决策都是确定性函数 + 一个随机源参数（`rng`），单元测试传固定种子的
`random.Random(0)` 就能复现任何一次排程，不需要等真实时间流逝。

三条必须遵守的排程规则（写在这里，是因为最容易实现错）：

1. **错峰**：新增账号不能都设成"现在 + 间隔"。n 个账号要均匀铺在一个间隔内，
   否则它们会在同一秒一起到点，形成一次突发——抖音风控对突发极其敏感。
2. **冷却中的账号不被提前叫醒**：无论是面板"立即检查"还是配置热改，都不能把
   `cooldown_until` 拉到未来之前；把正在被风控的账号提前拉起来只会加重风控。
3. **账号越多间隔越长**：`interval = max(base, base × N / 平台并发)`。这条是
   "总请求速率"的守恒：加了博主就必须拉长间隔，而不是让单位时间的请求变多。
"""

from __future__ import annotations

import random
import time

from . import constants as C
from .types import SnsAccount


def night_factor(
    now: float | None = None,
    *,
    enabled: bool = True,
    start_hour: int = C.NIGHT_START_HOUR,
    end_hour: int = C.NIGHT_END_HOUR,
) -> float:
    """凌晨放大系数：夜里没人看，把间隔拉长纯属省风控额度。"""
    if not enabled:
        return 1.0
    moment = time.time() if now is None else now
    hour = time.localtime(moment).tm_hour
    if start_hour <= hour < end_hour:
        return C.NIGHT_FACTOR
    # 支持跨零点区间（比如 23 -> 7）
    if start_hour > end_hour and (hour >= start_hour or hour < end_hour):
        return C.NIGHT_FACTOR
    return 1.0


def warmup_active(
    created_ts: float,
    now: float | None = None,
    *,
    minutes: float = C.WARMUP_MINUTES_DEFAULT,
) -> bool:
    """新账号预热期：刚加进来的一段时间里放慢并只检一个账号。

    刚订阅就满速跑是最容易被盯上的行为：新身份 + 立刻高频访问同一博主。
    """
    if minutes <= 0:
        return False
    moment = time.time() if now is None else now
    return (moment - float(created_ts or 0)) < float(minutes) * 60


def effective_interval(
    base: float,
    *,
    account_count: int = 1,
    concurrency: int = 1,
    health: float = 1.0,
    degrade_threshold: float = C.HEALTH_DEGRADE_DEFAULT,
    warmup: bool = False,
    night: float = 1.0,
    rng: random.Random | None = None,
    jitter_ratio: float | None = None,
    platform: str = "",
) -> float:
    """算出这次该等多久（秒）。结果不小于 `base`，且带随机抖动。"""
    floor = max(1.0, float(base))
    concord = max(1, int(concurrency))
    count = max(1, int(account_count))
    interval = max(floor, floor * count / concord)
    if warmup:
        interval *= 2.0
    if night and night > 1.0:
        interval *= float(night)
    if degrade_threshold > 0 and health < degrade_threshold:
        interval *= C.HEALTH_DEGRADE_FACTOR
    ratio = C.JITTER_RATIO.get(platform, 0.2) if jitter_ratio in (None, 0) else float(jitter_ratio)
    ratio = min(max(ratio, 0.0), 0.5)
    if ratio > 0 and interval > 0:
        source = rng or random
        # 只往上抖不往下抖：向下抖会突破"不小于 base"的硬下限
        interval *= 1.0 + source.uniform(0.0, ratio)
    return round(interval, 3)


def stagger_offsets(
    count: int,
    interval: float,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> list[float]:
    """把 `count` 个账号的首次检查时间均匀铺在一个间隔内。

    取 `i/count × interval` 再加一点小抖动，保证既不错峰失败也不整齐得像
    机器定时器（同一毫秒的 n 个请求本身就是爬虫特征）。
    """
    total = max(0, int(count))
    if total == 0:
        return []
    moment = time.time() if now is None else now
    span = max(1.0, float(interval))
    source = rng or random
    return [moment + (i + source.uniform(0.0, 0.4)) * span / total for i in range(total)]


def plan_round(
    accounts: list[SnsAccount],
    now: float | None = None,
    *,
    per_platform_limit: dict[str, int] | None = None,
    platform_paused: dict[str, float] | None = None,
    force: bool = False,
) -> list[SnsAccount]:
    """挑出本轮要检查的账号（按 `next_check_ts` 升序、受平台并发上限约束）。

    `platform_paused` 的值是**熔断截止时间戳**（不是布尔）：只要还没到点就跳过
    该平台，到点后自动恢复调度。曾经的写法按真值判断，一次熔断就把整个平台
    永久踢出轮询——因为落盘的时间戳永远非零。

    `force=True` 是"用户点了立即检查"：**只**忽略 `next_check_ts`，冷却中的
    账号依然不碰——把正在被风控的账号提前拉起来只会加重风控，手动操作也一样。

    暂停（`account.paused`）的账号仍然会在冷却结束后被放行一次：暂停是"先别动"
    而不是"永久停用"，否则用户重新登录后也没有任何路径把订阅救回来。
    """
    moment = time.time() if now is None else now
    limits = dict(C.PLATFORM_CONCURRENCY)
    if per_platform_limit:
        for key, value in per_platform_limit.items():
            if int(value or 0) > 0:
                limits[str(key)] = int(value)
    paused = {str(k): float(v or 0.0) for k, v in (platform_paused or {}).items()}
    picked: list[SnsAccount] = []
    used: dict[str, int] = {}
    for account in sorted(accounts, key=lambda a: float(a.next_check_ts or 0)):
        if force:
            due = bool(account.enabled) and not account.paused and not account.cooling(moment)
        elif account.enabled and account.paused:
            # 暂停账号的探活：只在冷却窗口过去之后放行一次，成功就自动解除暂停
            due = not account.cooling(moment)
        else:
            due = account.due(moment)
        if not due:
            continue
        platform = account.platform
        if paused.get(platform, 0.0) > moment:
            continue
        cap = max(1, int(limits.get(platform, 1)))
        if used.get(platform, 0) >= cap:
            continue
        used[platform] = used.get(platform, 0) + 1
        picked.append(account)
    return picked


def next_check_time(
    account: SnsAccount,
    interval: float,
    *,
    now: float | None = None,
) -> float:
    """算下一次检查时间。冷却未结束的账号以冷却结束时间为准。"""
    moment = time.time() if now is None else now
    base = moment + max(1.0, float(interval))
    return max(base, float(account.cooldown_until or 0))
