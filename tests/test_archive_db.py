"""ArchiveStore 单测：表结构、幂等写入、状态机、游标、聚合与并发。

全部离线（tmp_path），不依赖 astrbot 运行时；覆盖合同第 9(A) 节清单。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive_db import (
    MEDIA_STATES,
    STATE_AT_COLUMN,
    ArchiveStore,
)


@pytest.fixture
def store(tmp_path: Path):
    instance = ArchiveStore(tmp_path / "archive.db", tmp_path / "archive")
    try:
        yield instance
    finally:
        instance.close()


def make_post(mid: str = "1001", uid: str = "42", **overrides):
    post = {
        "mid": mid,
        "uid": uid,
        "author": "作者",
        "bid": f"B{mid}",
        "link": f"https://weibo.com/{uid}/{mid}",
        "created_at": 1_700_000_000,
        "created_raw": "Sat Mar 08 16:51:30 +0800 2025",
        "text": "正文",
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
                "kind": "pic",
            }
        ],
        "video_src": "",
        "raw_json": "{}",
        "source": "monitor",
    }
    post.update(overrides)
    return post


def is_utc_iso(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return False
    return True


def test_schema_tables_indexes_and_connection_pragmas(store, tmp_path):
    assert store.meta_get("schema_version") == "2"
    assert store.schema_version == 2
    post_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(posts)").fetchall()}
    media_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(media)").fetchall()}
    assert "cleaned_text" in post_columns
    assert "thumb_url" in media_columns
    assert store.media_root == tmp_path / "archive"
    assert MEDIA_STATES == ("pending", "downloaded", "album", "bed", "thumb", "vector")

    import sqlite3

    connection = sqlite3.connect(tmp_path / "archive.db")
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert {"meta", "posts", "media", "backfill_cursor"} <= tables
    assert {
        "idx_posts_uid_created",
        "idx_posts_created",
        "idx_media_mid",
        "idx_media_state",
    } <= indexes
    assert str(journal_mode).lower() == "wal"
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_higher_schema_version_is_not_downgraded(tmp_path):
    import sqlite3

    db_path = tmp_path / "archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '3')")
        connection.commit()
    finally:
        connection.close()

    store = ArchiveStore(db_path)
    try:
        assert store.meta_get("schema_version") == "3"
        assert store.schema_version == 3
        assert store.get_post("missing") is None
    finally:
        store.close()


def test_upsert_post_roundtrip_with_parsed_pics(store):
    assert store.upsert_post(make_post()) == "1001"
    row = store.get_post("1001")
    assert row is not None
    assert row["mid"] == "1001"
    assert row["uid"] == "42"
    assert row["author"] == "作者"
    assert row["text"] == "正文"
    assert row["text_raw"] == "<p>正文</p>"
    assert row["created_at"] == 1_700_000_000
    assert row["source"] == "monitor"
    assert row["video_src"] == ""
    assert row["pics"] == [
        {
            "pid": "p1001",
            "url": "https://wx1.sinaimg.cn/large/a.jpg",
            "alt_url": "",
            "ext": "jpg",
            "kind": "pic",
        }
    ]
    assert isinstance(row["pics_json"], str)


def test_upsert_post_is_idempotent_and_only_fills_non_empty(store):
    store.upsert_post(make_post(source="monitor"))
    store.upsert_post(
        make_post(
            source="json",
            author="",
            text="新正文",
            created_at=1_600_000_000,
            pic_total=5,
            pics=[],
            raw_json="",
        )
    )
    assert store.count_posts() == 1
    row = store.get_post("1001")
    assert row["author"] == "作者"  # 空串不覆盖
    assert row["text"] == "新正文"  # 非空覆盖
    assert row["created_at"] == 1_700_000_000  # 取 max
    assert row["pic_total"] == 5  # 非 -1 覆盖
    assert row["pics"][0]["pid"] == "p1001"  # 空 pics 不抹掉旧图
    assert row["raw_json"] == "{}"  # 空串不覆盖
    assert row["source"] == "monitor"  # 首见来源保留


def test_upsert_post_accepts_monitor_style_keys(store):
    store.upsert_post(
        {
            "id": "5001",
            "uid": "77",
            "author": "某人",
            "time": "03-08 16:51",
            "created_ts": 1_700_000_123.0,
            "text": "hi",
            "raw_text": "<p>hi</p>",
            "is_long": True,
            "is_retweet": False,
            "pics": [{"pid": "x", "url": "u", "kind": "pic"}],
            "pic_total": 2,
            "video_src": [{"page_info": {"type": "video"}}],
        }
    )
    row = store.get_post("5001")
    assert row["uid"] == "77"
    assert row["created_at"] == 1_700_000_123
    assert row["created_raw"] == "03-08 16:51"
    assert row["text_raw"] == "<p>hi</p>"
    assert row["is_long"] == 1
    assert row["is_retweet"] == 0
    assert row["pic_total"] == 2
    assert row["video_src"] == ""  # 非字符串的视频结构不入库
    assert row["pics"][0]["pid"] == "x"


@pytest.mark.parametrize(
    "post",
    [{"uid": "1"}, {"mid": "1"}, {"mid": "1", "uid": ""}],
)
def test_upsert_post_requires_mid_and_uid(store, post):
    with pytest.raises(ValueError):
        store.upsert_post(post)


def test_count_and_list_posts_filter_and_order(store):
    store.upsert_post(make_post("a", "1", created_at=100))
    store.upsert_post(make_post("b", "1", created_at=300))
    store.upsert_post(make_post("c", "2", created_at=200, source="json"))

    assert store.count_posts() == 3
    assert store.count_posts("1") == 2
    assert store.count_posts(source="json") == 1
    assert store.count_posts("1", source="json") == 0
    assert store.has_post("a") is True
    assert store.has_post("zzz") is False

    assert [row["mid"] for row in store.list_posts("1")] == ["b", "a"]
    page = store.list_posts(limit=1, offset=1)
    assert len(page) == 1
    assert page[0]["mid"] == "c"
    assert page[0]["pics"]  # 列表结果同样带解析后的 pics


def test_pending_text_posts_and_post_time_columns(store):
    store.upsert_post(make_post("p1"))
    store.upsert_post(make_post("p2"))
    assert {row["mid"] for row in store.iter_pending_text_posts("42")} == {"p1", "p2"}

    store.set_post_time("p1", "cleaned_at", "2026-01-01T00:00:00Z")
    assert [row["mid"] for row in store.iter_pending_text_posts()] == ["p2"]
    store.set_post_time("p2", "pushed_at", "2026-01-02T00:00:00Z")
    store.set_post_time("p2", "reconciled_at", "2026-01-03T00:00:00Z")
    row = store.get_post("p2")
    assert row["pushed_at"] == "2026-01-02T00:00:00Z"
    assert row["reconciled_at"] == "2026-01-03T00:00:00Z"

    with pytest.raises(ValueError):
        store.set_post_time("p1", "source", "x")
    store.set_post_time("missing", "pushed_at", "x")  # 不存在的 mid 安全空操作
    assert store.get_post("p1")["pushed_at"] == ""


def test_set_pics_rewrites_json_and_total(store):
    store.upsert_post(make_post())
    store.set_pics("1001", [{"pid": "x", "url": "u"}, {"pid": "y", "url": "u2"}])
    row = store.get_post("1001")
    assert [pic["pid"] for pic in row["pics"]] == ["x", "y"]
    assert row["pic_total"] == 2

    store.set_pics("1001", [])
    row = store.get_post("1001")
    assert row["pics"] == []
    assert row["pic_total"] == 0


def test_upsert_media_dedupes_and_preserves_download_state(store):
    store.upsert_post(make_post())
    inserted = store.upsert_media(
        "1001",
        [
            {"pid": "p1", "url": "u1", "alt_url": "a1", "ext": "jpg", "kind": "pic", "idx": 0},
            {"pid": "p2", "url": "u2", "alt_url": "", "ext": "png", "kind": "pic"},
            {"pid": "", "url": "ignored"},
        ],
    )
    assert inserted == 2
    rows = store.list_media("1001")
    assert [row["pid"] for row in rows] == ["p1", "p2"]
    assert rows[1]["idx"] == 1  # 缺 idx 时用位置
    assert rows[1]["state"] == "pending"
    assert rows[1]["state_rank"] == 0
    assert rows[0]["ext"] == "jpg"

    store.mark_media("1001", "p1", "downloaded", local_path="L", size=9)
    before = store.get_media("1001", "p1")
    assert store.upsert_media("1001", [{"pid": "p1", "url": "u1b", "kind": "live"}]) == 0
    after = store.get_media("1001", "p1")
    assert after["url"] == "u1b"  # 非空新值补齐
    assert after["kind"] == "live"
    assert after["state"] == "downloaded"
    assert after["local_path"] == "L"
    assert after["size"] == 9
    assert after["downloaded_at"] == before["downloaded_at"]

    store.upsert_media("1001", [{"pid": "p1", "url": "", "alt_url": ""}])
    assert store.get_media("1001", "p1")["url"] == "u1b"  # 空串不覆盖


def test_mark_media_is_monotonic_and_writes_timestamps(store):
    store.upsert_post(make_post())
    store.upsert_media("1001", [{"pid": "p1", "url": "u1"}])

    store.mark_media("1001", "p1", "downloaded", local_path="L1", size=10)
    row = store.get_media("1001", "p1")
    assert row["state"] == "downloaded"
    assert row["state_rank"] == 1
    assert is_utc_iso(row["downloaded_at"])

    store.mark_media("1001", "p1", "album")
    row = store.get_media("1001", "p1")
    assert row["state"] == "album"
    assert row["state_rank"] == 2
    assert is_utc_iso(row["album_at"])
    assert row["local_path"] == "L1"  # 未传就不动

    store.mark_media("1001", "p1", "pending", local_path="L2", error="重试")
    row = store.get_media("1001", "p1")
    assert row["state"] == "album"  # 不降级
    assert row["state_rank"] == 2
    assert row["local_path"] == "L2"  # 但仍写 local_path
    assert row["error"] == "重试"

    with pytest.raises(ValueError):
        store.mark_media("1001", "p1", "bogus")
    store.mark_media("missing", "p1", "downloaded")  # 行不存在安全空操作
    assert STATE_AT_COLUMN == {
        "downloaded": "downloaded_at",
        "album": "album_at",
        "bed": "bed_at",
        "thumb": "thumb_at",
        "vector": "vector_at",
    }


def test_bump_media_attempt_counts_and_records_error(store):
    store.upsert_post(make_post())
    store.upsert_media("1001", [{"pid": "p1", "url": "u1"}])

    assert store.bump_media_attempt("1001", "p1", "boom") == 1
    row = store.get_media("1001", "p1")
    assert row["attempts"] == 1
    assert row["error"] == "boom"
    assert row["state"] == "pending"

    assert store.bump_media_attempt("1001", "p1") == 2
    assert store.get_media("1001", "p1")["error"] == ""
    assert store.bump_media_attempt("missing", "p1") == 0


def test_media_stats_counts_at_or_above_rank(store):
    store.upsert_post(make_post("m1", uid="1"))
    store.upsert_post(make_post("m2", uid="2"))
    store.upsert_media("m1", [{"pid": "a", "url": "u"}, {"pid": "b", "url": "u"}])
    store.upsert_media("m1", [{"pid": "d", "url": "u"}])
    store.upsert_media("m2", [{"pid": "c", "url": "u"}])

    store.mark_media("m1", "a", "bed")
    store.mark_media("m1", "b", "downloaded")
    store.mark_media("m2", "c", "vector")
    store.bump_media_attempt("m1", "d", "失败")

    stats = store.media_stats()
    assert stats == {
        "total": 4,
        "pending": 4,
        "downloaded": 3,
        "album": 2,
        "bed": 2,
        "thumb": 1,
        "vector": 1,
        "failed": 1,
    }
    first = store.media_stats("1")
    assert first["total"] == 3
    assert first["album"] == 1
    assert first["vector"] == 0
    assert first["failed"] == 1
    assert store.media_stats("2")["total"] == 1
    assert store.media_stats("nobody")["total"] == 0


def test_media_file_resolves_relative_and_absolute_paths(store, tmp_path):
    target = tmp_path / "archive" / "42" / "1001" / "p1.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"img")

    store.upsert_post(make_post())
    store.upsert_media("1001", [{"pid": "p1", "url": "u"}])
    assert store.media_file("1001", "p1") is None  # local_path 为空

    store.mark_media("1001", "p1", "downloaded", local_path="42/1001/p1.jpg")
    assert store.media_file("1001", "p1") == target  # 相对路径按媒体根解析

    store.mark_media("1001", "p1", "downloaded", local_path=str(target))
    assert store.media_file("1001", "p1") == target  # 绝对路径原样

    store.mark_media("1001", "p1", "downloaded", local_path=str(target.parent / "gone.jpg"))
    assert store.media_file("1001", "p1") is None


def test_cursor_roundtrip_and_clear(store):
    assert store.get_cursor("42") is None
    store.set_cursor("42", 2, 1_700_000_000, "running")
    cursor = store.get_cursor("42")
    assert cursor["next_page"] == 2
    assert cursor["oldest_created_at"] == 1_700_000_000
    assert cursor["status"] == "running"
    assert is_utc_iso(cursor["updated_at"])

    store.set_cursor("42", 3, 0, "paused")
    cursor = store.get_cursor("42")
    assert cursor["next_page"] == 3
    assert cursor["oldest_created_at"] == 1_700_000_000  # 0 不覆盖已知值
    assert cursor["status"] == "paused"

    store.clear_cursor("42")
    assert store.get_cursor("42") is None


def test_reconcile_counts_aggregates(store):
    store.upsert_post(make_post("r1", uid="1", text="有正文", pic_total=3))
    store.upsert_post(make_post("r2", uid="1", text="", pic_total=2, pics=[]))
    store.upsert_post(make_post("r3", uid="2", text="x", pic_total=-1, pics=[]))
    store.upsert_media("r1", [{"pid": "a", "url": "u"}, {"pid": "b", "url": ""}])
    store.upsert_media("r2", [{"pid": "c", "url": "u"}])
    store.mark_media("r1", "a", "bed")

    report = store.reconcile_counts()
    assert report["uid"] == ""
    assert report["posts"] == 3
    assert report["posts_with_text"] == 2
    assert report["posts_without_text"] == 1
    # posts_need_text 的判据是 cleaned_at（清洗流水线是否跑过），不是 TRIM(text)：
    # 三条帖子都还没洗过，所以是 3，而不是跟着 posts_without_text 一起变成 1。
    assert report["posts_need_text"] == 3
    # 洗过一条之后就该少一条，即使它的清洗结果合法为空串（纯链接/纯表情帖）。
    store.mark_post_cleaned("r2", "")
    assert store.reconcile_counts()["posts_need_text"] == 2
    assert report["posts_with_media"] == 2
    assert report["media_expected"] == 5  # 3 + 2 + 0（-1 不计）
    assert report["pic_total_sum"] == 5
    assert report["media_rows"] == 3
    assert report["media_missing_url"] == 1
    assert report["downloaded"] == 1
    assert report["album"] == 1
    assert report["bed"] == 1
    assert report["uploaded_bed"] == 1
    assert report["thumbnail"] == 0
    assert report["vectorized"] == 0
    assert report["pending"] == 2
    assert report["failed"] == 0

    first = store.reconcile_counts("1")
    assert first["uid"] == "1"
    assert first["posts"] == 2
    assert first["media_rows"] == 3
    assert first["posts_with_media"] == 2
    second = store.reconcile_counts("2")
    assert second["posts"] == 1
    assert second["media_rows"] == 0
    assert second["media_expected"] == 0


def test_close_is_idempotent_and_methods_stay_safe(store):
    store.upsert_post(make_post())
    store.close()
    store.close()

    assert store.get_post("1001") is None
    assert store.has_post("1001") is False
    assert store.count_posts() == 0
    assert store.list_posts() == []
    assert store.iter_pending_text_posts() == []
    assert store.upsert_post(make_post("x")) == "x"  # 返回 mid 但不再写库
    assert store.upsert_media("1001", [{"pid": "p"}]) == 0
    assert store.get_media("1001", "p") is None
    assert store.list_media("1001") == []
    assert store.media_stats()["total"] == 0
    assert store.media_file("1001", "p") is None
    assert store.reconcile_counts()["posts"] == 0
    assert store.meta_get("schema_version", "x") == "x"
    store.meta_set("a", "b")
    assert store.get_cursor("42") is None
    store.set_cursor("42", 1)
    store.clear_cursor("42")
    with pytest.raises(ValueError):
        store.set_post_time("1001", "bogus", "x")
    with pytest.raises(ValueError):
        store.mark_media("1001", "p", "bogus")
    assert store.bump_media_attempt("1001", "p") == 0


def test_concurrent_writes_do_not_raise_database_locked(store):
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(15):
                mid = f"{worker_id}-{index}"
                store.upsert_post(make_post(mid, str(worker_id)))
                store.upsert_media(mid, [{"pid": "p", "url": "u"}])
                store.mark_media(mid, "p", "downloaded")
        except BaseException as exc:  # 收集线程内异常，回到主线程统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert store.count_posts() == 60
    assert store.media_stats()["total"] == 60


_OLD_SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE posts (
    mid TEXT PRIMARY KEY, uid TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
    bid TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0, created_raw TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '', text_raw TEXT NOT NULL DEFAULT '',
    is_long INTEGER NOT NULL DEFAULT 0, is_retweet INTEGER NOT NULL DEFAULT 0,
    orig_text TEXT NOT NULL DEFAULT '', pic_total INTEGER NOT NULL DEFAULT -1,
    pics_json TEXT NOT NULL DEFAULT '[]', video_src TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'monitor',
    pushed_at TEXT NOT NULL DEFAULT '', cleaned_at TEXT NOT NULL DEFAULT '',
    reconciled_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE media (
    mid TEXT NOT NULL, pid TEXT NOT NULL, idx INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '', alt_url TEXT NOT NULL DEFAULT '',
    ext TEXT NOT NULL DEFAULT 'jpg', kind TEXT NOT NULL DEFAULT 'image',
    local_path TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending', state_rank INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    downloaded_at TEXT NOT NULL DEFAULT '', album_at TEXT NOT NULL DEFAULT '',
    bed_at TEXT NOT NULL DEFAULT '', thumb_at TEXT NOT NULL DEFAULT '',
    vector_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
    PRIMARY KEY (mid, pid)
);
"""


