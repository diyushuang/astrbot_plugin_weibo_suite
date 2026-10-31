"""Shared warning/error persistence."""

from __future__ import annotations

import logging
from pathlib import Path

from astrbot_plugin_weibo_suite.shared.errorlog import install_error_log, remove_error_log


class _Plugin:
    def __init__(self, logger: logging.Logger):
        self.logger = logger


def test_error_log_round_trip_and_close(tmp_path: Path):
    logger = logging.getLogger("test.weibo_suite.errorlog")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"

    store = install_error_log(plugin, path)
    logger.warning("归档失败：测试告警")
    assert store.flush_to_disk()

    records = store.snapshot(10)
    assert records
    assert records[0]["msg"] == "归档失败：测试告警"
    assert path.is_file()

    remove_error_log(plugin)
    assert plugin._errlog is None
    assert not any(handler is store for handler in logger.handlers)
