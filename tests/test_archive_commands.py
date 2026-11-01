"""P3 归档接线（C 轨道）：指令注册、宿主生命周期、三条钩子。

归档业务本身由 weibo/archive.py 提供；这里一律 monkeypatch 成桩，
既保证用例纯离线，也不会被 B 轨道的实现细节绑住。
"""

from __future__ import annotations

import asyncio
import time

from astrbot_plugin_weibo_suite.constants import PLUGIN_VERSION as ROOT_PLUGIN_VERSION
from astrbot_plugin_weibo_suite.weibo.constants import (
    ARCHIVE_DB_NAME,
    ARCHIVE_DIR_NAME,
    ARCHIVE_QUEUE_MAX,
    BACKFILL_SAFETY_MAX_PAGES,
    MEDIA_STATES,
    PLUGIN_VERSION,
    USAGE,
)

from .host_compat import command_name_of

UID = "1234567890"

# _check_account 入队的 14 个键：归档钩子不许改变这个结构
PENDING_KEYS = {
    "post_id",
    "uid",
    "text",
    "raw_text",
    "created_ts",
    "retries",
    "pics",
    "is_video",
    "videos",
    "videos_ts",
    "video_files",
    "video_attempts",
    "video_fail",
    "video_note",
}

# 6 条新指令：方法名 -> (子指令名, 是否管理员)
NEW_COMMANDS = {
    "weibo_backfill": ("回填", True),
    "weibo_backfill_status": ("回填状态", False),
    "weibo_backfill_stop": ("回填停止", True),
    "weibo_archive_import": ("归档导入", True),
    "weibo_archive_query_cmd": ("归档查询", False),
    "weibo_archive_reconcile": ("对账", True),
}


async def _collect(agen):
    return [item async for item in agen]


def _texts(results) -> str:
    return "\n".join(str(getattr(r, "text", "")) for r in results)


class _FakeStore:
    """ArchiveStore 的最小替身：只覆盖指令/状态读到的接口。"""

    def __init__(self, posts=0, stats=None, cursors=None):
        self._posts = posts
        self._stats = stats or {}
        self._cursors = cursors or {}

    def count_posts(self, uid=None, *, source=None):
        return self._posts

    def media_stats(self, uid=None):
        return dict(self._stats)

    def get_cursor(self, uid):
        return self._cursors.get(uid)


def _post(post_id="501", **overrides) -> dict:
    """_parse_post 的产物形状（字段齐全，钩子只读不写）。"""
    post = {
        "id": post_id,
        "uid": UID,
        "author": "博主A",
        "link": f"https://m.weibo.cn/detail/{post_id}",
        "time": "2026-10-31 12:00",
        "created_ts": time.time(),
        "text": "正文内容",
        "is_retweet": False,
        "orig_name": "",
        "orig_text": "",
        "orig_id": "",
        "orig_is_long": False,
        "is_long": False,
        "pics": [{"pid": "p1", "url": "https://wx1.sinaimg.cn/large/p1.jpg", "kind": "photo"}],
        "pic_total": 1,
        "is_video": False,
        "video_src": [],
    }
    post.update(overrides)
    return post


def _ready_plugin(plugin) -> None:
    """把账号表置成"已过基线"的最小状态，_check_account 才能走到入队分支。"""
    plugin.accounts[UID] = {"name": "博主A", "seen_ids": [], "baseline_done": True}


# ---------------------------------------------------------------- 常量 / USAGE


def test_archive_constants_match_contract():
    assert ARCHIVE_DB_NAME == "archive.db"
    assert ARCHIVE_DIR_NAME == "archive"
    assert ARCHIVE_QUEUE_MAX == 500
    assert BACKFILL_SAFETY_MAX_PAGES == 1000
    assert MEDIA_STATES == ("pending", "downloaded", "album", "bed", "thumb", "vector")
    # 版本号由 scripts/build_release.py 统一校验（metadata / 包根 / 微博域三处
    # 必须一致），这里只防"微博域那份常量被误删或单独漂移"。
    assert PLUGIN_VERSION == ROOT_PLUGIN_VERSION


