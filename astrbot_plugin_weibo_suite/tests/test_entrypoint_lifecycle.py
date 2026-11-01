"""入口生命周期契约：initialize 失败必须自己回收已建资源。

宿主 astrbot 的 star_manager 在 `await initialize()` 抛错时只做模块级清理
（`_cleanup_plugin_state`），**从不调用 terminate()**。因此 initialize 中途
失败时，已经建起来的 errlog 写线程、sqlite 连接、HTTP 会话、后台任务都会变成
孤儿，重载还会再叠加一份。

这些用例把"失败 → 回滚 → 继续向外抛"这条链钉死，并锁定卸载顺序：
微博域（推送管线）先停，溯源域（sqlite / HTTP）后关。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin
from astrbot_plugin_weibo_suite.tests.conftest import FakeConfig, _Context
from astrbot_plugin_weibo_suite.trace.main import ImageTracePlugin
from astrbot_plugin_weibo_suite.weibo.plugin import WeiboMonitorPlugin


@pytest.fixture
def suite(data_dir):
    """构造真实套件实例，记录两个域 terminate 的调用顺序。"""
    plugin = WeiboSuitePlugin(_Context(), FakeConfig({}))
    plugin._calls = []
    return plugin


def _record_terminate(monkeypatch, cls, label, plugin):
    original = cls.terminate

    async def wrapper(self, *args, **kwargs):
        plugin._calls.append(label)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "terminate", wrapper)


async def test_initialize_failure_rolls_back_both_domains(suite, monkeypatch):
    """溯源域 initialize 抛错 → 两个域都要被 terminate，且异常继续外抛。"""

    async def boom(self):
        raise RuntimeError("trace 初始化炸了")

    monkeypatch.setattr(ImageTracePlugin, "initialize", boom)
    _record_terminate(monkeypatch, WeiboMonitorPlugin, "weibo", suite)
    _record_terminate(monkeypatch, ImageTracePlugin, "trace", suite)

    with pytest.raises(RuntimeError, match="trace 初始化炸了"):
        await suite.initialize()

    assert suite._calls == ["weibo", "trace"], "回滚必须两个域都收，且微博域先收"
    assert suite._trace_initialized is False
    assert suite._weibo_initialized is False


async def test_initialize_failure_after_trace_ok_still_stops_pipeline(suite, monkeypatch):
    """溯源域成功、微博域失败：溯源域的资源同样要回收（以前会全部泄漏）。"""
    trace_initialized = {"done": False}

    async def fake_trace_init(self):
        trace_initialized["done"] = True

    async def boom(self):
        raise RuntimeError("weibo 初始化炸了")

    monkeypatch.setattr(ImageTracePlugin, "initialize", fake_trace_init)
    monkeypatch.setattr(WeiboMonitorPlugin, "initialize", boom)
    _record_terminate(monkeypatch, WeiboMonitorPlugin, "weibo", suite)
    _record_terminate(monkeypatch, ImageTracePlugin, "trace", suite)

    with pytest.raises(RuntimeError, match="weibo 初始化炸了"):
        await suite.initialize()

    assert trace_initialized["done"] is True
    assert suite._calls == ["weibo", "trace"]
    assert suite._trace_initialized is False


async def test_rollback_failure_is_reported_not_raised(suite, monkeypatch, caplog):
    """回滚本身失败要记日志，但不能遮盖原始异常（否则宿主看不到真正的失败原因）。"""

    async def boom_init(self):
        raise RuntimeError("原始失败")

    async def boom_term(self):
        raise OSError("回收也失败")

    monkeypatch.setattr(ImageTracePlugin, "initialize", boom_init)
    monkeypatch.setattr(WeiboMonitorPlugin, "terminate", boom_term)
    monkeypatch.setattr(ImageTracePlugin, "terminate", boom_term)

    with pytest.raises(RuntimeError, match="原始失败"):
        await suite.initialize()


async def test_terminate_stops_weibo_domain_before_trace(suite, monkeypatch):
    """卸载顺序：管线（微博域）先停，trace 后关；否则在途单帖会写已关闭的连接。"""
    suite._weibo_initialized = True
    suite._trace_initialized = True
    _record_terminate(monkeypatch, WeiboMonitorPlugin, "weibo", suite)
    _record_terminate(monkeypatch, ImageTracePlugin, "trace", suite)

    await suite.terminate()

    assert suite._calls == ["weibo", "trace"]


def test_construct_with_context_only(data_dir):
    """宿主"只传 context"的构造路径（新装/配置被删）必须成立。

    star_manager 只有在 data/config/<目录名>_config.json 已存在时才传 config，
    否则走 `star_cls_type(context=...)`；__init__ 把 config 写成必填参数会让
    WebUI 只显示"插件加载失败"且不给原因。
    """
    plugin = WeiboSuitePlugin(_Context())
    assert plugin.config == {}
    assert plugin._trace_plugin is not None
    assert plugin._weibo_initialized is False
    assert plugin._trace_initialized is False


def test_config_object_is_kept_by_reference(data_dir):
    """不能 dict(config) 拷贝：面板 save_config() 的回写通路依赖原对象。"""
    cfg = FakeConfig({"pipeline": {}})
    plugin = WeiboSuitePlugin(_Context(), cfg)
    assert plugin.config is cfg


async def test_terminate_before_initialize_is_safe(data_dir):
    """加载失败时宿主可能直接丢弃实例：没 initialize 就 terminate 不能炸。"""
    plugin = WeiboSuitePlugin(_Context())
    await plugin.terminate()


async def test_terminate_is_idempotent(suite, monkeypatch):
    """宿主重载时会再调一次 terminate：第二次不能再动资源。"""
    _record_terminate(monkeypatch, WeiboMonitorPlugin, "weibo", suite)
    _record_terminate(monkeypatch, ImageTracePlugin, "trace", suite)
    suite._weibo_initialized = True
    suite._trace_initialized = True

    await suite.terminate()
    await suite.terminate()

    assert suite._calls == ["weibo", "trace"]
