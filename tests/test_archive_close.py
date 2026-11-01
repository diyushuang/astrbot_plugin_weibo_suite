"""归档 worker 关闭语义：在途 mid 必须收尾，不能被静默丢弃（P3-13）。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_weibo_suite.weibo import archive as archive_mod


class _FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


async def test_close_lets_inflight_download_finish(make_weibo_plugin):
    """关闭时若正在下原图，先让它下完再关库——否则这条 mid 永远停在 pending。"""
    plugin = make_weibo_plugin()
    plugin._archive = _FakeStore()
    started = asyncio.Event()
    finished: list[str] = []

    async def slow_download(mid):
        started.set()
        await asyncio.sleep(0.05)
        finished.append(mid)
        return {"mid": mid}

    plugin.archive_download_post = slow_download  # type: ignore[method-assign]
    plugin._archive_worker_task = asyncio.create_task(plugin._archive_worker())
    plugin.archive_enqueue_download("m1")
    await started.wait()

    await plugin.archive_close()

    assert finished == ["m1"], "在途 mid 必须跑完"
    assert plugin._archive is None
    assert plugin._archive_worker_task is None


async def test_close_leaves_no_worker_task(make_weibo_plugin):
    plugin = make_weibo_plugin()
    plugin._archive = _FakeStore()
    plugin._archive_worker_task = asyncio.create_task(plugin._archive_worker())
    await asyncio.sleep(0)
    await plugin.archive_close()
    assert plugin._archive_worker_task is None


async def test_worker_exits_on_stop_sentinel_not_by_cancel(make_weibo_plugin):
    """干净退出（非 cancel）：哨兵之后还有 mid 也不该再处理。"""
    plugin = make_weibo_plugin()
    plugin._archive = _FakeStore()
    seen: list[str] = []

    async def record(mid):
        seen.append(mid)
        return {"mid": mid}

    plugin.archive_download_post = record  # type: ignore[method-assign]
    queue = plugin._archive_queue()
    task = asyncio.create_task(plugin._archive_worker())
    queue.put_nowait("m1")
    queue.put_nowait(archive_mod._ARCHIVE_STOP)
    queue.put_nowait("m2")
    await asyncio.wait_for(task, timeout=1)

    assert seen == ["m1"], "哨兵之后的 mid 不应被处理"
    assert not task.cancelled(), "必须是 return 退出而不是被 cancel"
