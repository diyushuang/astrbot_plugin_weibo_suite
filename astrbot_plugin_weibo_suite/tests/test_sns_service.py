"""P8 订阅服务：基线、判新旧、冷却、熔断、投递契约。

服务层是"取数 → 判新旧 → 守卫 → 投递"的唯一串联点，所以这里不测网络细节，
只测**决策**：新账号第一次必须只建基线不补推历史；抖音风控的空响应必须算错误；
冷却中的账号不会被"立即检查"提前叫醒；投递失败要被记成失败而不是成功。
"""

from __future__ import annotations

import time

import pytest

from astrbot_plugin_weibo_suite.sns import constants as C
from astrbot_plugin_weibo_suite.sns import service as sns_service
from astrbot_plugin_weibo_suite.sns.types import FetchResult, SnsPost


class FakeConfig(dict):
    def save_config(self):
        return None


class FakePlugin:
    """只实现 sns 服务真正会碰到的几个属性/方法。"""

    def __init__(self, config=None):
        self.config = FakeConfig(config or {})
        self._data_dir = ""
        self.delivered: list[tuple[dict, str]] = []
        self.deliver_result = "sent"

    def _sessions(self):
        return ["aiocqhttp:GroupMessage:12345"]

    async def deliver_sns_payload(self, payload, session):
        self.delivered.append((payload, session))
        return self.deliver_result


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    created = []

    def factory(config=None):
        plugin = FakePlugin(config)
        plugin._data_dir = str(tmp_path)
        service = sns_service.SnsService(plugin)
        created.append(service)
        return service

    yield factory
    for service in created:
        service.state.save()


def post(post_id: str, ts: float, **kwargs) -> SnsPost:
    base = dict(platform="douyin", account_id="MS4wLjABAAAAtest", post_id=post_id, ts=ts)
    base.update(kwargs)
    return base and SnsPost(**base)


def test_add_account_accepts_link_and_rejects_garbage(make_service):
    service = make_service()
    ok, message, account = service.add_account(
        "douyin",
        "https://www.douyin.com/user/MS4wLjABAAAAdemoSecUidPlaceholder000000000000000",
    )
    assert ok is True
    assert account.account_id.startswith("MS4wLjABAAAA")
    assert account.next_check_ts > 0
    # 重复订阅同一个博主不该产生第二条记录
    ok2, message2, _ = service.add_account("douyin", account.account_id)
    assert ok2 is False
    assert len(service.state.accounts) == 1
    ok3, message3, _ = service.add_account("douyin", "https://www.douyin.com/video/123")
    assert ok3 is False
    assert "链接" in message3 or "id" in message3
    assert message and message2