def test_p5_old_database_gets_new_columns(tmp_path):
    """1 版库（无 cleaned_text / thumb_url）打开后自动补列，旧数据保留。"""
    import sqlite3

    db_path = tmp_path / "archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_OLD_SCHEMA_SQL)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        connection.execute(
            "INSERT INTO posts (mid, uid, text, updated_at) VALUES ('old', '42', '旧正文', ?)",
            ("2026-01-01T00:00:00Z",),
        )
        connection.execute(
            "INSERT INTO media (mid, pid, url, updated_at) VALUES ('old', 'p1', 'u', ?)",
            ("2026-01-01T00:00:00Z",),
        )
        connection.commit()
    finally:
        connection.close()

    store = ArchiveStore(db_path)
    try:
        assert store.schema_version == 2
        assert store.meta_get("schema_version") == "2"
        row = store.get_post("old")
        assert row is not None
        assert row["text"] == "旧正文"
        assert row["cleaned_text"] == ""
        media = store.get_media("old", "p1")
        assert media is not None
        assert media["thumb_url"] == ""
        store.mark_media("old", "p1", "thumb", thumb_url="https://cdn/thumb.jpg")
        assert store.get_media("old", "p1")["thumb_url"] == "https://cdn/thumb.jpg"
    finally:
        store.close()


