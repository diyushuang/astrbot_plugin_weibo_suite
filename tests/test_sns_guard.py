"""P8 守卫与排程：退避、健康度、熔断、间隔与错峰。

这两个模块是"保号"的地基，任何一处算错都会让账号在风控里躺得更久，
所以用固定种子的随机源把每一次计算都做成可复现的断言。
"""

from __future__ import annotations

import random
import time

from astrbot_plugin_weibo_suite.sns import constants as C
from astrbot_plugin_weibo_suite.sns import scheduler
from astrbot_plugin_weibo_suite.sns.guard import (
    GuardConfig,
    apply_result,
    cooldown_seconds,
    health_score,
    network_backoff,
    platform_tripped,
)
from astrbot_plugin_weibo_suite.sns.types import FetchResult, SnsAccount, SnsPost


def make_account(**overrides) -> SnsAccount:
    base = dict(platform="douyin", account_id="MS4wLjABAAAAtest", name="测试博主")
    base.update(overrides)
    return SnsAccount(**base)


# ---------------------------------------------------------------- 冷却


def test_cooldown_grows_exponentially_and_is_capped():
    first = cooldown_seconds(600, 1, 1.0, 3600)
    second = cooldown_seconds(600, 2, 1.0, 3600)
    fifth = cooldown_seconds(600, 5, 1.0, 3600)
    assert first == 600
    assert second == 1200
    assert fifth == 3600  # 600 * 2^4 = 9600，被上限截断
    assert cooldown_seconds(600, 1, 1.8, 3600) == 1080
    # 权重缺省/<=0 一律按 1.0 处理：网络错误走 network_backoff，不经过这里
    assert cooldown_seconds(600, 1, 0.0, 3600) == 600
    assert cooldown_seconds(600, 4, 0.0, 3600) == 3600


def test_network_backoff_never_exceeds_five_minutes():
    assert network_backoff(1) == 30
    assert network_backoff(2) == 60
    assert network_backoff(3) == 120
    assert network_backoff(9) == 300


# ---------------------------------------------------------------- 健康度


def _row(ts: float, outcome: str) -> dict:
    return {"ts": ts, "outcome": outcome, "signal": "", "detail": ""}


def test_health_score_is_one_without_history():
    assert health_score([]) == 1.0


def test_health_score_drops_with_recent_risk_and_failures():
    now = time.time()
    clean = [_row(now - i, C.OUTCOME_OK) for i in range(5)]
    risky = [*clean, _row(now - 30, C.OUTCOME_RISK)]
    assert health_score(clean, now) == 1.0
    assert health_score(risky, now) < 1.0
    # 连续失败按 0.5 的幂次再打一次折
    assert health_score(clean, now, fail_streak=3) == 0.125


def test_platform_tripped_需要窗口内足够多次风控():
    now = time.time()
    two = [now - 60, now - 30]
    three = [*two, now - 5]
    assert platform_tripped(two, now) is False
    assert platform_tripped(three, now) is True
    # 窗口外的旧记录不作数
    stale = [now - 4000, now - 3000, now - 2000]
    assert platform_tripped(stale, now) is False


# ---------------------------------------------------------------- apply_result


def test_ok_result_clears_cooldown_and_streaks():
    now = time.time()
    account = make_account(
        fail_streak=3,
        risk_streak=2,
        cooldown_until=now + 600,
        paused=True,
        paused_reason="健康度过低",
        last_error="老错误",
    )
    result = FetchResult.success([SnsPost(platform="douyin", account_id="a", post_id="1")])
    applied = apply_result(account, result, GuardConfig(), now=now)
    assert applied["paused"] is False
    assert account.paused is False
    assert account.paused_reason == ""
    assert account.fail_streak == 0
    assert account.risk_streak == 0
    assert account.cooldown_until == 0.0
    assert account.last_error == ""


def test_risk_result_cools_and_may_auto_pause():
    now = time.time()
    account = make_account(history=[_row(now - 10, C.OUTCOME_RISK) for _ in range(4)])
    result = FetchResult.failure("douyin.empty_silent", "空响应")
    applied = apply_result(account, result, GuardConfig(health_pause=0.2), now=now)
    assert applied["outcome"] == C.OUTCOME_RISK
    assert account.cooldown_until > now
    # 健康度已经很低，应自动暂停而不是继续尝试
    assert account.paused is True
    assert "健康度" in account.paused_reason


def test_auth_result_pauses_and_never_retries_automatically():
    now = time.time()
    account = make_account()
    result = FetchResult.failure("ig.checkpoint", "需要人工在浏览器里过验证")
    applied = apply_result(account, result, GuardConfig(), now=now)
    assert applied["paused"] is True
    assert account.paused is True
    assert "验证" in account.paused_reason
    assert account.cooldown_until == now + 3600  # 冷却直接给到上限


def test_network_error_does_not_count_as_risk():
    now = time.time()
    account = make_account(risk_streak=2)
    result = FetchResult.failure("net.timeout", "超时")
    applied = apply_result(account, result, GuardConfig(), now=now)
    assert applied["outcome"] == C.OUTCOME_NETWORK
    assert account.risk_streak == 2  # 未清零，但也没有增加
    assert account.paused is False
    assert account.cooldown_until == now + 30


