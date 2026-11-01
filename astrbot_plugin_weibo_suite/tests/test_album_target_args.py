"""关键词相册参数解析：数字开头的相册名不能被当成群号（P3-8）。"""

from __future__ import annotations

import time

GROUP = "987654321"


async def _run(plugin, text, *, group_id=None):
    from astrbot_plugin_weibo_suite.tests.conftest import FakeEvent

    origin = f"napcat/GroupMessage:{group_id}" if group_id else "aiocqhttp:FriendMessage:u1"
    event = FakeEvent(text, origin=origin, group_id=group_id)
    kw = text.split()[1]
    return [r async for r in plugin.weibo_keyword_album(event, kw=kw)]


def _rules(plugin):
    return plugin._album_keyword_rules()


async def test_numeric_leading_album_name_stays_in_current_group(make_weibo_plugin):
    plugin = make_weibo_plugin()
    await _run(plugin, "微博关键词相册 福利 123旅行", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", GROUP, "123旅行")]


async def test_real_group_id_with_album_name(make_weibo_plugin):
    plugin = make_weibo_plugin()
    await _run(plugin, "微博关键词相册 福利 1234567890 福利图集", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", "1234567890", "福利图集")]


async def test_known_album_numeric_id_is_not_read_as_group(make_weibo_plugin):
    """解析缓存里已知的纯数字相册 ID，必须留在当前群。"""
    plugin = make_weibo_plugin()
    plugin._album_cache[f"{GROUP}:1234567890"] = (time.time(), "1234567890", "福利图集")
    await _run(plugin, "微博关键词相册 福利 1234567890", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", GROUP, "1234567890")]


async def test_four_digit_leading_album_name_stays_in_current_group(make_weibo_plugin):
    """4 位纯数字开头的相册名（「2024 图集」）必须留在当前群。

    这是把「5 位以上才当群号」这条阈值真正锁住的用例：旧逻辑只判
    rest[0].isdigit()，会把 2024 当群号，规则落到一个不存在的群上。
    """
    plugin = make_weibo_plugin()
    await _run(plugin, "微博关键词相册 福利 2024 图集", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", GROUP, "2024 图集")]


async def test_five_digit_leading_number_without_cache_is_group_id(make_weibo_plugin):
    """5 位纯数字且缓存未命中 → 按群号处理（QQ 群号最短 5 位）。

    与上一条成对：一条锁「<5 位留本群」，一条锁「>=5 位当群号」，把阈值两侧
    都钉死，避免只测一侧导致阈值被改小/改大仍全绿。
    """
    plugin = make_weibo_plugin()
    await _run(plugin, "微博关键词相册 福利 12345 福利图集", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", "12345", "福利图集")]


async def test_known_album_cache_wins_over_group_id_heuristic(make_weibo_plugin):
    """整段命中解析缓存时优先走缓存，即使首段是长数字也不当群号。"""
    plugin = make_weibo_plugin()
    plugin._album_cache[f"{GROUP}:1234567890 福利图集"] = (
        time.time(),
        "alb-1",
        "1234567890 福利图集",
    )
    await _run(plugin, "微博关键词相册 福利 1234567890 福利图集", group_id=GROUP)
    assert _rules(plugin) == [(None, "福利", GROUP, "1234567890 福利图集")]


async def test_private_chat_requires_group_id(make_weibo_plugin):
    plugin = make_weibo_plugin()
    out = await _run(plugin, "微博关键词相册 福利 福利图集")
    assert _rules(plugin) == []
    assert "必须写明群号" in str(out[-1].text)


async def test_private_chat_accepts_group_then_album(make_weibo_plugin):
    plugin = make_weibo_plugin()
    await _run(plugin, "微博关键词相册 福利 1234567890 福利图集")
    assert _rules(plugin) == [(None, "福利", "1234567890", "福利图集")]
