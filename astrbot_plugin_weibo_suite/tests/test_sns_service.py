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
from astrbot_plugin_weibo_suite.sns.types import FetchResult, SnsAccount, SnsPost


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


def test_deliver_counts_unknown_separately_but_never_retries(monkeypatch, make_service):
    """超时（unknown）既不能当成功，也不能重试——重试就是重复推送。

    P2-5/P2-6 之后的口径：unknown **计入 delivered**（因此会被 mark_seen，
    不会下一轮重复推），但记账走独立的 push_unknown，sent 不 +1。
    以前这里断言 unknown 算 sent，面板上的"成功推送"数字因此虚高。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    service.p.deliver_result = "unknown"
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert (counts["sent"], counts["unknown"], counts["failed"]) == (0, 1, 0)
    assert counts["delivered"] == ["1"]  # 记已读，下一轮不重复推
    assert service.state.retry_queue == []  # 但也不排补发
    assert service.state.stats["by_platform"]["douyin"]["push_unknown"] == 1


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


# ---------------------------------------------------------------- 回归：状态与重试


def test_state_is_loaded_at_construction_so_save_never_wipes(tmp_path):
    """P1-1 回归：`sns.enabled=false` 时服务不会 start，但面板/指令仍会构造它。

    修复前 load 只在 `start()` 里调，而 `add_account` 立刻 `save()`（整文件
    覆盖），于是"关掉轮询再加一个订阅"会把磁盘上所有订阅清空。
    """
    from astrbot_plugin_weibo_suite.sns.state import SnsState

    # 预置两条订阅
    seed = SnsState(tmp_path)
    seed.add(SnsAccount(platform="douyin", account_id="a", name="甲"))
    seed.add(SnsAccount(platform="douyin", account_id="b", name="乙"))
    assert seed.save() is True

    # 新实例（等价于 enabled=false 时的惰性构造），不调 start() 直接写
    fresh = SnsState(tmp_path)
    assert set(fresh.accounts) == {"douyin:a", "douyin:b"}
    fresh.add(SnsAccount(platform="xhs", account_id="c", name="丙"))
    assert fresh.save() is True

    reloaded = SnsState(tmp_path)
    assert set(reloaded.accounts) == {"douyin:a", "douyin:b", "xhs:c"}


def test_service_adds_subscription_without_start_and_keeps_existing(tmp_path):
    """P1-1 的端到端版本：走真实 `SnsService`（`sns.enabled=false` 时不 start）。"""
    from astrbot_plugin_weibo_suite.sns.state import SnsState

    seed = SnsState(tmp_path)
    seed.add(SnsAccount(platform="douyin", account_id="MS4wLjABAAAAold1"))
    seed.add(SnsAccount(platform="douyin", account_id="MS4wLjABAAAAold2"))
    assert seed.save() is True

    plugin = FakePlugin({"sns": {"enabled": False, "push_sessions": ["g:1"]}})
    plugin._data_dir = str(tmp_path)
    service = sns_service.SnsService(plugin)  # 关键：不调 start()

    ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    assert ok is True and account is not None

    reloaded = SnsState(tmp_path)
    assert set(reloaded.accounts) == {
        "douyin:MS4wLjABAAAAold1",
        "douyin:MS4wLjABAAAAold2",
        "douyin:MS4wLjABAAAAtest",
    }, "关掉轮询后加订阅，把已有订阅清空了"


def test_partial_session_failure_marks_post_seen_and_retries_only_failed_session(
    make_service, monkeypatch
):
    """P2-5/P2-6 回归："已读"与"每个会话收没收到"是两个粒度。

    已读 = 这条 post 不再进 fresh 全量推送；还没收到的会话只由 retry_queue 补。
    修复前"有会话失败就不记已读"，于是下一轮 fresh 会对**所有**会话重推一遍，
    只要有一个群长期发不出去，每个周期都刷屏。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    # 两个推送目标：一个能发、一个发不出去
    monkeypatch.setattr(service, "sessions", lambda: ["g:1", "g:2"])

    calls: list[str] = []

    async def deliver(payload, session, **_kwargs):
        calls.append(session)
        return "sent" if session == "g:1" else "failed"

    monkeypatch.setattr(service.p, "deliver_sns_payload", deliver, raising=False)
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))

    # 失败会话已全部入队 → 整条记已读，不再走 fresh 全量推送
    assert counts["delivered"] == ["1"]
    assert (counts["sent"], counts["failed"], counts["retry"]) == (0, 1, 1)
    # 只有失败的会话进了补发队列
    assert [row["session"] for row in service.state.retry_queue] == ["g:2"]
    # 成功的那群照样记账（不能因为同一条在别处失败就吞掉）
    assert service.state.stats["by_platform"]["douyin"]["push_ok"] == 1

    # 下一轮只对该群补发，成功群不会收到重复消息
    calls.clear()
    _run(service._drain_retries(account))
    assert calls == ["g:2"]


