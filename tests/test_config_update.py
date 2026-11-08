"""配置批量写入 helper（`weibo/plugin.py::_update_config`）的离线单测。

面板一次请求可能同时改多个键；逐键落盘中途失败会留下「内存改了、磁盘只写
一半」的混合态。这里锁两件事：写失败要整体回滚内存，成功路径只写一次盘。
"""

from __future__ import annotations

import threading


class CountingConfig(dict):
    def __init__(self, data=None, *, fail=False):
        super().__init__(data or {})
        self.fail = fail
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.fail:
            raise OSError("磁盘写入失败")


def test_update_config_rolls_back_memory_when_save_fails(make_weibo_plugin):
    plugin = make_weibo_plugin()
    plugin.config = CountingConfig({"push": {"at_all": False}}, fail=True)

    ok = plugin._update_config(lambda cfg: cfg["push"].__setitem__("at_all", True))

    assert ok is False
    assert plugin.config == {"push": {"at_all": False}}
    assert plugin.config.saves == 1


def test_update_config_batch_writes_disk_once(make_weibo_plugin):
    plugin = make_weibo_plugin()
    plugin.config = CountingConfig({"push": {"at_all": False}})

    def mutate(cfg):
        cfg["push"]["at_all"] = True
        cfg["monitor"] = {"poll_interval": 300}

    assert plugin._update_config(mutate) is True
    assert plugin.config.saves == 1
    assert plugin.config["push"]["at_all"] is True
    assert plugin.config["monitor"] == {"poll_interval": 300}


def test_update_config_snapshot_failure_returns_false(make_weibo_plugin):
    """配置含不可深拷贝对象时只返回 False，不把异常抛到面板成 500。"""
    plugin = make_weibo_plugin()
    plugin.config = CountingConfig({"push": {"at_all": False}})
    plugin.config["lock"] = threading.Lock()
    mutated = []

    def mutate(cfg):
        mutated.append(True)

    assert plugin._update_config(mutate) is False
    # 快照失败发生在 mutate 之前：内存配置与磁盘都没有被动过
    assert mutated == []
    assert plugin.config["push"] == {"at_all": False}
    assert plugin.config.saves == 0


class AsyncCountingConfig(dict):
    def __init__(self, data=None, *, fail=False):
        super().__init__(data or {})
        self.fail = fail
        self.saves_async = 0
        self.saves_sync = 0

    def save_config(self):
        self.saves_sync += 1

    async def save_config_async(self):
        self.saves_async += 1
        if self.fail:
            raise OSError("磁盘写入失败")


class ThreadRecordingConfig(CountingConfig):
    def __init__(self, data=None):
        super().__init__(data)
        self.thread_ids = []

    def save_config(self):
        self.thread_ids.append(threading.get_ident())
        super().save_config()


async def test_update_config_async_prefers_host_async_saver(make_weibo_plugin):
    plugin = make_weibo_plugin()
    plugin.config = AsyncCountingConfig({"push": {"at_all": False}})

    ok = await plugin._update_config_async(lambda cfg: cfg["push"].__setitem__("at_all", True))

    assert ok is True
    assert plugin.config.saves_async == 1
    assert plugin.config.saves_sync == 0


async def test_update_config_async_falls_back_to_a_worker_thread(make_weibo_plugin):
    """老宿主没有 save_config_async 时，同步写盘必须离开事件循环线程。"""
    plugin = make_weibo_plugin()
    plugin.config = ThreadRecordingConfig({"push": {"at_all": False}})

    ok = await plugin._update_config_async(lambda cfg: cfg["push"].__setitem__("at_all", True))

    assert ok is True
    assert plugin.config.saves == 1
    assert plugin.config.thread_ids and plugin.config.thread_ids[0] != threading.get_ident()


async def test_update_config_async_rolls_back_when_save_fails(make_weibo_plugin):
    plugin = make_weibo_plugin()
    plugin.config = AsyncCountingConfig({"push": {"at_all": False}}, fail=True)

    ok = await plugin._update_config_async(lambda cfg: cfg["push"].__setitem__("at_all", True))

    assert ok is False
    assert plugin.config == {"push": {"at_all": False}}
    assert plugin.config.saves_async == 1
