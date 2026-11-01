"""SNS 投递侧 @全体记账与超时参数（task-6 附加 A/B/C）。

历史缺陷：订阅域的链前置了 AtAll，但发送成功后从不调 `_at_all_mark_sent`，
配额永远是 0 —— 开了 `at_all_daily_limit` 也拦不住，能把账号的 @全体 次数
刷穿。反过来，带 @ 的链失败后整条更新直接丢弃（正文也丢了）。

这里钉住三条不变量：

1. 带 @全体 成功 → 配额 +1（"sent" 与 "unknown" 都算真发出去过）；
2. 带 @全体 明确失败 → 去掉 AtAll 补发一次，成功仍报 "sent" 且不记配额；
3. `timeout_seconds` 关键字参数真的透传到发送层（订阅域有自己的超时预算）。
"""

from __future__ import annotations

import asyncio

from .host_compat import chain_segments

GROUP = "aiocqhttp:GroupMessage:12345"


def _payload(**overrides):
    payload = {
        "platform": "抖音",
        "name": "示例抖音博主",
        "title": "新作品",
        "text": "今天更新了一条",
        "link": "https://www.douyin.com/video/7300000000000000000",
        "ts": 1_700_000_000,
        "images": [],
        "videos": [],
        "kind": "video",
        "at_all_keys": ["示例抖音博主"],
        "dedupe_key": "douyin:test:1",
    }
    payload.update(overrides)
    return payload


def _has_at_all(chain):
    return any(getattr(part, "qq", "") == "all" for part in chain_segments(chain))


def _texts(chain):
    return [getattr(part, "text", "") for part in chain_segments(chain) if hasattr(part, "text")]


class _Recorder:
    """记录每次发送的链；`errors` 逐次生效（None = 成功）。"""

    def __init__(self, *, ok=True, errors=None):
        self.ok = ok
        self.errors = list(errors or [])
        self.chains = []
        self.sessions = []

    async def __call__(self, session, chain):
        self.sessions.append(session)
        self.chains.append(chain_segments(chain))
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return self.ok


def _plugin(make_weibo_plugin, recorder):
    plugin = make_weibo_plugin(sns={"at_all": True})
    plugin._http = None
    plugin.context.send_message = recorder
    plugin._at_all_sent = []
    return plugin


def test_at_all_marks_quota_on_sent(make_weibo_plugin):
    """带 @全体 真发出去以后配额必须 +1，否则 at_all_daily_limit 形同虚设。"""
    recorder = _Recorder()
    plugin = _plugin(make_weibo_plugin, recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "sent"

    assert _has_at_all(recorder.chains[0]) is True
    assert plugin._at_all_quota_used() == 1


def test_at_all_marks_quota_on_timeout(make_weibo_plugin):
    """超时=可能已出群：配额同样要记，不能让超时反复叠 @全体。"""
    recorder = _Recorder(errors=[asyncio.TimeoutError()])
    plugin = _plugin(make_weibo_plugin, recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "unknown"
    assert plugin._at_all_quota_used() == 1


def test_at_all_failure_resends_without_at_all_and_keeps_quota(make_weibo_plugin):
    """带 @ 的链明确失败 → 去掉 AtAll 补发一次，正文不能跟着丢，配额不记。"""
    recorder = _Recorder(errors=[RuntimeError("机器人不是管理员")])
    plugin = _plugin(make_weibo_plugin, recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "sent"

    assert len(recorder.chains) == 2
    assert _has_at_all(recorder.chains[0]) is True
    assert _has_at_all(recorder.chains[1]) is False
    assert "今天更新了一条" in "\n".join(_texts(recorder.chains[1]))
    assert plugin._at_all_quota_used() == 0


def test_at_all_failure_and_resend_failure_reports_failed(make_weibo_plugin):
    """补发也失败：如实报 failed，让订阅域自己记失败，不虚报。"""
    recorder = _Recorder(errors=[RuntimeError("炸了"), RuntimeError("又炸了")])
    plugin = _plugin(make_weibo_plugin, recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "failed"
    assert len(recorder.chains) == 2
    assert plugin._at_all_quota_used() == 0


def test_timeout_seconds_is_forwarded(make_weibo_plugin):
    """订阅域传进来的超时必须真的落到发送层（订阅域有自己的时间预算）。"""
    plugin = _plugin(make_weibo_plugin, _Recorder())
    seen = []

    async def fake_send(session, chain, *, timeout_seconds=None):
        seen.append(timeout_seconds)
        return True

    plugin._send_with_timeout = fake_send

    assert (
        asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP, timeout_seconds=12.5)) == "sent"
    )
    assert seen == [12.5]
