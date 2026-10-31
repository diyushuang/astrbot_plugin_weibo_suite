"""归档回填（archive_backfill / archive_enrich）离线用例。

全部走 FakePlugin 桩：时间线分页、长文补全都用内存假数据，不联网、不 sleep 长延时。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive import ArchiveMixin
from astrbot_plugin_weibo_suite.weibo.parser import WeiboFetchError, WeiboRiskError

UID = "1000000001"


class FakePlugin(ArchiveMixin):
    """只提供 archive 编排需要的最小宿主；不继承真实插件，避免依赖在线初始化。"""

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
        self.pages = {}
        self.on_fetch = None
        self.fetch_calls = []
        self.enrich_calls = []
        self.longtext_calls = []
        self.full_text = ""

    async def _fetch_timeline_page(self, uid, page):
        self.fetch_calls.append((uid, page))
        if self.on_fetch is not None:
            self.on_fetch(page)
        item = self.pages.get(page, [])
        if isinstance(item, Exception):
            raise item
        return [dict(p) for p in item]

    async def _ensure_full_pics(self, post):
        self.enrich_calls.append(("pics", str(post.get("id") or "")))

    async def _api_get(self, url, params, referer=None):
        # 生产环境 visitor 白名单不含 weibo.com，首选接口必然先失败。
        raise WeiboFetchError("目标 host 不在白名单内: weibo.com")

    async def _fetch_long_text(self, mid):
        self.longtext_calls.append(str(mid))
        return self.full_text or None


def make_post(
    mid,
    *,
    uid=UID,
    created_ts=1760000000,
    text="正文",
    pics=(),
    pic_total=None,
    is_long=False,
):
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
        "is_long": is_long,
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


async def test_backfill_paginates_until_end_and_clears_cursor(fake):
    fake.pages = {
        1: [make_post("101", created_ts=1760000000), make_post("102", created_ts=1759990000)],
        2: [make_post("103", created_ts=1759980000)],
    }

    stats = await fake.archive_backfill(UID)

    assert [page for _uid, page in fake.fetch_calls] == [1, 2, 3]
    assert stats["status"] == "done"
    assert stats["stopped"] == "end"
    assert stats["pages"] == 3
    assert stats["fetched"] == 3
    assert stats["inserted"] == 3
    assert stats["existing"] == 0
    assert fake.archive_backfill_running() is False
    store = fake.archive_store()
    assert store.count_posts(UID) == 3
    assert store.get_cursor(UID) is None
    assert fake.format_backfill(stats).startswith(f"回填 {UID}：完成（翻到底）")


async def test_backfill_resumes_from_saved_cursor(fake):
    store = fake.archive_store()
    store.set_cursor(UID, 5, 1759900000, "running")
    fake.pages = {5: [make_post("201")], 6: []}

    stats = await fake.archive_backfill(UID)

    assert [page for _uid, page in fake.fetch_calls] == [5, 6]
    assert stats["inserted"] == 1
    assert stats["stopped"] == "end"
    # 游标只在页处理完后推进，翻到底才清除
    assert store.get_cursor(UID) is None
    assert store.count_posts(UID) == 1


async def test_backfill_stops_at_since_boundary(fake):
    store = fake.archive_store()
    fake.pages = {
        1: [make_post("301", created_ts=1767200000), make_post("302", created_ts=1700000000)],
    }

    stats = await fake.archive_backfill(UID, since="2026-01-01")

    assert stats["skipped_since"] == 1
    assert stats["inserted"] == 1
    assert stats["status"] == "done"
    assert stats["stopped"] == "since"
    assert store.count_posts(UID) == 1
    assert store.get_cursor(UID) is None


async def test_backfill_pauses_on_risk_and_keeps_cursor(fake):
    store = fake.archive_store()
    fake.pages = {1: [make_post("401")], 2: WeiboRiskError("HTTP 432 全局冷却中")}

    stats = await fake.archive_backfill(UID)

    assert stats["status"] == "paused"
    assert stats["stopped"] == "risk"
    assert stats["next_page"] == 2
    assert stats["inserted"] == 1
    cursor = store.get_cursor(UID)
    assert cursor is not None
    assert cursor["next_page"] == 2
    assert cursor["status"] == "paused"


async def test_backfill_marks_error_on_fetch_failure(fake):
    store = fake.archive_store()
    fake.pages = {1: WeiboFetchError("接口 500")}

    stats = await fake.archive_backfill(UID)

    assert stats["status"] == "error"
    assert stats["stopped"] == "error"
    assert stats["pages"] == 0
    assert stats["next_page"] == 1
    cursor = store.get_cursor(UID)
    assert cursor["status"] == "error"
    assert cursor["next_page"] == 1


async def test_backfill_stops_when_requested(fake):
    fake.pages = {1: [make_post("501")], 2: [make_post("502")], 3: [make_post("503")]}
    fake.on_fetch = lambda page: fake.archive_backfill_stop() if page == 2 else None

    stats = await fake.archive_backfill(UID)

    assert stats["status"] == "paused"
    assert stats["stopped"] == "paused"
    assert [page for _uid, page in fake.fetch_calls] == [1, 2]
    # 当前页处理完才停：游标停在下一页，之后可以续跑
    assert fake.archive_store().get_cursor(UID)["next_page"] == 3


async def test_backfill_rerun_is_idempotent(fake):
    fake.pages = {1: [make_post("601"), make_post("602")]}

    first = await fake.archive_backfill(UID)
    second = await fake.archive_backfill(UID)

    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert second["existing"] == 2
    assert fake.archive_store().count_posts(UID) == 2
    # 已有帖不重复补全（enrich 只在新增时跑）
    assert fake.enrich_calls == []


async def test_backfill_full_mode_enriches_every_post(fake):
    fake.pages = {1: [make_post("611", text="短正文", is_long=True)]}
    fake.full_text = "展开后的完整长文"

    await fake.archive_backfill(UID, full=True)

    assert fake.longtext_calls == ["611"]
    post = fake.archive_store().get_post("611")
    assert post["text"] == "展开后的完整长文"


async def test_archive_enrich_only_when_needed(fake):
    fake.full_text = "更长" * 300
    truncated = make_post(
        "701", text="截断" * 80, is_long=True, pics=[make_pic("p1")], pic_total=3
    )

    out = await fake.archive_enrich(truncated)

    assert fake.longtext_calls == ["701"]
    assert out["text"] == fake.full_text
    assert out["text_full"] == 1
    assert out["pic_total"] == 3
    assert fake.enrich_calls == [("pics", "701")]

    untouched = await fake.archive_enrich(make_post("702", text="短", is_long=False))
    assert untouched["text"] == "短"
    assert fake.longtext_calls == ["701"]
    assert fake.enrich_calls == [("pics", "701")]


async def test_backfill_rejects_bad_uid_and_unready_store(data_dir):
    plugin = FakePlugin(data_dir)

    assert await plugin.archive_backfill("abc") == {"error": "uid 不合法"}
    assert await plugin.archive_backfill(UID) == {"error": "归档未启用"}
    assert await plugin.archive_post(make_post("801")) is False


async def test_archive_open_close_are_idempotent(data_dir):
    plugin = FakePlugin(data_dir)

    await plugin.archive_open()
    task = plugin._archive_worker_task
    await plugin.archive_open()

    assert plugin._archive_worker_task is task
    assert plugin.archive_ready() is True
    assert (data_dir / "archive.db").is_file()
    assert plugin.archive_enqueue_download("123") is True

    await plugin.archive_close()
    await plugin.archive_close()

    assert plugin.archive_ready() is False
    assert plugin._archive_worker_task is None
    assert plugin.archive_enqueue_download("123") is False
