"""留存写盘失败的上报与限频（复审指出此处此前零覆盖）。

变异测试证据（复审给的）：`shared/errorlog.py` 的 `_report_write_failure` 开头插
`return` 后，`tests/test_errorlog.py` 仍全绿——因为旧用例只测“写得进去”的成功
路径，写盘失败（磁盘满 / 只读挂载 / 路径被目录占住）这条分支从来没被触发过。

本文件用**真实的 OSError** 触发失败，不 monkeypatch 落盘函数：
把 `errors.log` 这个路径做成一个**目录**，于是 `open(path, "a")` 必然抛
IsADirectoryError（Windows 上是 PermissionError，两者都是 OSError），
不需要伪造任何内部实现。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.shared import errorlog
from astrbot_plugin_weibo_suite.shared.errorlog import (
    install_error_log,
    remove_error_log,
)


class _Plugin:
    def __init__(self, logger: logging.Logger):
        self.logger = logger


class _Collector(logging.Handler):
    """挂在 `_WRITER_LOG` 上收记录（它 propagate=False，caplog 抓不到）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def writer_records():
    collector = _Collector()
    target = errorlog._WRITER_LOG
    target.addHandler(collector)
    try:
        yield collector
    finally:
        target.removeHandler(collector)


@pytest.fixture
def broken_store(tmp_path):
    """装一个“落盘必然失败”的留存器：errors.log 这个路径被一个目录占住。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_fail")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    path = tmp_path / "errors.log"
    path.mkdir()  # 让 open(path, "a") 抛 OSError
    plugin = _Plugin(logger)
    store = install_error_log(plugin, path)
    try:
        yield plugin, store, logger, path
    finally:
        remove_error_log(plugin)


def test_write_failure_is_reported_not_swallowed(broken_store, writer_records):
    """写盘失败必须上报一条，而不是 except OSError: pass 静默吞掉。"""
    _plugin, store, logger, _path = broken_store

    logger.warning("归档失败：写盘会失败的告警")
    assert store.flush_to_disk() is True, "写盘失败不应把 flush 变成阻塞/失败"

    assert len(writer_records.records) == 1, (
        f"写盘失败应上报一次，实际 {[r.getMessage() for r in writer_records.records]}"
    )
    message = writer_records.records[0].getMessage()
    assert "写入 errors.log 失败" in message
    assert "OSError" in message or "Error" in message, "上报里要带上异常本身"
    assert writer_records.records[0].levelno == logging.WARNING


def test_write_failure_survives_strict_root_logger(broken_store, writer_records):
    """宿主把根日志器收紧到 ERROR 时，写盘失败仍要上报。

    回归用例，对应 CI 首次运行在 Python 3.10 上红的三条：`_WRITER_LOG` 以前
    不设自己的 level，"NOTSET 继承根日志器" 让它的可见性跟着全局日志级别走。
    AstrBot 用户把日志级别调到 ERROR 之后，"磁盘满了、留存其实已经失效" 这条
    唯一告警会被静默吞掉，面板依旧显示"已留存 N 条"——正是这块代码当初要修的
    那个误导。写盘失败的上报级别是模块自己的契约，不该由宿主决定。
    """
    _plugin, store, logger, _path = broken_store
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.ERROR)
    try:
        logger.warning("根日志器收紧后的告警")
        assert store.flush_to_disk() is True, "写盘失败不应把 flush 变成阻塞/失败"
    finally:
        root.setLevel(original_level)

    assert len(writer_records.records) == 1, (
        f"根日志器级别不得影响写盘失败上报，实际 {[r.getMessage() for r in writer_records.records]}"
    )
    assert "写入 errors.log 失败" in writer_records.records[0].getMessage()


def test_first_failure_is_reported_on_freshly_booted_host(
    broken_store, writer_records, monkeypatch
):
    """机器开机还不到限频窗口时，第一次失败也必须上报。

    回归用例，对应 CI 上「同一份代码有时红有时绿」的抖动：`time.monotonic()`
    是开机以来的秒数，而 `_last_write_error_ts` 的初值 0.0 既是"从没上报过"
    的哨兵、又被直接拿去相减。开机不足 `_WRITE_FAIL_LOG_INTERVAL` 秒时，
    首次失败被算成"窗口内的重复失败"直接吞掉——正好是这段代码要消灭的静默。
    Actions 的 runner 每次都是新开机器，跑得快的那几次就撞进开机后第一分钟。

    这里用一个只往前的假时钟把"开机 5 秒"稳定下来；时钟必须单调递增，
    否则 `flush_to_disk` 的超时判定永远不成立。
    """
    _plugin, store, logger, _path = broken_store

    ticks = [0]

    def fake_clock() -> float:
        ticks[0] += 1
        return 5.0 + ticks[0] * 0.001

    monkeypatch.setattr(errorlog.time, "monotonic", fake_clock)

    logger.warning("新开机机器上的第一条告警")
    assert store.flush_to_disk() is True, "写盘失败不应把 flush 变成阻塞/失败"

    assert len(writer_records.records) == 1, (
        f"首次失败不得被限频吞掉，实际 {[r.getMessage() for r in writer_records.records]}"
    )
    assert store._suppressed_write_errors == 0, "首次失败不该被记成被抑制"


def test_write_failure_does_not_feed_itself_back(broken_store, writer_records):
    """上报日志不得回流进自己的 handler（会形成“失败→记日志→再失败”的活锁）。"""
    _plugin, store, logger, _path = broken_store

    logger.warning("第一条")
    store.flush_to_disk()
    logger.warning("第二条")
    store.flush_to_disk()

    # 上报用的 logger 必须关掉 propagate，否则会落回全局 astrbot 日志器；
    # 而挂在插件日志器上的留存器更不能收到这条“上报”
    assert errorlog._WRITER_LOG.propagate is False
    assert not any(handler is store for handler in errorlog._WRITER_LOG.handlers), (
        "留存器不能挂在写盘失败上报的 logger 上"
    )
    # 留存缓冲里只应有业务告警（两条），不多出“写盘失败”自己的记录
    messages = [item["msg"] for item in store.snapshot(50)]
    assert messages == ["第二条", "第一条"], messages


def test_write_failures_are_rate_limited(broken_store, writer_records):
    """60 秒内连续失败只出一条，并带上被抑制的次数。"""
    _plugin, store, logger, _path = broken_store

    logger.warning("告警 1")
    store.flush_to_disk()
    logger.warning("告警 2")
    store.flush_to_disk()
    logger.warning("告警 3")
    store.flush_to_disk()

    assert len(writer_records.records) == 1, "60 秒内的后续失败必须被抑制"
    assert store._suppressed_write_errors == 2

    # 把上次上报时间推到限频窗口之外，下次失败应重新上报并说明抑制了多少次
    store._last_write_error_ts -= errorlog._WRITE_FAIL_LOG_INTERVAL + 1
    logger.warning("告警 4")
    store.flush_to_disk()

    assert len(writer_records.records) == 2
    assert "此前 2 次同类失败已抑制" in writer_records.records[1].getMessage()
    assert store._suppressed_write_errors == 0


def test_rotation_failure_is_also_reported(broken_store, writer_records, monkeypatch):
    """轮转失败同样要上报（另一条 except OSError 分支）。"""
    _plugin, store, _logger, path = broken_store

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "stat", boom, raising=False)
    store._rotate_if_needed(10**9)

    assert len(writer_records.records) == 1
    assert "轮转 errors.log 失败" in writer_records.records[0].getMessage()
    assert store._path == path


def test_write_failure_does_not_break_lifecycle(broken_store):
    """写盘失败不能影响正常生命周期：clear/close 仍能完成，不留半死线程。"""
    plugin, store, logger, path = broken_store

    logger.warning("告警")
    assert store.flush_to_disk() is True
    # clear 内部会先 flush 再截断；截断同样会失败，但不得抛出去
    assert store.clear() == 1
    remove_error_log(plugin)
    assert plugin._errlog is None
    assert store._closing is True, "close() 必须真正把后台线程标记为退出"
    assert not store._writer.is_alive() or store._writer.daemon
    assert path.is_dir(), "测试前提：路径仍被目录占住（失败是真的）"
