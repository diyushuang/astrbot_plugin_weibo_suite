"""共用的告警/错误持久化。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from astrbot_plugin_weibo_suite.shared.errorlog import (
    ERRLOG_MSG_LIMIT,
    install_error_log,
    remove_error_log,
)


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


def test_error_log_masks_bearer_token_on_disk_and_panel(tmp_path: Path):
    """认证头令牌既不能落进 errors.log，也不能从 /errors 面板回放出来。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_bearer")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    token = "eyJhbGciOiJIUzI1NiJ9.abc.def"

    store = install_error_log(plugin, path)
    logger.warning("请求失败 authorization: Bearer %s", token)
    assert store.flush_to_disk()

    disk = path.read_text(encoding="utf-8")
    assert token not in disk
    assert "Bearer" not in disk
    records = store.snapshot(10)
    assert records
    assert token not in records[0]["msg"]
    assert "authorization: <凭据已隐藏>" in records[0]["msg"]

    remove_error_log(plugin)


def test_error_log_masks_authorization_inside_url_on_disk_and_panel(tmp_path: Path):
    """URL 查询串里的认证头在落盘与面板回放两侧都不能留下令牌。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_url_bearer")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    messages = [
        "https://h.example.com/x?authorization=Bearer TOKEN failed",
        "https://h.example.com/x?authorization=Basic dXNlcjpwYXNz failed",
        "https://h.example.com/x?authorization=Bearer%20TOKEN failed",
        "https://h.example.com/x?authorization=Bearer TOKEN&x=1",
    ]

    store = install_error_log(plugin, path)
    for text in messages:
        logger.warning(text)
    assert store.flush_to_disk()

    disk = path.read_text(encoding="utf-8")
    assert "TOKEN" not in disk
    assert "dXNlcjpwYXNz" not in disk
    assert "<地址已隐藏><凭据已隐藏>" not in disk

    combined = "\n".join(item["msg"] for item in store.snapshot(10))
    assert "TOKEN" not in combined
    assert "dXNlcjpwYXNz" not in combined
    assert "<地址已隐藏><凭据已隐藏>" not in combined
    assert "<地址已隐藏> failed" in combined

    remove_error_log(plugin)


def test_error_log_keeps_full_message_on_disk_and_truncates_panel(tmp_path: Path):
    """落盘保留完整原文；面板展示才按 ERRLOG_MSG_LIMIT 截断。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_long_message")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    body = "y" * 3000

    store = install_error_log(plugin, path)
    logger.warning(body)
    assert store.flush_to_disk()

    disk = path.read_text(encoding="utf-8")
    assert body in disk
    line = disk.strip().splitlines()[-1]
    assert len(json.loads(line)["msg"]) == len(body)

    records = store.snapshot(10)
    assert records[0]["msg"] == "y" * ERRLOG_MSG_LIMIT + "…"
    assert len(records[0]["msg"]) == ERRLOG_MSG_LIMIT + 1

    remove_error_log(plugin)