def test_mark_post_cleaned_and_copy_stats(store):
    store.upsert_post(make_post("1001"))
    store.upsert_post(make_post("1002", pic_total=0, pics=[]))
    store.upsert_post(make_post("1003", is_long=1, is_retweet=1))

    assert {row["mid"] for row in store.iter_uncleaned_posts()} == {"1001", "1002", "1003"}
    store.mark_post_cleaned("1001", "清洗后的文案 [赞]")
    assert [row["mid"] for row in store.iter_uncleaned_posts()] == ["1002", "1003"]
    assert [row["mid"] for row in store.iter_uncleaned_posts(limit=1)] == ["1002"]

    row = store.get_post("1001")
    assert row["cleaned_text"] == "清洗后的文案 [赞]"
    assert is_utc_iso(row["cleaned_at"])

    stats = store.copy_stats()
    assert stats["total"] == 3
    assert stats["cleaned"] == 1
    assert stats["long"] == 1
    assert stats["retweet"] == 1
    assert stats["with_pics"] == 2
    assert store.copy_stats(uid="42")["total"] == 3
    assert store.copy_stats(uid="404")["total"] == 0

    accounts = store.copy_accounts()
    assert accounts == [{"uid": "42", "author": "作者", "total": 3, "cleaned": 1, "long": 1}]
    assert store.copy_signature().startswith("3|")


