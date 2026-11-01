"""推送循环的部分会话失败重试与终态记账（P1-1 回归）。

这里的 `_send_rich_media` 只被换成"可控结果"，真实的 `_flush_pending` 状态机、
队列进出、累计记账、落盘全部走生产路径——此前 1099 个测试全绿而这条缺陷仍在，
正是因为推送循环本体一行都没被测到。
"""

from __future__ import annotations

import time

S1 = "qq:GroupMessage:111"
S2 = "qq:GroupMessage:222"
S3 = "qq:GroupMessage:333"


def _item(post_id: str = "p1") -> dict:
    return {
        "post_id": post_id,
        "uid": "1669879400",
        "text": "正文",
        "raw_text": "正文",
        "created_ts": time.time(),
        "pics": [],
        "videos": [],
        "video_files": [],
    }


def _plugin(make_weibo_plugin, sessions, **cfg):
    return make_weibo_plugin(
        push_sessions=list(sessions),
        push_delay_seconds=0,
        album_enabled=False,
        **cfg,
    )


def _patch_send(monkeypatch, plugin, decide, calls=None):
    """把单会话发送换成脚本化结果，返回 (session, item) -> status 的回调。"""

    async def fake_send(session, item, chain, timeout_seconds):
        if calls is not None:
            calls.append(session)
        return decide(session, item), chain

    async def fake_chain(item, *, with_video=True):
        return None

    monkeypatch.setattr(plugin, "_send_rich_media", fake_send)
    monkeypatch.setattr(plugin, "_build_chain", fake_chain)


async def test_partial_failure_stays_queued_and_only_retries_failed(make_weibo_plugin, monkeypatch):
    """3 会话 sent/failed/sent：条目留队、只记待补发会话、本轮不记成功。"""
    plugin = _plugin(make_weibo_plugin, [S1, S2, S3], max_pending_retries=3)
    calls: list[str] = []
    state = {"round": 1}

    def decide(session, item):
        if state["round"] == 1:
            return "failed" if session == S2 else "sent"
        return "sent"

    _patch_send(monkeypatch, plugin, decide, calls)
    item = _item()
    plugin.pending = [item]

    assert await plugin._flush_pending() is True

    assert plugin.pending == [item], "部分失败必须留在队列里，否则失败的群永远收不到"
    assert item["retries"] == 1
    assert item["retry_sessions"] == [S2]
    assert item["sent_acc"] == [S1, S3]
    # 本轮只是"部分成功、还在重试中"，不许记成功，也不许记失败
    assert plugin.stats.get("push_ok", 0) == 0
    assert plugin.stats.get("push_fail", 0) == 0
    # 发送窗口已收尾：残留的"发送中"戳会让重载按结果未知整条丢弃
    assert "last_send_ts" not in item

    state["round"] = 2
    calls.clear()
    await plugin._flush_pending()

    assert calls == [S2], "补发只该发给上一轮明确失败的会话，不能重发已送达的"
    assert plugin.pending == []
    assert plugin.stats["push_ok"] == 1
    assert plugin.stats.get("push_fail", 0) == 0
    detail = plugin.activity[-1]["detail"]
    assert "3 个会话" in detail
    assert "失败后补发送达" in detail


async def test_all_sessions_failed_records_single_failure(make_weibo_plugin, monkeypatch):
    """全部会话失败：按 max_pending_retries 重试后放弃，且只记一条失败。"""
    plugin = _plugin(make_weibo_plugin, [S1, S2], max_pending_retries=2)
    _patch_send(monkeypatch, plugin, lambda session, item: "failed", [])
    item = _item()
    plugin.pending = [item]

    for _ in range(6):
        await plugin._flush_pending()
        if not plugin.pending:
            break

    assert plugin.pending == [], "重试超限后必须终态出队，不能永远留在队列里"
    assert plugin.stats["push_fail"] == 1
    assert plugin.stats.get("push_ok", 0) == 0
    assert "放弃" in plugin.activity[-1]["detail"]


async def test_partial_then_give_up_counts_as_delivered_once(make_weibo_plugin, monkeypatch):
    """部分送达后补发超限：整条确实进过群，终态只能记一次成功。"""
    plugin = _plugin(make_weibo_plugin, [S1, S2], max_pending_retries=2)
    _patch_send(
        monkeypatch,
        plugin,
        lambda session, item: "sent" if session == S1 else "failed",
        [],
    )
    plugin.pending = [_item()]

    for _ in range(6):
        await plugin._flush_pending()
        if not plugin.pending:
            break

    assert plugin.pending == []
    assert plugin.stats["push_ok"] == 1
    assert plugin.stats.get("push_fail", 0) == 0
    detail = plugin.activity[-1]["detail"]
    assert "放弃" in detail and "1 个会话仍失败" not in detail