def test_save_refuses_when_state_never_loaded(tmp_path, monkeypatch):
    """读盘失败（且备份也失败）时，save 必须拒绝，绝不能用空状态盖掉用户数据。"""
    from astrbot_plugin_weibo_suite.sns.state import SnsState

    seed = SnsState(tmp_path)
    seed.add(SnsAccount(platform="douyin", account_id="a"))
    assert seed.save() is True

    broken = SnsState(tmp_path)
    broken._loaded = False  # 模拟"没读到、也不能写"
    broken.add(SnsAccount(platform="douyin", account_id="z"))
    assert broken.save() is False

    again = SnsState(tmp_path)
    assert set(again.accounts) == {"douyin:a"}, "用户在磁盘上的订阅被静默覆盖了"


def test_add_feed_rejects_private_and_loopback_addresses(make_service, monkeypatch):
    """P1-2 回归：RSS 地址是群成员输入，必须过 SSRF 校验。

    `add_feed` 以前只判了 scheme，`http://127.0.0.1:8080/x` 与云元数据地址
    都能被订成"RSS 源"，然后由后台轮询替攻击者把响应读回群里。
    """
    from astrbot_plugin_weibo_suite.shared import http as shared_http

    service = make_service()
    for url in (
        "http://127.0.0.1:8080/feed.xml",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal.xml",
        "file:///etc/passwd",
    ):
        ok, _message, account = _run(service.add_feed(url))
        assert ok is False, f"{url} 不该被接受"
        assert account is None
    assert service.state.accounts == {}

    # 公网地址放行（DNS 用桩，避免依赖外网）
    monkeypatch.setattr(shared_http, "resolve_public_addrs", lambda url, allowed=None: (url, []))
    ok, _message, account = _run(service.add_feed("https://example.com/feed.xml", "我的源"))
    assert ok is True
    assert account.platform == "rss"
    assert account.name == "我的源"


def test_delivery_failure_is_queued_and_retried_next_round(make_service):
    """P2-5/P2-6 回归：失败会话进持久化补发队列，下一轮**只**补那个会话。

    已读与"送没送到"解耦：这条照样记已读（不再进 fresh 全量推送，否则每轮
    把内容重推给所有正常会话），投递由 retry_queue 逐会话补齐。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    service.p.deliver_result = "failed"
    counts = _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert (counts["sent"], counts["failed"]) == (0, 1)
    assert counts["delivered"] == ["1"], "失败会话已入队，这条就该记已读"
    assert len(service.state.retry_queue) == 1
    assert service.state.retry_queue[0]["post_id"] == "1"

    # 第二轮流恢复正常：补发成功、出队、记账 push_ok
    service.p.deliver_result = "sent"
    drained = _run(service._drain_retries(account))
    assert drained["sent"] == 1
    assert service.state.retry_queue == []


def test_retry_gives_up_after_max_attempts(make_service):
    """补发不能无限重试：达到上限后放弃并留痕，否则一条坏会话会卡住整个队列。"""
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    service.p.deliver_result = "failed"
    _run(service._deliver(account, [post("1", 1)], warmup=False))
    assert len(service.state.retry_queue) == 1

    for _ in range(C.MAX_PUSH_RETRIES):
        _run(service._drain_retries(account))
    assert service.state.retry_queue == []
    assert any(row["kind"] == "push_fail" for row in service.state.events)


def test_partial_session_failure_never_rebroadcasts_to_successful_session(
    make_service, monkeypatch
):
    """P1 回归（刷屏级）+ 走**真实** `_check_account`，不是只调 _deliver。

    g:1 能发、g:2 每轮必失败。修复前第 1 轮不记已读，第 2 轮 fresh 对**两个**
    会话又推一遍，同时 drain 再补 g:2，调用序列是 [g:2, g:1, g:2] —— 正常的
    群每轮都被重推同一条内容。修复后第 2 轮 g:1 零调用、g:2 恰好一次。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    monkeypatch.setattr(service, "sessions", lambda: ["g:1", "g:2"])
    calls: list[tuple[str, str]] = []

    async def deliver(payload, session, **_kwargs):
        calls.append((payload["dedupe_key"].rsplit(":", 1)[-1], session))
        return "sent" if session == "g:1" else "failed"

    monkeypatch.setattr(service.p, "deliver_sns_payload", deliver, raising=False)
    service._fetch = _async_return(FetchResult.success([post("1", 100)]))

    _run(service._check_account(account))
    assert calls == [("1", "g:1"), ("1", "g:2")], "第 1 轮：两个会话各一次"

    calls.clear()
    _run(service._check_account(account))
    assert calls == [("1", "g:2")], f"第 2 轮只该补发失败的 g:2，实际 {calls}"
    assert account.seen("1"), "失败会话已入队，这条就该记已读（不再进 fresh）"