def test_list_copy_posts_filters_and_paginates(store):
    store.upsert_post(make_post("1001", created_at=100))
    store.upsert_post(make_post("1002", created_at=200, is_retweet=1))
    store.upsert_post(make_post("1003", created_at=300))
    store.mark_post_cleaned("1002", "转发文案")

    newest = store.list_copy_posts(limit=2)
    assert [row["mid"] for row in newest] == ["1003", "1002"]
    assert store.list_copy_posts(limit=2, offset=2)[0]["mid"] == "1001"
    assert [row["mid"] for row in store.list_copy_posts(include_retweets=False)] == [
        "1003",
        "1001",
    ]
    assert [row["mid"] for row in store.list_copy_posts(keyword="转发")] == ["1002"]
    assert store.list_copy_posts(uid="404") == []


def test_list_copy_entries_joins_first_image(store):
    store.upsert_post(make_post("1001"))
    store.upsert_media(
        "1001",
        [
            {"pid": "p2", "idx": 2, "url": "u2"},
            {"pid": "p1", "idx": 1, "url": "u1"},
            {"pid": "v1", "idx": 0, "url": "uv", "kind": "video"},
        ],
    )
    store.mark_media("1001", "p1", "thumb", thumb_url="https://cdn/t1.jpg")
    store.upsert_post(make_post("1002", pic_total=0, pics=[]))
    store.mark_post_cleaned("1001", "文案一")
    store.mark_post_cleaned("1002", "文案二")

    entries = store.list_copy_entries()
    assert [row["mid"] for row in entries] == ["1002", "1001"]
    with_image = entries[1]
    assert with_image["pid"] == "p1"
    assert with_image["image_url"] == "u1"
    assert with_image["thumb_url"] == "https://cdn/t1.jpg"
    assert with_image["cleaned_text"] == "文案一"
    assert entries[0]["pid"] is None
    assert [row["mid"] for row in store.list_copy_entries(uid="404")] == []


