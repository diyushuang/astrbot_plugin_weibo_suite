"""按天推送统计桶必须用东八区口径（P3-12）。

旧实现在 UTC 机器上 00:00–08:00（北京时间）的推送记到前一天，且 _prune_stats
用同一套本地时区裁剪，两个口径一旦漂移就会把"当天"桶裁掉。这里不依赖本机
时区，直接断言调用方**显式**传了 tz，并验证固定时刻落到的桶。
"""

from __future__ import annotations

import datetime as dt

from astrbot_plugin_weibo_suite.weibo import plugin as plugin_module


class _FixedDatetime(dt.datetime):
    # 2025-12-31 20:30 UTC == 2026-01-01 04:30 北京时间：两天的交界
    FIXED = dt.datetime(2025, 12, 31, 20, 30, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        assert tz is not None, "统计桶必须显式传时区，否则跨时区部署会错天"
        return cls.FIXED.astimezone(tz)


def _patch(monkeypatch):
    monkeypatch.setattr(plugin_module, "datetime", _FixedDatetime)


def test_daily_bucket_uses_china_time(make_weibo_plugin, monkeypatch):
    _patch(monkeypatch)
    plugin = make_weibo_plugin()
    plugin._record_push("123456", True, "hi")
    assert "2026-01-01" in plugin.stats["daily"]
    assert "2025-12-31" not in plugin.stats["daily"]


def test_prune_keeps_today_bucket(make_weibo_plugin, monkeypatch):
    _patch(monkeypatch)
    plugin = make_weibo_plugin()
    plugin.stats["daily"] = {
        "2020-01-01": {"push_ok": 1, "push_fail": 0},
        "2025-12-31": {"push_ok": 1, "push_fail": 0},
    }
    plugin._prune_stats()
    kept = set(plugin.stats["daily"])
    assert "2020-01-01" not in kept
    assert "2025-12-31" in kept, "东八区 2026-01-01 04:30 时，前一天仍在保留窗口内"
