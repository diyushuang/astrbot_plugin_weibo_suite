"""共用的告警/错误持久化。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.shared.errorlog import (
    ERRLOG_DISK_MSG_LIMIT,
    ERRLOG_MSG_LIMIT,
    _ErrorLogStore,
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


def test_error_log_masks_cookie_header_on_disk_and_panel(tmp_path: Path):
    """Cookie 头里的 session 值既不能落盘，也不能从面板回放出来。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_cookie")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"

    store = install_error_log(plugin, path)
    logger.warning("请求失败 Cookie: session=abc123; theme=dark")
    logger.warning("Set-Cookie: session=xyz; Path=/; HttpOnly")
    assert store.flush_to_disk()

    disk = path.read_text(encoding="utf-8")
    assert "abc123" not in disk
    assert "xyz" not in disk
    assert "session=<凭据已隐藏>" in disk
    assert "theme=dark" in disk
    assert "Path=/" in disk

    combined = "\n".join(item["msg"] for item in store.snapshot(10))
    assert "abc123" not in combined
    assert "xyz" not in combined
    assert "theme=dark" in combined

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


@pytest.mark.parametrize(
    ("size", "truncated"),
    [
        (1024 * 1024, True),
        (512 * 1024, True),
        (100 * 1024, True),
        (40 * 1024, True),
        (32 * 1024, False),
        (31 * 1024, False),
    ],
)
def test_error_log_caps_disk_message_at_32k_bytes(tmp_path: Path, size: int, truncated: bool):
    """落盘单条上限按 UTF-8 字节：超过 32768 字节截断并加省略号，未超过原样保留。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_disk_cap")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    body = "z" * size

    store = install_error_log(plugin, path)
    logger.warning(body)
    assert store.flush_to_disk()

    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    msg = json.loads(line)["msg"]
    if truncated:
        assert msg == "z" * ERRLOG_DISK_MSG_LIMIT + "…"
        assert len(msg) == ERRLOG_DISK_MSG_LIMIT + 1
    else:
        assert msg == body

    panel = store.snapshot(10)[0]["msg"]
    assert len(panel) == ERRLOG_MSG_LIMIT + 1, "面板上限与落盘上限相互独立"

    remove_error_log(plugin)


def test_error_log_rotation_keeps_readable_records(tmp_path: Path):
    """超大单条不再是整文件的拖累：轮转后仍保留多条可解析记录，不会清空。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_rotation")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    body = "z" * ERRLOG_DISK_MSG_LIMIT

    store = install_error_log(plugin, path)
    for i in range(20):
        logger.warning("rec-%02d %s", i, body)
    assert store.flush_to_disk()
    logger.warning("rec-tail after rotation")
    assert store.flush_to_disk()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 2, "轮转不能把文件清空"
    messages = [json.loads(ln)["msg"] for ln in lines]
    assert any("rec-" in msg for msg in messages), "轮转前的记录仍应可读"
    assert any("rec-tail after rotation" in msg for msg in messages)

    remove_error_log(plugin)


def test_error_log_32769_char_line_survives_restart(tmp_path: Path):
    """比上限多一个字符的单行仍能落盘，并在重启（restore）后完整回读。"""
    logger = logging.getLogger("test.weibo_suite.errorlog_32769")
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    plugin = _Plugin(logger)
    path = tmp_path / "errors.log"
    body = "w" * (ERRLOG_DISK_MSG_LIMIT + 1)

    store = install_error_log(plugin, path)
    logger.warning(body)
    assert store.flush_to_disk()
    remove_error_log(plugin)

    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    msg = json.loads(line)["msg"]
    assert msg == "w" * ERRLOG_DISK_MSG_LIMIT + "…"
    assert len(msg) == ERRLOG_DISK_MSG_LIMIT + 1

    again = _Plugin(logging.getLogger("test.weibo_suite.errorlog_32769_restore"))
    again.logger.handlers.clear()
    again.logger.setLevel(logging.WARNING)
    restored = install_error_log(again, path)
    assert restored.restore() == 1
    assert restored.snapshot(10)[0]["msg"] == "w" * ERRLOG_MSG_LIMIT + "…"
    remove_error_log(again)


def test_take_batch_accepts_single_message_at_disk_cap(tmp_path: Path):
    """32769 字符的单行小于单批上限，应作为完整一批取走。"""
    store = _ErrorLogStore(tmp_path / "errors.log")
    line = (
        json.dumps(
            {
                "ts": 1.0,
                "level": "WARNING",
                "src": "x:1",
                "msg": "w" * (ERRLOG_DISK_MSG_LIMIT + 1),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    with store._lock:
        store._pending.append(line)

    batch = store._take_batch()
    assert batch == [line]
    assert len(batch[0].encode("utf-8")) > ERRLOG_DISK_MSG_LIMIT
    store.close(timeout=0.1)