def test_text_full_is_not_overwritten_by_truncated_text(store):
    """P2-4：补全长文入库后，时间线刷新带来的短摘要不得把全文顶掉。"""
    full = "全" * 800
    store.upsert_post(make_post(text=full, text_full=1))
    store.upsert_post(make_post(text="截" * 120))
    row = store.get_post("1001")
    assert row["text"] == full
    assert row["text_full"] == 1


def test_text_full_accepts_longer_incoming_text(store):
    """反向：更长的正文照常更新，并升级 text_full。"""
    store.upsert_post(make_post(text="短" * 50))
    store.upsert_post(make_post(text="长" * 500, text_full=1))
    row = store.get_post("1001")
    assert row["text"] == "长" * 500
    assert row["text_full"] == 1


def test_list_media_bulk_matches_per_post_queries(store):
    """P2-3：一次批量取回，结果必须与逐帖 list_media 等价。"""
    for mid in ("1001", "1002", "1004"):
        store.upsert_post(make_post(mid=mid))
        store.upsert_media(
            mid,
            [
                {"pid": f"p{mid}b", "idx": 1, "url": "u2"},
                {"pid": f"p{mid}a", "idx": 0, "url": "u1"},
            ],
        )
    mids = ["1001", "1002", "1003", "1004"]

    bulk = store.list_media_bulk(mids)
    assert set(bulk) == {"1001", "1002", "1004"}, "无媒体的帖不该出现空列表键"
    for mid in bulk:
        assert [row["pid"] for row in bulk[mid]] == [row["pid"] for row in store.list_media(mid)]


