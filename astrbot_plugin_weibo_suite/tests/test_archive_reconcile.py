"""归档对账 / 原图下载 / 查询的离线用例。

FakePlugin 自带 _download_pic 桩：不联网，用字节串伪造落盘，并记录并发度以验证串行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive import ArchiveMixin
from astrbot_plugin_weibo_suite.weibo.parser import WeiboFetchError

UID = "1000000001"


class FakePlugin(ArchiveMixin):
    def __init__(self, data_dir, **archive_cfg) -> None:
        config = {"enabled": True, "download_originals": False, "page_delay": 0}
        config.update(archive_cfg)
        self.config = {"archive": config}
        self._data_dir = Path(data_dir)
        self._archive = None
        self._archive_dl_queue = None
        self._archive_worker_task = None
        self._backfill_stop = False
        self._backfill_task = None
        self._backfill_active = False
        self.fail_pids = set()
        self.download_calls = []
        self.max_active = 0
        self._active = 0

    async def _download_pic(self, pic, dest):
        pid = str(pic.get("pid") or "")
        self.download_calls.append(pid)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            await asyncio.sleep(0.01)
            if pid in self.fail_pids:
                raise WeiboFetchError(f"下载失败: {pid}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * 2048)
            return dest
        finally:
            self._active -= 1


def make_post(mid, *, uid=UID, created_ts=1760000000, text="正文", pics=(), pic_total=None):
    pics = list(pics)
    return {
        "id": mid,
        "uid": uid,
        "author": "作者",
        "link": f"https://weibo.com/{uid}/{mid}",
        "time": "Sat Mar 08 16:51:30 +0800 2025",
        "created_ts": created_ts,
        "text": text,
        "is_retweet": False,
        "orig_text": "",
        "is_long": False,
        "pics": pics,
        "pic_total": len(pics) if pic_total is None else pic_total,
        "is_video": False,
        "video_src": "",
    }


def make_pic(pid, ext="jpg"):
    return {
        "pid": pid,
        "url": f"https://wx1.sinaimg.cn/large/{pid}.{ext}",
        "alt_url": "",
        "ext": ext,
        "kind": "pic",
        "video_url": "",
    }


@pytest.fixture
async def fake(data_dir):
    plugin = FakePlugin(data_dir)
    await plugin.archive_open()
    try:
        yield plugin
    finally:
        await plugin.archive_close()


def landed_file(plugin, mid, pid, ext="jpg") -> Path:
    dest = plugin._archive_media_root() / UID / mid / f"{pid}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"y" * 2048)
    return dest


async def test_reconcile_detects_gaps_and_formats_report(fake):
    await fake.archive_post(make_post("5001", text="", pics=[make_pic("p1")], pic_total=3))
    await fake.archive_post(make_post("5002", text="有正文"))

    report = await fake.archive_reconcile()

    assert report["uid"] == ""
    assert report["posts"] == 2
    assert report["posts_with_text"] == 1
    assert report["posts_without_text"] == 1
    assert report["posts_with_media"] == 1
    assert report["media_expected"] == 3
    assert report["media_rows"] == 1
    assert report["ok"] is False
    assert report["gap_counts"] == {
        "post_no_text": 1,
        "pic_count_mismatch": 1,
        "media_not_downloaded": 1,
    }
    gaps = report["gaps"]
    assert [gap["kind"] for gap in gaps] == [
        "post_no_text",
        "pic_count_mismatch",
        "media_not_downloaded",
    ]
    assert gaps[0]["detail"] == "正文为空"
    assert gaps[1]["detail"] == "自述 3 张，实际 1 张"
    assert gaps[2]["pid"] == "p1"
    assert gaps[2]["detail"] == "原图未落盘"

    text = fake.format_reconcile(report)
    assert text.startswith("对账（全库）：2 帖 / 有正文 1 / 缺正文 1")
    assert "缺口·缺正文：1 处" in text
    assert "缺口·图数不符：1 处" in text
    assert "缺口·原图未落盘：1 处" in text
    assert text.endswith("结论：存在 3 处缺口")


async def test_reconcile_clean_store_reports_consistent(fake):
    await fake.archive_post(make_post("5101", pics=[make_pic("p1")], pic_total=1))
    store = fake.archive_store()
    dest = landed_file(fake, "5101", "p1")
    store.mark_media("5101", "p1", "downloaded", local_path=str(dest), size=2048)

    report = await fake.archive_reconcile(UID)

    assert report["uid"] == UID
    assert report["posts"] == 1
    assert report["downloaded"] == 1
    assert report["gaps"] == []
    assert report["ok"] is True
    assert not any(report["gap_counts"].values())
    text = fake.format_reconcile(report)
    assert text.startswith(f"对账（{UID}）：1 帖")
    assert text.endswith("结论：一致")


async def test_reconcile_reports_error_when_store_not_ready(data_dir):
    plugin = FakePlugin(data_dir)

    report = await plugin.archive_reconcile()
    assert report["error"] == "归档未启用"
    assert report["ok"] is False
    assert FakePlugin.format_reconcile(report) == "对账失败：归档未启用"


async def test_download_worker_is_serial_and_bumps_failures(data_dir):
    plugin = FakePlugin(data_dir, download_originals=True)
    plugin.fail_pids = {"p2"}
    await plugin.archive_open()
    try:
        await plugin.archive_post(make_post("5201", pics=[make_pic("p1"), make_pic("p2")]))
        await plugin.archive_post(make_post("5202", pics=[make_pic("p3")]))
        await asyncio.wait_for(plugin._archive_dl_queue.join(), timeout=10)

        store = plugin.archive_store()
        # 单 worker 串行：并发度始终 1，且按入队顺序处理
        assert plugin.max_active == 1
        assert plugin.download_calls == ["p1", "p2", "p3"]
        p1 = store.get_media("5201", "p1")
        assert p1["state"] == "downloaded"
        assert p1["size"] == 2048
        assert Path(p1["local_path"]).is_file()
        failed = store.get_media("5201", "p2")
        assert failed["state"] == "pending"
        assert failed["attempts"] == 1
        assert failed["error"]
        assert store.get_media("5202", "p3")["state"] == "downloaded"
    finally:
        await plugin.archive_close()


async def test_archive_download_post_counts_and_retries(fake):
    await fake.archive_post(make_post("5301", pics=[make_pic("p1"), make_pic("p2")]))
    fake.fail_pids = {"p2"}

    first = await fake.archive_download_post("5301")
    second = await fake.archive_download_post("5301")

    assert first == {"mid": "5301", "downloaded": 1, "failed": 1}
    # 已落盘的 p1 被跳过，只有 p2 再失败一次
    assert second == {"mid": "5301", "downloaded": 0, "failed": 1}
    store = fake.archive_store()
    assert store.get_media("5301", "p1")["state"] == "downloaded"
    assert store.get_media("5301", "p2")["attempts"] == 2
    assert store.media_stats(UID)["failed"] == 1

    assert await fake.archive_download_post("") == {"mid": "", "downloaded": 0, "failed": 0}


async def test_archive_post_idempotent_keeps_media_state(fake):
    post = make_post("5401", pics=[make_pic("p1")])
    assert await fake.archive_post(post) is True
    store = fake.archive_store()
    dest = landed_file(fake, "5401", "p1")
    store.mark_media("5401", "p1", "album", local_path=str(dest), size=2048)

    assert await fake.archive_post(post) is True

    row = store.get_media("5401", "p1")
    assert row["state"] == "album"
    assert row["local_path"] == str(dest)
    assert store.count_posts(UID) == 1
    assert len(store.list_media("5401")) == 1
    assert fake.archive_local_media("5401", "p1") == dest
    assert fake.archive_local_media("9999", "p1") is None


async def test_archive_query_orders_and_paginates(fake):
    for mid, created in (("5501", 1700000000), ("5502", 1800000000), ("5503", 1750000000)):
        await fake.archive_post(make_post(mid, created_ts=created, text=f"帖子{mid}"))

    newest = await fake.archive_query(UID, limit=2)
    older = await fake.archive_query(UID, limit=2, offset=2)

    assert [row["mid"] for row in newest] == ["5502", "5503"]
    assert [row["mid"] for row in older] == ["5501"]
    assert [row["text"] for row in newest] == ["帖子5502", "帖子5503"]


async def test_archive_query_degrades_when_not_ready(data_dir):
    assert await FakePlugin(data_dir).archive_query() == []


def test_format_backfill_and_format_import():
    stats = {
        "uid": UID,
        "pages": 3,
        "fetched": 10,
        "inserted": 8,
        "existing": 2,
        "skipped_since": 0,
        "downloaded_queued": 4,
        "stopped": "risk",
        "status": "paused",
        "next_page": 7,
        "elapsed": 12.5,
    }
    text = FakePlugin.format_backfill(stats)
    assert text.startswith(f"回填 {UID}：已暂停（触发风控暂停）")
    assert "下一页 7" in text
    assert "新增 8" in text
    assert FakePlugin.format_backfill({"error": "归档未启用"}) == "回填失败：归档未启用"

    imported = {
        "file": "x.json",
        "uid": UID,
        "total": 5,
        "inserted": 4,
        "updated": 1,
        "skipped": 0,
        "media_rows": 2,
    }
    line = FakePlugin.format_import(imported)
    assert line.startswith(f"导入 x.json：uid {UID} / 共 5 条 / 新增 4 / 更新 1")
    assert "图片行 2" in line
    missing = {"error": "文件不存在: a.json"}
    assert FakePlugin.format_import(missing) == "导入失败：文件不存在: a.json"
    assert FakePlugin.format_import(None) == "导入：无数据"
