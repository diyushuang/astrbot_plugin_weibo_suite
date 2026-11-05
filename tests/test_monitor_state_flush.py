"""check_all 的落盘时机：推送异常不能把本轮已见/待推送状态丢在内存里（审查 L3）。"""

from __future__ import annotations

import pytest


def _prepare_round(plugin, monkeypatch) -> None:
    plugin.accounts = {}
    plugin._checking = False
    plugin._blocked_until = 0.0
    # 跳过游客身份续期：本用例只关心落盘时机，不联网
    plugin._custom_cookies = {"SUB": "fake"}
    monkeypatch.setattr(plugin, "_sync_accounts_from_config", lambda: None)


async def test_flush_exception_still_saves_state(make_weibo_plugin, monkeypatch):
    """_flush_pending 抛异常时，本轮 seen/pending 的变更仍必须落盘。"""
    plugin = make_weibo_plugin()
    _prepare_round(plugin, monkeypatch)
    saved: list[int] = []

    async def boom():
        raise RuntimeError("push queue broken")

    async def fake_save():
        saved.append(1)

    monkeypatch.setattr(plugin, "_flush_pending", boom)
    monkeypatch.setattr(plugin, "_save_state_async", fake_save)

    with pytest.raises(RuntimeError):
        await plugin.check_all()

    assert saved == [1], "推送异常路径也必须落盘一次"
    assert plugin.last_check_ts > 0
    assert plugin._checking is False


async def test_unchanged_round_does_not_write_state(make_weibo_plugin, monkeypatch):
    """没有新微博也没有推送变化时不得额外写 state.json。"""
    plugin = make_weibo_plugin()
    _prepare_round(plugin, monkeypatch)
    written: list[int] = []

    async def no_change():
        return False

    async def fake_save():
        written.append(1)

    monkeypatch.setattr(plugin, "_flush_pending", no_change)
    monkeypatch.setattr(plugin, "_save_state_async", fake_save)

    assert await plugin.check_all() == 0
    assert written == []