def test_list_media_bulk_batches_past_sqlite_variable_limit(store):
    """1200 个 mid 能查完，且结果与逐帖查一致。

    注意：这条**证明不了分批**。本机 sqlite 的 SQLITE_LIMIT_VARIABLE_NUMBER
    实测是 32766（不是早期版本默认的 999），一条 1200 个占位符的 IN 照样能过，
    把分批循环改回单条超长 SQL 它依然绿。真正锁住分批语义的是下面两条按
    真实执行的 SQL 段数断言的用例。
    """
    mids = [str(2000 + i) for i in range(1200)]
    assert store.list_media_bulk(mids) == {}
    store.upsert_post(make_post(mid=mids[-1]))
    store.upsert_media(mids[-1], [{"pid": "pz", "idx": 0, "url": "u"}])
    bulk = store.list_media_bulk(mids)
    assert list(bulk) == [mids[-1]]


_IN_CLAUSE_MARK = "FROM media WHERE mid IN ("


def _media_in_clause_sizes(store, mids: list[str]) -> list[int]:
    """跑一次 list_media_bulk，返回每条 "mid IN (...)" 语句里塞了几个 mid。

    sqlite3 的 trace 回调拿到的是**参数已展开**的语句文本（CPython 走
    sqlite3_expanded_sql，所以看到的是 '3000','3001' 而不是 ?），因此直接数
    IN 列表里的字面量个数就是"这条 SQL 带了多少个 mid"。这是"到底发了几条
    IN、每条多大"的直接证据，与 SQLite 的变量上限有多大无关。
    """
    connection = store._connection()
    assert connection is not None
    seen: list[str] = []
    connection.set_trace_callback(seen.append)
    try:
        store.list_media_bulk(mids)
    finally:
        connection.set_trace_callback(None)
    sizes: list[int] = []
    for statement in seen:
        if _IN_CLAUSE_MARK not in statement:
            continue
        inner = statement.split(_IN_CLAUSE_MARK, 1)[1].split(")", 1)[0]
        sizes.append(inner.count("',") + 1)
    return sizes