def test_usage_lists_six_archive_commands():
    for line in (
        "微博回填 <uid>",
        "微博回填状态",
        "微博回填停止",
        "微博归档导入",
        "微博归档查询",
        "微博对账",
    ):
        assert line in USAGE


# ---------------------------------------------------------------- 指令注册


def _registered_filters(func) -> list:
    """取某个处理器注册在 star_handlers_registry 上的 event_filters。

    桩模式没有这个模块（只有装饰器写回的函数属性，返回空表）；真宿主模式下
    指令名/指令组/权限**只**存在于过滤器实例上，函数本身不带任何属性。
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
    """指令组名：桩读函数属性，真宿主读 CommandFilter.parent_command_names。"""
    for event_filter in _registered_filters(func):
        names = getattr(event_filter, "parent_command_names", None)
        if names:
            return names[0]
    return getattr(func, "command_group_name", None)


def _permission_of(func) -> str | None:
    """权限：桩读函数属性，真宿主读 PermissionTypeFilter.permission_type。"""
    for event_filter in _registered_filters(func):
        permission = getattr(event_filter, "permission_type", None)
        if permission is not None and getattr(permission, "name", ""):
            return permission.name.lower()
    return getattr(func, "permission_required", None)


def test_six_archive_commands_registered_under_weibo_group():
    # 指令注册统一在入口 main.py（AstrBot 只按入口模块绑定 handler）
    from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin

    for method, (name, admin) in NEW_COMMANDS.items():
        func = getattr(WeiboSuitePlugin, method)
        assert command_name_of(func) == name, method
        assert _command_group_of(func) == "微博", method
        assert _permission_of(func) == ("admin" if admin else None), method


def test_command_surface_grows_without_losing_legacy_commands():
    from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin

    from . import test_weibo_migration as legacy

    found = set()
    for node in dir(WeiboSuitePlugin):
        if not node.startswith("weibo_"):
            continue
        member = getattr(WeiboSuitePlugin, node)
        # 真宿主下"是否注册成指令"只能查注册表（函数上没有 command_name 属性）
        if callable(member) and command_name_of(member):
            found.add(node)
    assert found >= set(NEW_COMMANDS)
    assert len(found) >= 23
    assert found >= legacy.LEGACY_WEIBO_COMMANDS


# ---------------------------------------------------------------- 宿主状态


def test_plugin_host_state_and_mro_order(make_weibo_plugin):
    plugin = make_weibo_plugin()
    assert plugin._archive_dl_queue.maxsize == ARCHIVE_QUEUE_MAX
    assert plugin._archive_worker_task is None
    assert plugin._backfill_stop is False
    assert plugin._backfill_task is None
    assert plugin._archive is None
    assert plugin._backfill_last_stats == {}
    names = [cls.__name__ for cls in type(plugin).__mro__]
    assert names.index("ArchiveMixin") > names.index("AlbumMixin")
    assert names.index("ArchiveMixin") < names.index("CommandsMixin")


async def test_terminate_closes_archive_and_cancels_backfill(
    make_weibo_plugin, data_dir, monkeypatch
):
    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    closed = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr(plugin, "archive_close", fake_close)
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(3600)

    plugin._backfill_task = asyncio.create_task(forever())
    await started.wait()
    await plugin.terminate()
    assert closed == [True]
    assert plugin._backfill_task is None
    assert plugin._http is None


# ---------------------------------------------------------------- 微博回填


async def test_backfill_rejects_missing_uid(make_weibo_plugin, make_event):
    plugin = make_weibo_plugin()
    results = await _collect(plugin.weibo_backfill(make_event("微博回填")))
    assert "用法：微博回填" in _texts(results)
    assert plugin._backfill_task is None


async def test_backfill_starts_background_task(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    calls = []

    async def fake_backfill(uid, *, since="", full=False):
        calls.append((uid, since, full))
        return {"uid": uid}

    monkeypatch.setattr(plugin, "archive_backfill", fake_backfill)
    monkeypatch.setattr(plugin, "archive_backfill_running", lambda: False)
    results = await _collect(
        plugin.weibo_backfill(make_event(f"微博回填 {UID} --since 2024-01-01 --full"))
    )
    assert "已开始回填" in _texts(results)
    assert plugin._backfill_task is not None
    await plugin._backfill_task
    assert calls == [(UID, "2024-01-01", True)]


async def test_backfill_refuses_when_another_run_is_active(
    make_weibo_plugin, make_event, monkeypatch
):
    plugin = make_weibo_plugin()
    monkeypatch.setattr(plugin, "archive_backfill_running", lambda: True)
    results = await _collect(plugin.weibo_backfill(make_event(f"微博回填 {UID}")))
    assert "已有回填任务在跑" in _texts(results)
    assert plugin._backfill_task is None


async def test_backfill_status_reports_stats_and_cursor(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    _ready_plugin(plugin)
    plugin._backfill_last_stats = {"uid": UID, "status": "done"}
    monkeypatch.setattr(plugin, "archive_backfill_running", lambda: True)
    monkeypatch.setattr(plugin, "format_backfill", lambda stats: "统计：新增 3 条")
    monkeypatch.setattr(
        plugin,
        "archive_store",
        lambda: _FakeStore(cursors={UID: {"status": "running", "next_page": 3}}),
    )
    text = _texts(await _collect(plugin.weibo_backfill_status(make_event("微博回填状态"))))
    assert "任务：进行中" in text
    assert "统计：新增 3 条" in text
    assert "博主A" in text
    assert "下一页 3" in text


async def test_backfill_status_without_stats_is_explicit(
    make_weibo_plugin, make_event, monkeypatch
):
    plugin = make_weibo_plugin()
    monkeypatch.setattr(plugin, "archive_backfill_running", lambda: False)
    monkeypatch.setattr(plugin, "archive_store", lambda: None)
    text = _texts(await _collect(plugin.weibo_backfill_status(make_event("微博回填状态"))))
    assert "任务：空闲" in text
    assert "最近统计：暂无" in text
    assert "游标：无" in text


async def test_backfill_stop_calls_archive_stop(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    hits = []
    monkeypatch.setattr(plugin, "archive_backfill_stop", lambda: hits.append(True))
    text = _texts(await _collect(plugin.weibo_backfill_stop(make_event("微博回填停止"))))
    assert hits == [True]
    assert "已请求停止回填" in text


# ---------------------------------------------------------------- 归档导入


async def test_archive_import_keeps_spaces_in_path(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    seen = []

    async def fake_import(path):
        seen.append(path)
        return {"file": path, "total": 2}

    monkeypatch.setattr(plugin, "archive_import_json", fake_import)
    monkeypatch.setattr(plugin, "format_import", lambda stats: f"导入完成 {stats['total']} 条")
    results = await _collect(
        plugin.weibo_archive_import(make_event("微博归档导入 /data/my seeds/a b.json"))
    )
    texts = _texts(results)
    assert "正在导入归档" in texts
    assert "导入完成 2 条" in texts
    assert seen == ["/data/my seeds/a b.json"]


async def test_archive_import_requires_path(make_weibo_plugin, make_event):
    plugin = make_weibo_plugin()
    text = _texts(await _collect(plugin.weibo_archive_import(make_event("微博归档导入"))))
    assert "用法：微博归档导入" in text


async def test_archive_import_reports_error(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()

    async def fake_import(path):
        return {"error": "文件不存在"}

    monkeypatch.setattr(plugin, "archive_import_json", fake_import)
    text = _texts(
        await _collect(plugin.weibo_archive_import(make_event("微博归档导入 /nope.json")))
    )
    assert "导入失败：文件不存在" in text


# ---------------------------------------------------------------- 归档查询


async def test_archive_query_renders_rows_and_paging(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    seen = []
    long_text = "甲" * 60

    async def fake_query(uid="", *, limit=10, offset=0):
        seen.append((uid, limit, offset))
        return [
            {
                "uid": UID,
                "author": "博主A",
                "created_at": 1790000000,
                "text": long_text,
                "pics": [{"pid": "p1"}, {"pid": "p2"}],
            },
            {"uid": UID, "author": "", "created_at": 0, "created_raw": "未知时间", "text": ""},
        ]

    monkeypatch.setattr(plugin, "archive_query", fake_query)
    event = make_event(f"微博归档查询 {UID} 2")
    text = _texts(await _collect(plugin.weibo_archive_query_cmd(event)))
    assert seen == [(UID, 10, 10)]
    assert "第 2 页" in text
    assert "@博主A" in text
    assert "甲" * 40 + "…" in text
    assert "（2 图）" in text
    assert "未知时间" in text
    assert "（无正文）" in text


async def test_archive_query_empty_and_default_page(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    seen = []

    async def fake_query(uid="", *, limit=10, offset=0):
        seen.append((uid, offset))
        return []

    monkeypatch.setattr(plugin, "archive_query", fake_query)
    text = _texts(await _collect(plugin.weibo_archive_query_cmd(make_event("微博归档查询"))))
    assert seen == [("", 0)]
    assert "归档暂无微博" in text


# ---------------------------------------------------------------- 对账


async def test_archive_reconcile_passes_uid_and_formats(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    seen = []

    async def fake_reconcile(uid=""):
        seen.append(uid)
        return {"uid": uid}

    monkeypatch.setattr(plugin, "archive_reconcile", fake_reconcile)
    monkeypatch.setattr(plugin, "format_reconcile", lambda report: f"对账（{report['uid']}）")
    text = _texts(await _collect(plugin.weibo_archive_reconcile(make_event(f"微博对账 {UID}"))))
    assert seen == [UID]
    assert f"对账（{UID}）" in text


# ---------------------------------------------------------------- 微博状态


async def test_status_shows_archive_disabled(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    monkeypatch.setattr(plugin, "archive_store", lambda: None)
    text = _texts(await _collect(plugin.weibo_status(make_event("微博状态"))))
    assert "归档：未启用" in text


async def test_status_shows_archive_counts(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()
    store = _FakeStore(posts=7, stats={"total": 9, "downloaded": 5})
    monkeypatch.setattr(plugin, "archive_store", lambda: store)
    text = _texts(await _collect(plugin.weibo_status(make_event("微博状态"))))
    assert "归档：7 帖 / 9 图（已落盘 5）" in text


async def test_status_survives_broken_archive_stats(make_weibo_plugin, make_event, monkeypatch):
    plugin = make_weibo_plugin()

    class _Broken(_FakeStore):
        def count_posts(self, uid=None, *, source=None):
            raise RuntimeError("db locked")

    monkeypatch.setattr(plugin, "archive_store", lambda: _Broken())
    text = _texts(await _collect(plugin.weibo_status(make_event("微博状态"))))
    assert "归档：统计读取失败" in text


# ---------------------------------------------------------------- monitor 钩子


def _stub_monitor(plugin, monkeypatch, archive_post):
    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(plugin, "_fetch_timeline", _async_return([_post()]))
    monkeypatch.setattr(plugin, "_build_message", lambda post: f"消息:{post['id']}")
    monkeypatch.setattr(plugin, "_ensure_full_pics", noop)
    monkeypatch.setattr(plugin, "_apply_video", noop)
    monkeypatch.setattr(plugin, "archive_post", archive_post)


def _async_return(value):
    async def inner(*_args, **_kwargs):
        return value

    return inner


async def test_monitor_archives_after_filters_and_keeps_pending_shape(
    make_weibo_plugin, monkeypatch
):
    plugin = make_weibo_plugin()
    _ready_plugin(plugin)
    archived = []

    async def fake_archive(post, *, source="monitor"):
        archived.append((post["id"], source))
        return True

    _stub_monitor(plugin, monkeypatch, fake_archive)
    monkeypatch.setattr(plugin, "_archive_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_archive_bool", lambda key, default: default)
    count, _dirty = await plugin._check_account(UID)
    assert count == 1
    assert archived == [("501", "monitor")]
    assert len(plugin.pending) == 1
    assert set(plugin.pending[0]) == PENDING_KEYS


async def test_monitor_archive_failure_never_blocks_push(make_weibo_plugin, monkeypatch):
    plugin = make_weibo_plugin()
    _ready_plugin(plugin)

    async def boom(_post, *, source="monitor"):
        raise RuntimeError("磁盘满了")

    _stub_monitor(plugin, monkeypatch, boom)
    monkeypatch.setattr(plugin, "_archive_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_archive_bool", lambda key, default: default)
    count, _dirty = await plugin._check_account(UID)
    assert count == 1
    assert len(plugin.pending) == 1
    assert plugin.pending[0]["post_id"] == "501"


async def test_monitor_skips_archive_when_disabled_or_auto_off(make_weibo_plugin, monkeypatch):
    plugin = make_weibo_plugin()
    _ready_plugin(plugin)
    archived = []

    async def fake_archive(post, *, source="monitor"):
        archived.append(post["id"])
        return True

    _stub_monitor(plugin, monkeypatch, fake_archive)
    monkeypatch.setattr(plugin, "_archive_enabled", lambda: False)
    await plugin._check_account(UID)
    assert archived == []

    plugin.pending.clear()
    plugin.accounts[UID]["seen_ids"] = []
    monkeypatch.setattr(plugin, "_archive_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_archive_bool", lambda key, default: False)
    await plugin._check_account(UID)
    assert archived == []
    assert len(plugin.pending) == 1


# ---------------------------------------------------------------- album 本地优先


def _pic(pid="p1", kind="photo") -> dict:
    return {"pid": pid, "url": f"https://wx1.sinaimg.cn/large/{pid}.jpg", "kind": kind}


async def test_album_local_hit_skips_download(make_weibo_plugin, data_dir, monkeypatch):
    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    local = data_dir / "archive" / UID / "m1" / "p1.jpg"
    downloads = []

    async def fake_download(pic, path):
        downloads.append(path)

    monkeypatch.setattr(plugin, "_album_tmp_root", lambda: data_dir)
    monkeypatch.setattr(plugin, "_download_pic", fake_download)
    monkeypatch.setattr(plugin, "archive_local_media", lambda mid, pid: local)
    files = await plugin._download_pics([_pic()], data_dir, post_id="m1")
    assert downloads == []
    assert files == [(_pic(), local)]


async def test_album_without_post_id_uses_remote(make_weibo_plugin, data_dir, monkeypatch):
    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    downloads = []
    lookups = []

    async def fake_download(pic, path):
        downloads.append(path)

    monkeypatch.setattr(plugin, "_album_tmp_root", lambda: data_dir)
    monkeypatch.setattr(plugin, "_download_pic", fake_download)
    monkeypatch.setattr(plugin, "archive_local_media", lambda mid, pid: lookups.append(mid))
    files = await plugin._download_pics([_pic()], data_dir)
    assert lookups == []
    assert len(downloads) == 1
    assert files and files[0][1].name == "p1.jpg"


async def test_album_live_pic_still_downloads_even_with_local_hit(
    make_weibo_plugin, data_dir, monkeypatch
):
    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    local = data_dir / "archive" / UID / "m1" / "p1.jpg"
    downloads = []

    async def fake_download(pic, path):
        downloads.append(path)

    monkeypatch.setattr(plugin, "_album_tmp_root", lambda: data_dir)
    monkeypatch.setattr(plugin, "_download_pic", fake_download)
    monkeypatch.setattr(plugin, "archive_local_media", lambda mid, pid: local)
    # _ffmpeg 为空时 want_gif=False，live 图按静图走下载而不是复用本地
    files = await plugin._download_pics([_pic(kind="live")], data_dir, post_id="m1")
    assert len(downloads) == 1
    assert files and files[0][1].name == "p1.jpg"