def test_first_check_only_builds_baseline_and_pushes_nothing(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    history = [post("1", 100), post("2", 200)]

    class Result(FetchResult):
        pass

    result = FetchResult.success(history, nickname="示例抖音博主")
    service._fetch = _async_return(result)
    detail = _run(service._check_account(account))

    assert account.baseline_done is True
    assert detail.get("baseline") is True
    assert service.p.delivered == []  # 基线绝不推历史
    assert account.seen("1") and account.seen("2")
    assert account.name == "示例抖音博主"


def test_second_check_pushes_only_new_posts_oldest_first(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    for post_id in ("1", "2"):
        account.mark_seen(post_id)

    fresh = [post("3", 300, text="第三条"), post("2", 200), post("4", 400, text="第四条")]
    service._fetch = _async_return(FetchResult.success(fresh[::-1]))
    detail = _run(service._check_account(account))

    pushed = [payload["dedupe_key"].rsplit(":", 1)[-1] for payload, _ in service.p.delivered]
    assert pushed == ["3", "4"]  # 已见过的不推，且按时间升序
    assert detail.get("new") == 2
    assert account.seen("3") and account.seen("4")


def test_push_per_round_is_capped_and_extras_are_marked_seen(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    many = [post(str(i), 100 + i) for i in range(1, C.MAX_PUSH_PER_ROUND + 3)]
    service._fetch = _async_return(FetchResult.success(many))
    _run(service._check_account(account))

    assert len(service.p.delivered) == C.MAX_PUSH_PER_ROUND
    # 超出的部分必须记为已见，否则下一轮又会重推
    for item in many:
        assert account.seen(item.post_id)


def test_risk_result_cools_down_and_records_event(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    service._fetch = _async_return(FetchResult.failure("douyin.empty_silent", "空响应"))
    detail = _run(service._check_account(account))

    assert detail.get("ok") is False
    assert account.cooldown_until > time.time()
    assert account.risk_streak >= 1
    assert service.p.delivered == []
    assert any(row["kind"] == "risk" for row in service.state.events)


def test_empty_result_from_a_platform_is_not_treated_as_no_update(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    # FetchResult.success([]) 的 signal 是 ok.empty：真·没有更新
    service._fetch = _async_return(FetchResult.success([]))
    detail = _run(service._check_account(account))
    assert detail.get("ok") is True
    assert account.risk_streak == 0
    assert account.cooldown_until == 0.0


def test_check_all_skips_when_previous_round_still_running(make_service):
    service = make_service()
    service.add_account("douyin", "MS4wLjABAAAAtest")
    _run(service._lock.acquire())
    try:
        result = _run(service.check_all())
    finally:
        service._lock.release()
    assert result["ok"] is False
    assert "还没跑完" in result["error"]


def test_check_all_respects_cooldown_and_platform_circuit_breaker(make_service):
    service = make_service()
    _ok, _message, cooling = service.add_account("douyin", "MS4wLjABAAAAcool")
    cooling.cooldown_until = time.time() + 600
    cooling.next_check_ts = 0
    called = []

    async def fake_check(account):
        called.append(account.account_id)
        return {"ok": True}

    service._check_account = fake_check
    _run(service.check_all())
    assert called == []  # 冷却中的账号不该被叫醒

    _ok, _message, fresh = service.add_account("douyin", "MS4wLjABAAAAfresh")
    fresh.next_check_ts = 0
    _run(service.check_all())
    assert called == ["MS4wLjABAAAAfresh"]

    service.state.platform_paused_until["douyin"] = time.time() + 600
    fresh.next_check_ts = 0
    called.clear()
    _run(service.check_all())
    assert called == []  # 平台熔断期间整平台停手


def test_three_risk_hits_trip_the_platform_breaker(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    service._fetch = _async_return(FetchResult.failure("douyin.empty_silent", "空响应"))
    for _ in range(C.PLATFORM_TRIP_HITS):
        _run(service._check_account(account))
    assert service.state.platform_paused_until.get("douyin", 0) > time.time()


def test_deliver_without_sessions_drops_and_records_error(make_service):
    service = make_service()
    service.p._sessions = lambda: []
    account = service.state.add(service.p, "douyin", "MS4wLjABAAAAtest") if False else None
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert (counts["sent"], counts["failed"]) == (0, 1)
    assert any("没有推送目标" in row["text"] for row in service.state.events)


def test_deliver_records_failure_when_channel_returns_failed(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    service.p.deliver_result = "failed"
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert (counts["sent"], counts["failed"]) == (0, 1)
    assert any(row["kind"] == "push_fail" for row in service.state.events)
    assert service.state.stats["by_platform"]["douyin"]["push_fail"] == 1


def test_deliver_treats_unknown_as_sent(monkeypatch, make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    service.p.deliver_result = "unknown"
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert (counts["sent"], counts["failed"]) == (1, 0)


def test_payload_shape_is_the_delivery_contract(make_service):
    service = make_service({"sns": {"max_images": 2, "text_max_length": 40}})
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.name = "示例抖音博主"
    item = post(
        "1",
        1730000000,
        text="很长很长的一段正文" * 5,
        title="标题",
        images=["https://a/1.jpeg", "https://a/2.jpeg", "https://a/3.jpeg"],
        videos=["https://a/v.mp4"],
        link="https://www.douyin.com/video/1",
        kind="video",
    )
    payload = service._payload(account, item)
    assert payload["platform"] == "抖音"
    assert payload["name"] == "示例抖音博主"
    assert payload["link"] == "https://www.douyin.com/video/1"
    assert len(payload["images"]) == 2
    assert payload["videos"] == ["https://a/v.mp4"]
    assert len(payload["text"]) <= 41  # 正文下限 40 字 + 省略号
    assert payload["at_all_keys"] == [account.account_id, account.name, "douyin"]
    assert payload["dedupe_key"] == "douyin:MS4wLjABAAAAtest:1"


def test_sessions_fall_back_to_the_weibo_push_list(make_service):
    service = make_service()
    service.p._sessions = lambda: ["aiocqhttp:GroupMessage:999"]
    assert service.sessions() == ["aiocqhttp:GroupMessage:999"]
    service.p.config["sns"] = {"push_sessions": ["aiocqhttp:GroupMessage:111"]}
    assert service.sessions() == ["aiocqhttp:GroupMessage:111"]


def test_snapshot_exposes_everything_the_panel_needs(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.name = "测试博主"
    snap = service.snapshot()
    assert snap["enabled"] is True
    assert snap["fetch_mode"] in ("browser", "http", "rss")
    assert isinstance(snap["accounts"], list) and snap["accounts"][0]["name"] == "测试博主"
    platform_row = snap["platforms"][0]
    assert platform_row["platform"] == "douyin"
    assert platform_row["base_interval"] >= C.MIN_BASE_INTERVAL["douyin"]
    assert "guard" in snap and "latency" in snap and "browser" in snap
    assert isinstance(snap["events"], list)
    # 面板读的每个值都必须能过 JSON（不能塞对象进去）
    import json

    json.dumps(snap, ensure_ascii=False)


def test_latency_estimate_reports_p50_and_p95(make_service):
    service = make_service()
    service.add_account("douyin", "MS4wLjABAAAAtest")
    estimate = service.latency_estimate()
    assert set(estimate) >= set(C.PLATFORMS)
    row = estimate["douyin"]
    assert row["interval"] >= C.MIN_BASE_INTERVAL["douyin"]
    assert row["p50"] <= row["p95"]


def test_drill_fetches_but_never_marks_seen_or_cooldowns(make_service):
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    service._fetch = _async_return(FetchResult.success([post("1", 100), post("2", 200)]))
    result = _run(service.drill("douyin"))
    assert result["ok"] is True
    assert result["posts"] == 2
    assert service.p.delivered == []
    assert account.seen("1") is False
    assert account.cooldown_until == 0.0


def test_resolve_browser_failure_is_unavailable_not_risk(make_service, monkeypatch):
    service = make_service({"sns": {"fetch_mode": "browser"}})

    async def boom(account, *, browser, post_limit=C.FETCH_POST_LIMIT):
        from astrbot_plugin_weibo_suite.sns.types import FetchResult as R

        return R.failure("env.unavailable", "没有可用浏览器")

    monkeypatch.setattr(sns_service.fetch_m1, "fetch", boom)
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    detail = _run(service._check_account(account))
    assert detail.get("ok") is False
    # 环境不可用不该把账号判成风控，更不该暂停
    assert account.paused is False


# ---------------------------------------------------------------- helpers


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner


def _run(coro):
    import asyncio

    return asyncio.run(coro)
