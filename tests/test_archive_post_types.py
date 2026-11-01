"""P3 验收：四类真实形态帖子（长文 / 多图 / 转发 / 视频）的归档落账。

这组用例不联网、不依赖 astrbot：只验证「一条帖子进来 → posts/media 两表
应有的字段都在」这一层，对应开发计划 §三 F1 的验收项「四类真实帖子各归档
一条」。下载与状态推进由 tests/test_archive_db.py、test_archive_backfill.py 覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive import ArchiveMixin

MID_LONG = "5100000000000001"
MID_ALBUM = "5100000000000002"
MID_RETWEET = "5100000000000003"
MID_VIDEO = "5100000000000004"
UID = "1000000001"


class _ArchivePlugin(ArchiveMixin):
    """只带归档能力的最小宿主：不联网、不下载。"""

    def __init__(self, base: Path, config: dict | None = None) -> None:
        self._data_dir = base
        self.config = config or {"archive": {"enabled": True, "download_originals": False}}
        self._archive = None


@pytest.fixture
async def host(tmp_path):
    plugin = _ArchivePlugin(tmp_path)
    await plugin.archive_open()
    assert plugin.archive_ready()
    try:
        yield plugin
    finally:
        await plugin.archive_close()


def _post(mid: str, **overrides) -> dict:
    post = {
        "id": mid,
        "uid": UID,
        "author": "示例博主A",
        "link": f"https://weibo.com/{UID}/abcdefg{mid[-1]}",
        "created_ts": 1761000000,
        "created_raw": "Wed Oct 21 11:00:58 +0800 2026",
        "text": "正文",
        "raw_text": "正文",
        "pic_total": 0,
        "pics": [],
        "is_long": False,
        "is_retweet": False,
    }
    post.update(overrides)
    return post


async def test_long_post_stores_full_text(host):
    """长文帖：archive_enrich 补到 longTextContent_raw 后，整篇进 text。"""
    full_text = "长文第一段。" * 200

    async def fake_long_text(mid: str) -> str | None:
        assert mid == MID_LONG
        return full_text

    host._archive_long_text = fake_long_text  # type: ignore[method-assign]
    post = _post(
        MID_LONG,
        is_long=True,
        text="长文开头…" + "x" * 140,
        raw_text="长文开头…" + "x" * 140 + " 全文",
    )
    enriched = await host.archive_enrich(post)
    assert enriched["text"] == full_text

    assert await host.archive_post(enriched, source="monitor") is True
    row = host.archive_store().get_post(MID_LONG)
    assert row["is_long"] == 1
    assert row["text"] == full_text
    assert len(row["text"]) == len(full_text)


async def test_album_post_records_every_picture(host):
    """18 图帖：pic_total 与 media 行数都对得上，状态全部停在 pending。"""
    pics = [
        {
            "pid": f"00{i:06d}",
            "url": f"https://wx1.sinaimg.cn/large/00{i:06d}.jpg",
            "alt_url": f"https://wx2.sinaimg.cn/large/00{i:06d}.jpg",
            "ext": "jpg",
        }
        for i in range(18)
    ]
    post = _post(MID_ALBUM, pic_total=18, pics=pics)
    assert await host.archive_post(post) is True

    store = host.archive_store()
    row = store.get_post(MID_ALBUM)
    assert row["pic_total"] == 18
    assert len(row["pics"]) == 18
    media = store.list_media(MID_ALBUM)
    assert len(media) == 18
    assert {item["pid"] for item in media} == {pic["pid"] for pic in pics}
    assert all(item["state"] == "pending" for item in media)
    assert all(item["url"].endswith(".jpg") for item in media)
    stats = store.media_stats()
    assert stats["total"] == 18
    assert stats["downloaded"] == 0


async def test_retweet_post_keeps_original_text(host):
    """转发帖：is_retweet 与转发原文都要留档，正文与原文不混淆。"""
    post = _post(
        MID_RETWEET,
        is_retweet=True,
        text="//@原作者：说得好",
        raw_text="//@原作者：说得好",
        orig_text="这是一条被转发的原文",
        orig_name="原作者",
    )
    assert await host.archive_post(post) is True

    row = host.archive_store().get_post(MID_RETWEET)
    assert row["is_retweet"] == 1
    assert row["orig_text"] == "这是一条被转发的原文"
    assert row["text"] == "//@原作者：说得好"


async def test_video_post_stores_stream_url(host):
    """视频帖：直链落 video_src，图和视频可以同时存在。"""
    pic = {"pid": "cover1", "url": "https://wx1.sinaimg.cn/large/cover1.jpg", "ext": "jpg"}
    video = "https://f.video.weibocdn.com/o0/abc123.mp4?label=mp4_720p"
    post = _post(MID_VIDEO, text="看视频", video_src=video, pic_total=1, pics=[pic])
    assert await host.archive_post(post) is True

    row = host.archive_store().get_post(MID_VIDEO)
    assert row["video_src"] == video
    assert len(row["pics"]) == 1


async def test_same_mid_rearchived_keeps_media_state(host):
    """同一条微博重复归档（监控命中 + 回填撞车）不重置状态、不重复媒体行。"""
    pics = [{"pid": "p1", "url": "https://wx1.sinaimg.cn/large/p1.jpg", "ext": "jpg"}]
    post = _post(MID_ALBUM, pic_total=1, pics=pics)
    assert await host.archive_post(post) is True

    store = host.archive_store()
    local_path = f"{UID}/{MID_ALBUM}/p1.jpg"
    store.mark_media(MID_ALBUM, "p1", "downloaded", local_path=local_path, size=123)
    assert await host.archive_post(post, source="backfill") is True

    media = store.list_media(MID_ALBUM)
    assert len(media) == 1
    assert media[0]["state"] == "downloaded"
    assert store.get_post(MID_ALBUM)["source"] == "monitor"


def test_post_row_accepts_monitor_shape():
    """monitor._parse_post 的字段名（id/created_ts/raw_text）能直接映射到归档列。"""
    post = _post(MID_LONG, is_long=True, text="短", raw_text="短", pic_total=3)
    row = ArchiveMixin._archive_post_row(post, MID_LONG, UID, "monitor")
    assert row["mid"] == MID_LONG
    assert row["uid"] == UID
    assert row["author"] == "示例博主A"
    assert row["created_at"] == 1761000000
    assert row["created_raw"] == "Wed Oct 21 11:00:58 +0800 2026"
    assert row["is_long"] == 1
    assert row["pic_total"] == 3
    assert row["source"] == "monitor"
    assert '"id": "' + MID_LONG in row["raw_json"]