def test_all_session_failure_marks_seen_and_never_double_sends(make_service, monkeypatch):
    """P1 回归：全部会话失败也要记已读，否则每轮 fresh 全量重推 + 补发 = 双份。

    每轮每个会话最多被调用一次；重试次数耗尽后不再有任何调用。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True
    monkeypatch.setattr(service, "sessions", lambda: ["g:1", "g:2"])
    calls: list[str] = []

    async def deliver(_payload, session, **_kwargs):
        calls.append(session)
        return "failed"

    monkeypatch.setattr(service.p, "deliver_sns_payload", deliver, raising=False)
    service._fetch = _async_return(FetchResult.success([post("1", 100)]))

    _run(service._check_account(account))
    assert calls == ["g:1", "g:2"], "第 1 轮：两个会话各一次"
    assert account.seen("1"), "全失败也要记已读，否则下一轮重推全部会话"

    for _ in range(C.MAX_PUSH_RETRIES + 1):
        calls.clear()
        _run(service._check_account(account))
        # 每轮只能是"补发每会话一次"或"没有可补的"，绝不能是 fresh 的 2 次 + 补发 2 次
        assert sorted(calls) in ([], ["g:1", "g:2"]), f"同一轮重复推送了：{calls}"
    assert service.state.retry_queue == [], "重试次数耗尽后队列必须清空"
    calls.clear()
    _run(service._check_account(account))
    assert calls == [], "队列空了就不该再发（更不该回头走 fresh 重推）"


def test_retry_queue_overflow_is_recorded_not_silently_dropped(make_service, monkeypatch):
    """队列满了会丢最旧的补发项 —— 因为"入队即记已读"，这就是永久丢消息。

    不能静默：必须留一条 error 事件，否则用户永远不知道为什么少收一条。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    monkeypatch.setattr(service, "sessions", lambda: ["g:1"])
    monkeypatch.setattr(C, "RETRY_QUEUE_LIMIT", 2)
    for index in range(3):
        service._enqueue_retry(account, post(str(index), 100 + index), {"k": index}, "g:1")

    assert len(service.state.retry_queue) == 2
    overflow = [row for row in service.state.events if "补发队列已满" in str(row.get("text"))]
    assert len(overflow) == 1, "队列溢出必须留痕"


def test_check_all_persists_mark_seen_even_when_a_check_raises(make_service, monkeypatch):
    """P3-13 回归：状态落盘以前在 try 主体末尾，异常一抛就全丢。

    表现为面板一直显示同一条"新内容"（mark_seen 只在内存里）。
    """
    service = make_service()
    _ok, _message, account = service.add_account("douyin", "MS4wLjABAAAAtest")
    account.baseline_done = True

    async def boom(_account):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_check_account", boom)
    result = _run(service.check_all(force=True))
    # 轮次本身跑完了（ok 表示"执行了"，不是"每条都成功"），但失败被单独计数
    assert result["ok"] is True
    assert result["failed"] == 1
    assert [row["key"] for row in result["detail"]] == ["douyin:MS4wLjABAAAAtest"]
    assert "检查异常" in result["detail"][0]["label"]
    # 异常之后仍然写出了状态文件
    assert service.state.path.exists()