def test_unavailable_platform_does_not_pause_account():
    now = time.time()
    account = make_account()
    result = FetchResult.failure("env.unavailable", "熔断中")
    apply_result(account, result, GuardConfig(), now=now)
    assert account.paused is False
    assert account.cooldown_until - now >= 60


# ---------------------------------------------------------------- 间隔


def test_effective_interval_scales_with_account_count_and_concurrency():
    # jitter_ratio <= 0 表示"关掉抖动"（0/None 是"用平台内置比例"）
    one = scheduler.effective_interval(300, account_count=1, concurrency=1, jitter_ratio=-1)
    four = scheduler.effective_interval(300, account_count=4, concurrency=2, jitter_ratio=-1)
    assert one == 300
    assert four == 600  # 300 * 4 / 2
    # 账号少时不得低于基数
    assert scheduler.effective_interval(300, account_count=1, concurrency=4, jitter_ratio=-1) == 300


def test_effective_interval_warmup_night_and_low_health_multiply():
    assert scheduler.effective_interval(300, jitter_ratio=-1) == 300
    warm = scheduler.effective_interval(300, jitter_ratio=-1, warmup=True)
    night = scheduler.effective_interval(300, jitter_ratio=-1, night=C.NIGHT_FACTOR)
    sick = scheduler.effective_interval(300, jitter_ratio=-1, health=0.1, degrade_threshold=0.4)
    assert warm == 600
    assert night == 600
    assert sick == 450


def test_zero_jitter_ratio_means_use_platform_default():
    # 0 与 None 都表示"用平台内置比例"，不是"关掉抖动"
    for value in (0, None):
        got = scheduler.effective_interval(
            300, jitter_ratio=value, rng=random.Random(0), platform="douyin"
        )
        assert 300 < got <= 360


def test_jitter_only_goes_upward():
    rng = random.Random(1234)
    values = [
        scheduler.effective_interval(300, jitter_ratio=0.2, rng=rng, platform="douyin")
        for _ in range(20)
    ]
    assert all(300 <= v <= 360 for v in values)
    assert len(set(values)) > 1  # 确实在抖，不是常量


def test_night_factor_handles_wrap_around_window():
    noon = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
    two_am = time.mktime((2026, 1, 1, 2, 0, 0, 0, 0, -1))
    assert scheduler.night_factor(noon) == 1.0
    assert scheduler.night_factor(two_am) == C.NIGHT_FACTOR
    assert scheduler.night_factor(two_am, enabled=False) == 1.0
    # 跨零点区间 23 -> 7：凌晨 2 点仍在区间内
    assert scheduler.night_factor(two_am, start_hour=23, end_hour=7) == C.NIGHT_FACTOR


def test_warmup_active_only_for_new_accounts():
    now = time.time()
    assert scheduler.warmup_active(now - 60, now, minutes=30) is True
    assert scheduler.warmup_active(now - 60 * 60, now, minutes=30) is False
    assert scheduler.warmup_active(now, now, minutes=0) is False


# ---------------------------------------------------------------- 排程


def test_stagger_spreads_accounts_within_one_interval():
    rng = random.Random(7)
    now = 1000.0
    offsets = scheduler.stagger_offsets(4, 400, now=now, rng=rng)
    assert len(offsets) == 4
    assert offsets == sorted(offsets)
    assert all(now <= ts <= now + 400 + 40 for ts in offsets)
    assert scheduler.stagger_offsets(0, 400) == []


def test_plan_round_respects_due_order_and_platform_cap():
    now = time.time()
    a = make_account(account_id="a", next_check_ts=now - 30)
    b = make_account(account_id="b", next_check_ts=now - 20)
    c = make_account(account_id="c", next_check_ts=now - 10)
    later = make_account(account_id="d", next_check_ts=now + 600)
    picked = scheduler.plan_round([a, b, c, later], now, per_platform_limit={"douyin": 2})
    assert [x.account_id for x in picked] == ["a", "b"]


def test_plan_round_skips_paused_platform_and_cooling_accounts():
    now = time.time()
    cooling = make_account(account_id="cool", cooldown_until=now + 600, next_check_ts=0)
    healthy = make_account(account_id="ok", next_check_ts=now - 5)
    assert [x.account_id for x in scheduler.plan_round([cooling, healthy], now)] == ["ok"]
    # platform_paused 的值是熔断截止时间戳，不是布尔：按真值判断会让一次熔断
    # 把整个平台永久踢出轮询（落盘的时间戳永远非零）。
    paused = scheduler.plan_round([healthy], now, platform_paused={"douyin": now + 600})
    assert paused == []
    # 已经过期的熔断不能继续挡路，否则平台再也醒不过来。
    expired = scheduler.plan_round([healthy], now, platform_paused={"douyin": now - 1})
    assert [x.account_id for x in expired] == ["ok"]


def test_next_check_time_never_wakes_a_cooling_account_early():
    now = time.time()
    account = make_account(cooldown_until=now + 900)
    assert scheduler.next_check_time(account, 300, now=now) == now + 900
    plain = make_account()
    assert scheduler.next_check_time(plain, 300, now=now) == now + 300
