"""风控守卫：把一次抓取结果折算成冷却、健康度与平台熔断。

这里刻意做成"纯函数 + 少量可变状态"：

- `cooldown_seconds()` / `health_score()` / `platform_tripped()` 全是纯函数，测试
  直接喂时间戳与历史，不需要网络也不需要时钟。
- `apply_result()` 是唯一会改 `SnsAccount` 的地方，改完把"这一轮发生了什么"
  作为 dict 返回，服务层据此写日志/面板，不必再从账号对象反推。

口径与上游文档对齐（`Douyin_TikTok_Download_API` 的 identities 设计）：

- 一次 `risk` 命中：`cooldown = base × 2^(risk_streak-1) × weight`，上限 `cap`。
- 一次 `network` 失败：只算失败，**不进风控连击**、也不按指数冷却用户账号——
  网络抖动不该让机器人把自己的账号"罚站"一小时。
- 一次 `auth` 失败：直接暂停账号并提示人工处理；**绝不自动重登**。自动重登是
  被风控的最快路径：平台会看到同一身份在异地/异常环境里反复认证。
- 健康度 = 15 分钟成功率 × (1 - 60 分钟风控率) × 0.5^连续失败数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import constants as C
from .types import FetchResult, SnsAccount


@dataclass
class GuardConfig:
    """守卫的可调参数（来自插件配置，构造一次即可）。"""

    base_cooldown: float = C.COOLDOWN_BASE_DEFAULT
    cooldown_cap: float = C.COOLDOWN_CAP_DEFAULT
    health_degrade: float = C.HEALTH_DEGRADE_DEFAULT
    health_pause: float = C.HEALTH_PAUSE_DEFAULT

    # 暂停阈值不能高于降级阈值，否则"降级"永远轮不到；构造时夹一次
    def __post_init__(self) -> None:
        self.base_cooldown = max(1.0, float(self.base_cooldown))
        self.cooldown_cap = max(self.base_cooldown, float(self.cooldown_cap))
        self.health_pause = min(max(0.0, float(self.health_pause)), 1.0)
        self.health_degrade = min(max(self.health_pause, float(self.health_degrade)), 1.0)


def cooldown_seconds(
    base: float,
    streak: int,
    weight: float,
    cap: float,
) -> float:
    """按连击数与权重算冷却时长，结果夹在 `[base, cap]` 之间。

    `weight >= 99` 视为"不可自动恢复"（Instagram 的 checkpoint/challenge）：
    返回 `cap` 只是为了让调用方有个数值，真正的处置是暂停账号。
    """
    if streak <= 0:
        streak = 1
    growth = 2 ** min(streak - 1, 10)  # 10 次以上不再翻倍，防浮点爆炸
    seconds = float(base) * growth * max(0.0, float(weight or 1.0))
    return float(min(max(seconds, float(base)), float(cap)))


def _window_counts(
    history: list[dict],
    now: float,
    window: float,
    *,
    outcomes: frozenset[str],
) -> tuple[int, int]:
    """(窗口内总次数, 窗口内命中指定 outcome 的次数)。"""
    total = 0
    hit = 0
    for row in history:
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > window or ts > now:
            continue
        total += 1
        if str(row.get("outcome") or "") in outcomes:
            hit += 1
    return total, hit


def health_score(
    history: list[dict],
    now: float | None = None,
    *,
    fail_streak: int = 0,
) -> float:
    """账号健康度（0~1）。没有历史时给 1.0（新账号不该一上来就被降级）。"""
    moment = time.time() if now is None else now
    total_ok, ok_hits = _window_counts(
        history, moment, C.HISTORY_OK_WINDOW, outcomes=frozenset({C.OUTCOME_OK})
    )
    total_risk, risk_hits = _window_counts(
        history, moment, C.HISTORY_RISK_WINDOW, outcomes=frozenset({C.OUTCOME_RISK})
    )
    success_rate = (ok_hits / total_ok) if total_ok else 1.0
    risk_rate = (risk_hits / total_risk) if total_risk else 0.0
    penalty = 0.5 ** min(max(int(fail_streak), 0), 8)
    score = success_rate * (1.0 - risk_rate) * penalty
    return max(0.0, min(1.0, round(score, 4)))


def platform_tripped(
    timestamps: list[float],
    now: float | None = None,
    *,
    hits: int = C.PLATFORM_TRIP_HITS,
    window: float = C.PLATFORM_TRIP_WINDOW,
) -> bool:
    """平台熔断判定：`window` 秒内累计 `hits` 次风控就该停下整个平台。"""
    moment = time.time() if now is None else now
    recent = [ts for ts in timestamps if 0 <= moment - float(ts) <= window]
    return len(recent) >= max(1, int(hits))


def network_backoff(fail_streak: int) -> float:
    """网络失败的重试间隔：30 秒起、每次翻倍、最多 5 分钟。

    刻意远离风控冷却的量级：网络抖动不该让账号"罚站"一小时，也不该立刻重试
    打成一个小风暴。
    """
    streak = max(1, int(fail_streak))
    return float(min(30.0 * (2 ** min(streak - 1, 4)), 300.0))


def apply_result(
    account: SnsAccount,
    result: FetchResult,
    cfg: GuardConfig,
    *,
    now: float | None = None,
) -> dict:
    """把一次抓取结果落到账号上，返回"这一轮做了什么"。

    返回 dict 里的键全带默认值，调用方按需取用：`cooldown`（秒）、`paused`、
    `health`、`signal`、`outcome`、`label`。
    """
    moment = time.time() if now is None else now
    outcome = result.outcome
    account.last_check_ts = moment
    account.last_signal = result.signal
    account.history.append({"ts": moment, "outcome": outcome})
    if len(account.history) > C.HISTORY_LIMIT:
        del account.history[: len(account.history) - C.HISTORY_LIMIT]

    cooldown = 0.0
    if outcome in (C.OUTCOME_OK, C.OUTCOME_EMPTY):
        account.ok_streak += 1
        account.fail_streak = 0
        account.risk_streak = 0
        account.last_ok_ts = moment
        account.last_error = ""
        account.cooldown_until = 0.0
        # 认证失效过的账号重新抓到数据：解除暂停（说明用户已经重新登录）
        if account.paused and account.paused_reason:
            account.paused = False
            account.paused_reason = ""
    elif outcome == C.OUTCOME_RISK:
        account.risk_streak += 1
        account.fail_streak += 1
        account.ok_streak = 0
        account.last_risk_ts = moment
        account.last_error = result.label
        cooldown = cooldown_seconds(
            cfg.base_cooldown,
            account.risk_streak,
            C.signal_weight(result.signal),
            cfg.cooldown_cap,
        )
        account.cooldown_until = moment + cooldown
    elif outcome == C.OUTCOME_AUTH:
        # 登录态失效：暂停，等人处理。不冷却重试——重试只会让平台更确信是异常。
        account.fail_streak += 1
        account.ok_streak = 0
        account.paused = True
        account.paused_reason = result.label or "登录态失效，需要重新登录"
        account.last_error = account.paused_reason
        cooldown = cfg.cooldown_cap
        account.cooldown_until = moment + cooldown
    elif outcome == C.OUTCOME_UNAVAILABLE:
        # 环境问题（没装 playwright / 浏览器起不来 / 缺 Cookie）：不惩罚账号，
        # 只是晚点再试；暂停反而会让用户在装好依赖后还得手动点一次恢复。
        account.last_error = result.label
        cooldown = max(60.0, min(cfg.base_cooldown, 600.0))
        account.cooldown_until = moment + cooldown
    else:  # OUTCOME_NETWORK 及其他
        account.fail_streak += 1
        account.ok_streak = 0
        account.last_error = result.label
        cooldown = network_backoff(account.fail_streak)
        account.cooldown_until = moment + cooldown

    account.health = health_score(account.history, moment, fail_streak=account.fail_streak)
    if outcome == C.OUTCOME_RISK and account.health < cfg.health_pause:
        account.paused = True
        account.paused_reason = (
            f"健康度 {account.health:.2f} 低于 {cfg.health_pause:.2f}，已自动暂停"
        )
    return {
        "outcome": outcome,
        "signal": result.signal,
        "label": result.label,
        "cooldown": cooldown,
        "paused": account.paused,
        "health": account.health,
        "risk_streak": account.risk_streak,
        "fail_streak": account.fail_streak,
    }