def test_list_media_bulk_chunks_by_module_constant(store, monkeypatch):
    """把分批常量调到 2：7 个 mid 必须发 4 条 IN（2/2/2/1），而不是一条。

    变异验证：把 range(0, len, chunk) 改回单条超长 IN（或忽略该常量）时，
    这里拿到的会是 [7] 而不是 [2, 2, 2, 1]，用例必然红。
    """
    from astrbot_plugin_weibo_suite.weibo import archive_db

    monkeypatch.setattr(archive_db, "_BULK_MEDIA_CHUNK", 2)
    mids = [str(3000 + i) for i in range(7)]
    for mid in mids:
        store.upsert_post(make_post(mid=mid))
        store.upsert_media(mid, [{"pid": f"p{mid}", "idx": 0, "url": "u"}])

    assert _media_in_clause_sizes(store, mids) == [2, 2, 2, 1]
    assert len(store.list_media_bulk(mids)) == 7


def test_list_media_bulk_issues_ceil_queries_at_default_chunk(store):
    """默认批次（500）下 1200 个 mid → ceil(1200/500)=3 条 IN，而不是 1 条。"""
    mids = [str(4000 + i) for i in range(1200)]
    assert _media_in_clause_sizes(store, mids) == [500, 500, 200]


def test_list_media_bulk_empty_input(store):
    assert store.list_media_bulk([]) == {}
