"""归档 worker 关闭语义：在途 mid 必须收尾，不能被静默丢弃（P3-13）。"""

from __future__ import annotations

import asyncio
import contextlib

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


async def test_paused_mid_is_requeued_and_processed_after_recovery(make_weibo_plugin):
    """磁盘暂停不是"这条做完了"：当前 mid 重投回队列，恢复后继续处理。"""
    plugin = make_weibo_plugin()
    plugin._archive = _FakeStore()
    seen: list[str] = []
    recovered = False
    done = asyncio.Event()

    async def download(mid):
        if not recovered:
            return {"mid": mid, "paused": "disk"}
        seen.append(mid)
        done.set()
        return {"mid": mid}

    async def wait_disk_free():
        nonlocal recovered
        recovered = True

    plugin.archive_download_post = download  # type: ignore[method-assign]
    plugin._archive_wait_disk_free = wait_disk_free  # type: ignore[method-assign]
    queue = plugin._archive_queue()
    task = asyncio.create_task(plugin._archive_worker())
    queue.put_nowait("m1")

    await asyncio.wait_for(done.wait(), timeout=1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert seen == ["m1"], "暂停期间取出的 mid 必须保留并最终处理"


async def test_paused_worker_does_not_consume_remaining_queue_on_shutdown(make_weibo_plugin):
    """关闭时仍在磁盘暂停：不再消费剩余队列，也不靠 task_done 掩盖丢弃。"""
    plugin = make_weibo_plugin()
    plugin._archive = _FakeStore()
    plugin._archive_stop_requested = True
    tried: list[str] = []

    async def download(mid):
        tried.append(mid)
        return {"mid": mid, "paused": "disk"}

    plugin.archive_download_post = download  # type: ignore[method-assign]
    queue = plugin._archive_queue()
    queue.put_nowait("m1")
    queue.put_nowait("m2")
    task = asyncio.create_task(plugin._archive_worker())
    await asyncio.wait_for(task, timeout=1)

    assert tried == ["m1"], "暂停后必须停止本轮消费，不能继续空转吃队列"
    assert queue.qsize() == 2, "当前 mid 已重投，剩余队列必须保留"
