"""验证 ImageLibrary 的入库、检索与去重。

含 v1 结构迁移时的 pHash 去重、按扫描目录裁剪记录，
以及匹配结果带回文件大小。
"""

import sqlite3

import pytest

from astrbot_plugin_weibo_suite.trace.library import DEGENERATE_SKIP, ImageLibrary


def make_row(phash: str, file_path: str = "", source: str = "register") -> dict:
    return {
        "phash": phash,
        "dhash": "00",
        "ahash": "00",
        "width": 2,
        "height": 2,
        "file_size": 4,
        "image_url": "",
        "file_path": file_path,
        "source": source,
        "note": "",
        "group_id": "",
        "sender_id": "",
        "sender_name": "",
    }


def test_add_search_and_duplicate(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    entry_id, duplicate = library.add_if_absent(make_row("0f"))
    assert entry_id == 1
    assert duplicate is None

    duplicate_id, duplicate = library.add_if_absent(make_row("0f"))
    assert duplicate_id is None
    assert duplicate["id"] == 1

    results = library.search("0f", limit=2)
    assert len(results) == 1
    assert results[0].similarity == pytest.approx(1.0)
    library.close()


def test_migration_from_v1_deduplicates_phash(tmp_path):
    db_path = tmp_path / "library.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phash TEXT NOT NULL,
            dhash TEXT NOT NULL,
            ahash TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            file_size INTEGER,
            image_url TEXT,
            file_path TEXT,
            source TEXT,
            note TEXT,
            group_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            created_at TEXT
        );
        INSERT INTO images (phash, dhash, ahash, created_at)
        VALUES ('aa', '00', '00', '2026-01-01'),
               ('aa', '00', '00', '2026-01-02');
        """
    )
    connection.commit()
    connection.close()

    library = ImageLibrary(db_path, expected_phash_hex_len=2)
    assert library.count() == 1
    with sqlite3.connect(db_path) as connection:
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_images_phash_unique'"
        ).fetchall()
    assert indexes == [("idx_images_phash_unique",)]
    library.close()


def test_prune_only_removes_scan_rows_under_scanned_dirs(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    keep_dir = tmp_path / "keep"
    drop_dir = tmp_path / "drop"
    outside_dir = tmp_path / "outside"
    for directory in (keep_dir, drop_dir, outside_dir):
        directory.mkdir()

    library.add_many(
        [
            make_row("0f", str(keep_dir / "keep.jpg"), source="scan"),
            make_row("1f", str(drop_dir / "drop.jpg"), source="scan"),
            make_row("2f", str(outside_dir / "outside.jpg"), source="scan"),
            make_row("3f", str(keep_dir / "register.jpg"), source="register"),
        ]
    )

    removed = library.prune_scan_missing(
        {str(keep_dir / "keep.jpg")}, {str(keep_dir), str(drop_dir)}
    )
    assert removed == 1
    assert library.find_by_phash("1f") is None
    assert library.find_by_phash("0f") is not None
    assert library.find_by_phash("2f") is not None
    assert library.find_by_phash("3f") is not None
    library.close()


def test_prune_with_empty_keep_set_clears_scanned_directory(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    library.add_if_absent(make_row("0f", str(scan_dir / "old.jpg"), source="scan"))

    assert library.prune_scan_missing(set(), {str(scan_dir)}) == 1
    assert library.count() == 0
    library.close()


def test_match_result_exposes_stored_file_size(tmp_path):
    """检索结果要带出入库时记录的原图体积。

    回传计划用它判断「图床缩放版是否真的更小」——有它就不必再探测原图体积，
    少一次往返、也少一次图床实时缩放处理。
    """
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    library.add_if_absent(make_row("0f", "C:/images/a.jpg"))
    library.reload_cache()

    hits = library.search("0f", limit=1)

    assert hits and hits[0].file_size == 4
    library.close()


# ----------------------------------------------------------------------
# 退化 pHash（纯色/大面积平坦图）不入库、不参与检索
# ----------------------------------------------------------------------


def test_add_if_absent_rejects_degenerate_phash(tmp_path):
    """全零 pHash 与另一张纯色图距离恒为 0：入库等于把图库永久污染。"""
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)

    entry_id, duplicate = library.add_if_absent(make_row("00"))

    assert entry_id is DEGENERATE_SKIP
    # 调用方可能写成 if entry_id:，退化标记必须为假值才不会被当成成功
    assert bool(entry_id) is False
    assert duplicate is None
    assert library.count() == 0
    assert library.search("00") == []
    assert library.search("0f") == []
    assert library.find_by_phash("00") is None
    library.close()


def test_add_many_filters_degenerate_rows(tmp_path):
    """/溯源重扫 的批量入库同样要过滤，否则纯色图会从批量口子漏进库。"""
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)

    inserted = library.add_many(
        [
            make_row("00", "C:/pure.jpg", source="scan"),
            make_row("0f", "C:/real.jpg", source="scan"),
        ]
    )

    assert inserted == 1
    assert library.count() == 1
    assert library.get_paths_for_source("scan") == {"C:/real.jpg"}
    library.close()


def test_legacy_degenerate_row_is_excluded_from_index(tmp_path):
    """旧版本写进库的全零行不能进内存索引，否则任意纯色图都以 1.0 命中它。"""
    db_path = tmp_path / "library.db"
    library = ImageLibrary(db_path, expected_phash_hex_len=2)
    library.add_if_absent(make_row("0f", "C:/real.jpg"))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO images (phash, dhash, ahash, file_size, file_path, source, created_at)"
            " VALUES ('00', '00', '00', 4, 'C:/pure.jpg', 'register', '2026-01-01')"
        )
    library.reload_cache()

    stats = library.stats()
    assert stats["total"] == 2
    assert stats["indexed"] == 1
    assert stats["skipped"] == 1
    assert library.find_by_phash("00") is None
    assert len(library.search("0f")) == 1
    library.close()


def test_add_if_absent_appends_to_live_index(tmp_path, monkeypatch):
    """单张登记只追加一行并立即进检索，不再为一张图重建整张矩阵。"""
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    library.add_if_absent(make_row("0f", "C:/a.jpg"))

    def boom():
        raise AssertionError("单张登记不应触发全表重建")

    monkeypatch.setattr(library, "_reload_cache", boom)

    assert library.add_if_absent(make_row("00", "C:/pure.jpg"))[0] is DEGENERATE_SKIP
    entry_id, duplicate = library.add_if_absent(make_row("1f", "C:/b.jpg"))

    assert (entry_id, duplicate) == (2, None)
    assert [hit.id for hit in library.search("0f", limit=5)] == [1, 2]
    library.close()


def test_replace_source_swaps_rows_and_keeps_other_sources(tmp_path):
    """强制重建只替换目标来源，register 行必须原样保留。"""
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    library.add_many(
        [
            make_row("0f", "C:/old1.jpg", source="scan"),
            make_row("1f", "C:/old2.jpg", source="scan"),
            make_row("2f", "C:/reg.jpg", source="register"),
        ]
    )

    inserted = library.replace_source("scan", [make_row("3f", "C:/new1.jpg", source="scan")])

    assert inserted == 1
    assert library.get_paths_for_source("scan") == {"C:/new1.jpg"}
    assert library.get_paths_for_source("register") == {"C:/reg.jpg"}
    assert [hit.id for hit in library.search("3f")] != []
    library.close()


def test_replace_source_rolls_back_on_failure(tmp_path):
    """中途失败必须整体回滚：旧 scan 行原样保留，最坏只是这次重建没生效。"""
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    library.add_many(
        [
            make_row("0f", "C:/old.jpg", source="scan"),
            make_row("1f", "C:/reg.jpg", source="register"),
        ]
    )
    broken = make_row("2f", "C:/new.jpg", source="scan")
    broken["file_size"] = object()  # sqlite3 无法绑定该类型：写入期抛错

    with pytest.raises(sqlite3.Error):
        library.replace_source("scan", [broken])

    assert library.get_paths_for_source("scan") == {"C:/old.jpg"}
    assert library.count() == 2
    library.close()
