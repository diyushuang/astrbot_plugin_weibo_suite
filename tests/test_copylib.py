"""P5 文案库编排（F3）单测：状态/列表/导出/导入/快照与指令接线。

清洗语义本身在 tests/test_cleaner.py（A 轨道）覆盖；这里把 cleaner 注入成桩，
保证编排逻辑（建目录、写 CSV、按 mid upsert、指纹失效）在任何时候都能离线验证。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive_db import ArchiveStore
from astrbot_plugin_weibo_suite.weibo.constants import (
    COPYLIB_CSV_HEADER,
    COPYLIB_DIR_NAME,
    COPYLIB_LIST_LIMIT,
    USAGE,
)
from astrbot_plugin_weibo_suite.weibo.copylib import (
    CopylibMixin,
    copylib_mid_from_link,
    copylib_uid_from_link,
)
from astrbot_plugin_weibo_suite.weibo.plugin import WeiboMonitorPlugin

from .host_compat import command_name_of

UID_A = "1000000001"
UID_B = "1000000002"

NEW_COMMANDS = {
    "weibo_copylib_status": ("文案库", False),
    "weibo_copylib_list": ("文案列表", False),
    "weibo_copylib_rebuild": ("文案清洗", True),
    "weibo_copylib_export": ("文案导出", True),
    "weibo_copylib_import": ("文案导入", True),
}


async def _collect(agen):
    return [item async for item in agen]


def _texts(results) -> str:
    return "\n".join(str(getattr(r, "text", "")) for r in results)


def make_post(mid: str = "1001", uid: str = UID_A, **overrides) -> dict:
    post = {
        "mid": mid,
        "uid": uid,
        "author": "博主A",
        "bid": f"B{mid}",
        "link": f"https://weibo.com/{uid}/{mid}",
        "created_at": 1_700_000_000,
        "created_raw": "Sat Mar 08 16:51:30 +0800 2025",
        "text": "#话题# 正文 https://t.cn/abc",
        "text_raw": "<p>正文</p>",
        "is_long": 0,
        "is_retweet": 0,
        "orig_text": "",
        "pic_total": 1,
        "pics": [
            {
                "pid": f"p{mid}",
                "url": "https://wx1.sinaimg.cn/large/a.jpg",
                "alt_url": "",
                "ext": "jpg",
                "kind": "image",
            }
        ],
        "video_src": "",
        "raw_json": "{}",
        "source": "monitor",
    }
    post.update(overrides)
    return post


@pytest.fixture
def store(tmp_path: Path):
    instance = ArchiveStore(tmp_path / "archive.db", tmp_path / "archive")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def plugin(make_weibo_plugin):
    return make_weibo_plugin()


@pytest.fixture
def wired(plugin, store, data_dir):
    """把真实 ArchiveStore 挂到插件上，等价于 archive_open 成功后的状态。"""
    plugin._archive = store
    for post in (
        make_post("1001", UID_A, text="#早安# 今天也很好 https://t.cn/aaa"),
        make_post("1002", UID_A, text="第二条", is_long=1),
        make_post("2001", UID_B, text="另一个账号", is_retweet=1),
    ):
        store.upsert_post(post)
        store.upsert_media(post["mid"], post["pics"])
    return plugin


@pytest.fixture
def fake_cleaner(monkeypatch):
    """注入 cleaner 桩：clean_mblog_text 只做可辨识的标记替换。"""
    import types

    module = types.ModuleType("astrbot_plugin_weibo_suite.weibo.cleaner")
    module.clean_mblog_text = lambda text, long_text="": "CLEAN:" + str(text).replace("#话题#", "")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    package = sys.modules.get("astrbot_plugin_weibo_suite.weibo")
    if package is not None:
        monkeypatch.setattr(package, "cleaner", module, raising=False)
    return module


# ---------------------------------------------------------------- 常量 / 接线


def test_copylib_constants_and_usage():
    assert COPYLIB_DIR_NAME == "copylib"
    assert COPYLIB_LIST_LIMIT == 10
    assert COPYLIB_CSV_HEADER == ("序号", "发布时间", "来源", "文案", "微博链接")
    for line in (
        "微博文案库",
        "微博文案列表",
        "微博文案清洗",
        "微博文案导出",
        "微博文案导入",
    ):
        assert line in USAGE, line


def _registered_filters(func) -> list:
    """取处理器注册在 star_handlers_registry 上的 event_filters。

    桩模式只有装饰器写回的函数属性（这里返回空表）；真宿主下指令组与权限只
    挂在过滤器实例上。取数逻辑与 tests/test_archive_commands.py 同一份，将来
    可收编进 tests/host_compat.py。
    """
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:  # 桩模式
        return []
    for handler_md in star_handlers_registry:
        if getattr(handler_md, "handler", None) is func:
            return list(getattr(handler_md, "event_filters", ()) or ())
    return []


def _command_group_of(func) -> str | None:
    for event_filter in _registered_filters(func):
        names = getattr(event_filter, "parent_command_names", None)
        if names:
            return names[0]
    return getattr(func, "command_group_name", None)


def _permission_of(func) -> str | None:
    for event_filter in _registered_filters(func):
        permission = getattr(event_filter, "permission_type", None)
        if permission is not None and getattr(permission, "name", ""):
            return permission.name.lower()
    return getattr(func, "permission_required", None)


def test_five_copylib_commands_registered():
    # 指令注册统一在入口 main.py（AstrBot 只按入口模块绑定 handler）
    from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin

    for method, (name, admin) in NEW_COMMANDS.items():
        func = getattr(WeiboSuitePlugin, method)
        assert command_name_of(func) == name, method
        assert _command_group_of(func) == "微博", method
        assert _permission_of(func) == ("admin" if admin else None), method


def test_mixin_order_keeps_single_source():
    names = [cls.__name__ for cls in WeiboMonitorPlugin.__mro__]
    assert names.index("CopylibMixin") > names.index("ArchiveMixin")
    assert names.index("CopylibMixin") < names.index("CommandsMixin")
    # 文案库读库，不经手管线
    assert "pipeline_notify_archived" not in dir(CopylibMixin)


def test_link_parsing_helpers():
    assert copylib_mid_from_link(f"https://weibo.com/{UID_A}/5001234567") == "5001234567"
    assert copylib_mid_from_link("https://m.weibo.cn/status/5001234567") == "5001234567"
    assert copylib_mid_from_link("https://weibo.com/abc") == ""
    assert copylib_mid_from_link("") == ""
    assert copylib_uid_from_link(f"https://weibo.com/{UID_A}/5001234567") == UID_A
    assert copylib_uid_from_link("https://m.weibo.cn/status/5001234567") == ""


# ---------------------------------------------------------------- 状态 / 列表


def test_status_without_store(plugin):
    stats = plugin.copylib_status()
    assert stats["ok"] is False
    assert "归档库未就绪" in stats["error"]
    assert "文案库不可用" in plugin.format_copylib_status()


def test_status_counts_and_accounts(wired):
    stats = wired.copylib_status()
    assert stats["ok"] is True
    assert stats["total"] == 3
    assert stats["cleaned"] == 0
    assert stats["dirty"] == 3
    assert stats["long"] == 1
    assert stats["retweet"] == 1
    accounts = {row["uid"]: row for row in stats["accounts"]}
    assert accounts[UID_A]["total"] == 2
    assert accounts[UID_B]["total"] == 1
    text = wired.format_copylib_status()
    assert "帖子 3 条" in text
    assert "待清洗 3" in text


def test_status_scoped_by_uid(wired):
    stats = wired.copylib_status(UID_A)
    assert stats["total"] == 2
    assert stats["long"] == 1
    text = wired.format_copylib_status(UID_A)
    assert f"账号 {UID_A}" in text


def test_list_prefers_cleaned_text_and_truncates(wired):
    wired.archive_store().mark_post_cleaned("1001", "清洗后的正文")
    rows = wired.copylib_list(UID_A)
    assert len(rows) == 2
    first = next(row for row in rows if row["mid"] == "1001")
    assert first["text"] == "清洗后的正文"
    assert first["cleaned"] is True
    assert first["published_at"].startswith("2023-11-1")

    long_post = make_post("1003", UID_A, text="长" * 300)
    wired.archive_store().upsert_post(long_post)
    rows = wired.copylib_list(UID_A)
    text = wired.format_copylib_list(rows, UID_A)
    assert "…" in text
    assert "长文" in text


def test_list_keyword_filter(wired):
    rows = wired.copylib_list("", "另一个")
    assert [row["mid"] for row in rows] == ["2001"]
    assert "转发" in wired.format_copylib_list(rows)


def test_list_empty_renders_placeholder(wired):
    assert wired.copylib_list("9999") == []
    assert "文案库为空" in wired.format_copylib_list([], "9999")


# ---------------------------------------------------------------- 清洗回填


async def test_rebuild_writes_cleaned_text(wired, fake_cleaner):
    stats = await wired.copylib_rebuild()
    assert stats["ok"] is True
    assert stats["scanned"] == 3
    assert stats["cleaned"] == 3
    row = wired.archive_store().get_post("1001")
    assert row["cleaned_text"].startswith("CLEAN:")
    # 已清洗的不再重复扫描
    again = await wired.copylib_rebuild()
    assert again["scanned"] == 0


async def test_rebuild_respects_uid_and_limit(wired, fake_cleaner):
    stats = await wired.copylib_rebuild(UID_A, 1)
    assert stats["scanned"] == 1
    assert stats["cleaned"] == 1


async def test_rebuild_degrades_when_cleaner_missing(wired, monkeypatch):
    monkeypatch.setitem(sys.modules, "astrbot_plugin_weibo_suite.weibo.cleaner", None)
    stats = await wired.copylib_rebuild()
    assert stats["ok"] is False
    assert "文案清洗模块" in stats["error"]


async def test_rebuild_without_store(plugin):
    stats = await plugin.copylib_rebuild()
    assert stats["ok"] is False
    assert stats["scanned"] == 0


# ---------------------------------------------------------------- 导出 / 导入


async def test_export_csv_matches_node_layout(wired, data_dir):
    wired.archive_store().mark_post_cleaned("1001", "清洗后的正文")
    result = await wired.copylib_export()
    assert result["ok"] is True
    assert result["count"] == 3
    path = Path(result["path"])
    assert path.parent.name == COPYLIB_DIR_NAME
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM：Excel 才能正确识别中文
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    assert tuple(rows[0]) == COPYLIB_CSV_HEADER
    assert len(rows) == 4
    assert rows[1][0] == "1"
    assert rows[1][2] == "微博@博主A"
    body = [row[3] for row in rows[1:]]
    assert "清洗后的正文" in body


async def test_export_markdown_groups_by_year(wired):
    result = await wired.copylib_export_markdown()
    assert result["ok"] is True
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "# 微博原创文案" in text
    assert "## 2023 年" in text
    assert "微博@博主A" in text


async def test_export_empty_library_reports_error(plugin, store):
    plugin._archive = store
    result = await plugin.copylib_export()
    assert result["ok"] is False
    assert "文案库为空" in result["error"]


def _write_csv(path: Path, rows) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COPYLIB_CSV_HEADER)
        for row in rows:
            writer.writerow(row)
    return path


async def test_import_csv_upserts_and_cleans(plugin, store, tmp_path):
    plugin._archive = store
    path = _write_csv(
        tmp_path / "copy.csv",
        [
            (
                1,
                "2023-11-15 10:30",
                "微博@博主A",
                "导入的文案",
                f"https://weibo.com/{UID_A}/5001234567",
            ),
            (
                2,
                "2024-01-02 09:00",
                "微博@博主B",
                "第二条",
                f"https://weibo.com/{UID_B}/5009999999",
            ),
        ],
    )
    result = await plugin.copylib_import(str(path))
    assert result["ok"] is True
    assert result["records"] == 2
    assert result["imported"] == 2
    stored = store.get_post("5001234567")
    assert stored["cleaned_text"] == "导入的文案"
    assert stored["uid"] == UID_A
    assert stored["author"] == "博主A"
    assert stored["source"] == "csv"
    assert stored["created_at"] > 0


async def test_import_is_idempotent(plugin, store, tmp_path):
    plugin._archive = store
    path = _write_csv(
        tmp_path / "copy.csv",
        [
            (
                1,
                "2023-11-15 10:30",
                "微博@博主A",
                "导入的文案",
                f"https://weibo.com/{UID_A}/5001234567",
            )
        ],
    )
    await plugin.copylib_import(str(path))
    await plugin.copylib_import(str(path))
    assert store.count_posts(UID_A) == 1


async def test_import_skips_rows_without_mid(plugin, store, tmp_path):
    plugin._archive = store
    path = _write_csv(
        tmp_path / "copy.csv",
        [(1, "2023-11-15 10:30", "微博@博主A", "无链接", "")],
    )
    result = await plugin.copylib_import(str(path))
    assert result["ok"] is True
    assert result["imported"] == 0
    assert store.count_posts() == 0


async def test_import_missing_file_reports_error(plugin):
    result = await plugin.copylib_import("no-such-file.csv")
    assert result["ok"] is False
    assert "文件不存在" in result["error"]


# ---------------------------------------------------------------- 快照（签到桥）


def test_snapshot_returns_signature_and_entries(wired):
    signature, rows = wired.copylib_snapshot()
    assert signature
    assert rows == []  # 未清洗的不进签到池
    wired.archive_store().mark_post_cleaned("1001", "清洗后的正文")
    new_signature, rows = wired.copylib_snapshot()
    assert new_signature != signature
    assert [row["mid"] for row in rows] == ["1001"]
    entry = rows[0]
    assert entry["cleaned_text"] == "清洗后的正文"
    assert entry["image_url"] == "https://wx1.sinaimg.cn/large/a.jpg"
    assert entry["thumb_url"] == ""
    assert entry["local_path"] == ""


def test_snapshot_without_store(plugin):
    assert plugin.copylib_snapshot() == ("", [])


def test_snapshot_filters_by_uid(wired):
    wired.archive_store().mark_post_cleaned("1001", "A")
    wired.archive_store().mark_post_cleaned("2001", "B")
    _signature, rows = wired.copylib_snapshot(UID_B)
    assert [row["mid"] for row in rows] == ["2001"]


# ---------------------------------------------------------------- 指令入口


async def test_command_status_and_list(wired, make_event):
    events = await _collect(wired.weibo_copylib_status(make_event("/微博文案库")))
    assert "帖子 3 条" in _texts(events)
    events = await _collect(wired.weibo_copylib_list(make_event(f"/微博文案列表 {UID_A}"), UID_A))
    assert "微博@" in _texts(events)


async def test_command_rebuild_reports_counts(wired, fake_cleaner, make_event):
    events = await _collect(wired.weibo_copylib_rebuild(make_event("/微博文案清洗")))
    text = _texts(events)
    assert "扫描 3 帖" in text
    assert "清洗 3 帖" in text


async def test_command_export_and_import(wired, make_event, tmp_path):
    events = await _collect(wired.weibo_copylib_export(make_event("/微博文案导出 csv")))
    text = _texts(events)
    assert "CSV" in text
    exported = [line for line in text.splitlines() if line.strip().endswith(".csv")]
    assert exported and Path(exported[-1]).is_file()

    csv_path = _write_csv(
        tmp_path / "in.csv",
        [(1, "2023-11-15 10:30", "微博@博主A", "x", f"https://weibo.com/{UID_A}/5001234568")],
    )
    events = await _collect(wired.weibo_copylib_import(make_event("/微博文案导入"), str(csv_path)))
    assert "入库 1 条" in _texts(events)


async def test_command_import_usage_hint(wired, make_event):
    events = await _collect(wired.weibo_copylib_import(make_event("/微博文案导入")))
    assert "用法" in _texts(events)
