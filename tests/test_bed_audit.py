"""图床反向收录巡检的纯函数与补录服务。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from types import SimpleNamespace

import pytest

from astrbot_plugin_weibo_suite.trace.bed_audit import (
    BedAuditService,
    build_index_snapshot,
    canonical_name,
    format_bed_audit,
    is_weibo_image,
    parse_audit_args,
)
from astrbot_plugin_weibo_suite.trace.bed_audit_mixin import BedAuditMixin
from astrbot_plugin_weibo_suite.trace.storage_utils import BedFile


def _entry(name: str, *, directory: str = "", url: str = "", local_path: str = "") -> BedFile:
    file_name = name.rsplit("/", 1)[-1]
    return BedFile(
        name=name,
        file_name=file_name,
        directory=directory,
        url=url,
        local_path=local_path,
    )


def test_canonical_name_strips_only_saf_duplicate_suffix():
    assert (
        canonical_name("【微博@博主】20260101-1：图 (1).jpg") == "【微博@博主】20260101-1：图.jpg"
    )
    assert (
        canonical_name("【微博@博主】20260101-1：图(12).jpg") == "【微博@博主】20260101-1：图.jpg"
    )
    assert canonical_name("旅行 (2024).jpg") == "旅行 (2024).jpg"


def test_is_weibo_image_excludes_thumbnail_and_other_business():
    assert is_weibo_image(_entry("12、日常/【微博图集@博主】/【微博@博主】20260101-1：图.jpg"))
    assert not is_weibo_image(
        _entry("thumbnails/【微博@博主】20260101-1：图.jpg", directory="thumbnails/")
    )
    assert not is_weibo_image(_entry("旅行/风景.jpg"))


def test_build_index_snapshot_reads_name_url_and_phash():
    points = [
        {
            "id": "1",
            "payload": {
                "file_name": "【微博@博主】20260101-1：图.jpg",
                "image_url": "https://bed.example.com/file/%E5%9B%BE.jpg",
                "phash": "AABB",
            },
        },
        {
            "id": "2",
            "payload": {
                "image_url": "https://cdn.example.com/file/dir/%E5%9B%BE.jpg",
                "phash": "ccdd",
            },
        },
    ]
    snapshot = build_index_snapshot(points)

    assert snapshot.points == 2
    assert "【微博@博主】20260101-1：图.jpg" in snapshot.names
    assert "aabb" in snapshot.phashes
    assert "ccdd" in snapshot.phashes
    assert "bed.example.com/file/图.jpg" in snapshot.urls
    assert "dir/图.jpg" in snapshot.paths


def test_parse_audit_args():
    assert parse_audit_args("dry 200") == (True, 200)
    assert parse_audit_args("只查") == (True, 0)
    assert parse_audit_args("300") == (False, 300)
    assert parse_audit_args("") == (False, 0)


class FakeBed:
    mode = "cloudflare_imgbed"

    def __init__(self, entries):
        self.entries = entries
        self.store_calls = 0

    async def list_files(self, *, limit: int = 0):
        return self.entries[:limit] if limit else list(self.entries)

    async def store(self, *_args, **_kwargs):
        self.store_calls += 1
        raise AssertionError("图床巡检禁止重复上传")

    @staticmethod
    def src_path_for(url: str) -> str:
        return url.split("/file/", 1)[-1]


class FakeVector:
    qdrant_ready = True
    enabled = True
    collection = "test"

    def __init__(self, points):
        self.points = points
        self.batches = []

    async def scroll_payloads(self, **_kwargs):
        return self.points

    async def upsert_file_batch(self, entries):
        self.batches.append(entries)
        for item in entries:
            self.points.append(
                {
                    "id": item.point_key,
                    "payload": {
                        "file_name": item.file_name,
                        "image_url": item.image_url,
                        "phash": item.point_key.split(":", 1)[-1],
                    },
                }
            )
        return len(entries)


class FakeLibrary:
    def __init__(self):
        self.rows = {}

    def find_by_phash(self, phash):
        return self.rows.get(phash)

    def add_if_absent(self, row):
        phash = row["phash"]
        if phash in self.rows:
            return None, self.rows[phash]
        self.rows[phash] = row
        return len(self.rows), None


@pytest.fixture
def audit(tmp_path):
    def build(entries, points, features, *, local_paths=None):
        async def download(url):
            name = url.rsplit("/", 1)[-1]
            path = tmp_path / f"dl_{len(os.listdir(tmp_path))}_{name}"
            path.write_bytes(b"x")
            return str(path)

        def compute(path, _hash_size):
            key = os.path.basename(path)
            if local_paths and path in local_paths:
                key = local_paths[path]
            elif key.startswith("dl_"):
                # 假下载文件名是 dl_<序号>_<原名>，取回原名查 features
                key = key.split("_", 2)[2]
            phash = features[key]
            return SimpleNamespace(phash=phash, width=100, height=200), "image/jpeg"

        removed = []

        def remove(path):
            removed.append(path)
            with contextlib.suppress(OSError):
                os.unlink(path)

        service = BedAuditService(
            bed=FakeBed(entries),
            vector=FakeVector(points),
            library=FakeLibrary(),
            download=download,
            compute_features=compute,
            remove_file=remove,
            hash_size=16,
        )
        return service, removed

    return build


async def test_run_matches_filename_then_content_and_adds_only_new(audit):
    indexed = _entry(
        "12、日常/【微博图集@博主】/【微博@博主】20260101-1：已收录.jpg",
        directory="12、日常/【微博图集@博主】/",
        url="https://bed.example.com/file/12、日常/【微博图集@博主】/【微博@博主】20260101-1：已收录.jpg",
    )
    same_content = _entry(
        "12、日常/【微博图集@博主】/【微博@博主】20260101-2：同图异名.jpg",
        directory="12、日常/【微博图集@博主】/",
        url="https://bed.example.com/file/12、日常/【微博图集@博主】/【微博@博主】20260101-2：同图异名.jpg",
    )
    brand_new = _entry(
        "12、日常/【微博图集@博主】/【微博@博主】20260101-3：新图.jpg",
        directory="12、日常/【微博图集@博主】/",
        url="https://bed.example.com/file/12、日常/【微博图集@博主】/【微博@博主】20260101-3：新图.jpg",
    )
    thumb = _entry(
        "thumbnails/【微博@博主】20260101-1：已收录.jpg",
        directory="thumbnails/",
        url="https://bed.example.com/file/thumbnails/【微博@博主】20260101-1：已收录.jpg",
    )
    other = _entry(
        "12、日常/风景.jpg",
        directory="12、日常/",
        url="https://bed.example.com/file/12、日常/风景.jpg",
    )
    points = [
        {
            "id": "p1",
            "payload": {
                "file_name": indexed.file_name,
                "image_url": indexed.url,
                "phash": "aaaa",
            },
        },
        {
            "id": "p2",
            "payload": {
                "file_name": "【微博@博主】20260101-9：旧名.jpg",
                "image_url": "https://bed.example.com/file/old.jpg",
                "phash": "bbbb",
            },
        },
    ]
    service, removed = audit(
        [indexed, same_content, brand_new, thumb, other],
        points,
        {
            same_content.file_name: "bbbb",
            brand_new.file_name: "cccc",
            thumb.file_name: "dddd",
            other.file_name: "eeee",
        },
    )
    stats = await service.run()

    assert stats.listed == 5
    assert stats.weibo == 3
    assert stats.known_by_name == 1
    assert stats.known_by_content == 1
    assert stats.missing == 1
    assert stats.added_vector == 1
    assert stats.added_library == 1
    assert service.bed.store_calls == 0
    assert len(service.vector.batches) == 1
    assert service.vector.batches[0][0].file_name == brand_new.file_name
    assert service.vector.batches[0][0].point_key == "phash:cccc"
    assert service.vector.batches[0][0].image_url == brand_new.url
    assert service.library.rows["cccc"]["source"] == "bed_audit"
    assert removed, "下载的临时文件必须删掉"


async def test_same_content_twice_in_one_scan_adds_once(audit):
    first = _entry(
        "【微博图集@博主】/【微博@博主】20260101-1：同图.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/first.jpg",
    )
    second = _entry(
        "【微博图集@博主】/【微博@博主】20260102-2：同图换名.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/second.jpg",
    )
    service, _removed = audit([first, second], [], {"first.jpg": "cccc", "second.jpg": "cccc"})

    stats = await service.run()

    assert stats.weibo == 2
    assert stats.missing == 1
    assert stats.known_by_content == 1
    assert stats.added_vector == 1
    assert stats.added_library == 1
    assert len(service.vector.batches[0]) == 1
    assert service.bed.store_calls == 0


async def test_dry_run_does_not_write_or_mark(audit):
    entry = _entry(
        "【微博图集@博主】/【微博@博主】20260101-1：新图.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/new.jpg",
    )
    service, _removed = audit([entry], [], {"new.jpg": "cccc"})

    stats = await service.run(dry_run=True)

    assert stats.missing == 1
    assert stats.added_vector == 0
    assert stats.added_library == 0
    assert service.vector.batches == []
    assert service.library.rows == {}
    assert service.bed.store_calls == 0
    assert "未收录" in format_bed_audit(stats)


async def test_local_mode_only_adds_library(audit, tmp_path):
    local_file = tmp_path / "local.jpg"
    local_file.write_bytes(b"x")
    entry = _entry(
        "【微博图集@博主】/【微博@博主】20260101-1：本地.jpg",
        directory="【微博图集@博主】/",
        local_path=str(local_file),
    )
    service, _removed = audit(
        [entry], [], {entry.file_name: "cccc"}, local_paths={str(local_file): entry.file_name}
    )
    service.vector.qdrant_ready = False
    service.vector.enabled = False

    stats = await service.run()

    assert stats.added_vector == 0
    assert stats.added_library == 1
    assert service.library.rows["cccc"]["image_url"] == ""
    assert service.bed.store_calls == 0


class _EmptyBed:
    mode = "local"

    async def list_files(self, *, limit: int = 0):
        return []


class _MixinHost(BedAuditMixin):
    """不给 AstrBot 的宿主壳，只验证首次启动标记与签名变化。"""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self._bed_cfg = {}
        self.vector = SimpleNamespace(
            qdrant_ready=False,
            enabled=False,
            qdrant_url="",
            collection="",
        )
        self.library = FakeLibrary()
        self.bed = _EmptyBed()
        self.hash_size = 16
        self._bed_audit_lock = asyncio.Lock()
        self._bed_audit_task = None

    def _bed_mode(self):
        return "local"

    def _dict_cfg(self, group):
        assert group == "limits"
        return {"bed_audit_on_start": True, "bed_audit_max_files": 0}

    async def _download(self, url):
        raise AssertionError("空图床不应下载")

    @staticmethod
    def _remove_quiet(path):
        return None


async def test_first_start_marks_done_and_skips_next(tmp_path):
    host = _MixinHost(tmp_path)
    host._maybe_start_bed_audit()
    task = host._bed_audit_task
    assert task is not None
    await task

    state = host._load_bed_audit_state()
    assert state["status"] == "done"
    assert state["stats"]["listed"] == 0

    host._maybe_start_bed_audit()
    assert host._bed_audit_task is None or host._bed_audit_task.done()

    # 换图床定位后签名变化，允许再跑一次首次补录
    host._bed_cfg = {"local_dir": str(tmp_path / "other")}
    host._maybe_start_bed_audit()
    assert host._bed_audit_task is not None
    await host._bed_audit_task


async def test_early_skip_paths_remove_downloaded_temp_files(audit, tmp_path):
    """判重/退化 pHash 提前返回时，下载的临时文件也必须删掉。

    线上图床可能上千张图，逐张下载后只要有一张判重就直接 return，早期实现
    只在异常分支清理，正常运行一轮就会在临时目录留下成百上千个副本。
    """
    same_content = _entry(
        "【微博图集@博主】/【微博@博主】20260101-1：同图.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/same-content.jpg",
    )
    degenerate = _entry(
        "【微博图集@博主】/【微博@博主】20260101-2：纯色.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/degenerate.jpg",
    )
    indexed = _entry(
        "【微博图集@博主】/【微博@博主】20260101-3：已收录.jpg",
        directory="【微博图集@博主】/",
        url="https://bed.example.com/file/indexed.jpg",
    )
    points = [
        {
            "id": "p1",
            "payload": {"file_name": indexed.file_name, "image_url": indexed.url, "phash": "aaaa"},
        }
    ]
    service, removed = audit(
        [same_content, degenerate, indexed],
        points,
        {"same-content.jpg": "aaaa", "degenerate.jpg": ""},
    )

    stats = await service.run(dry_run=True)

    assert stats.errors == 0
    assert stats.known_by_name == 1
    assert stats.known_by_content == 1
    assert stats.degenerate == 1
    assert stats.missing == 0
    assert len(removed) == 2, "两张下载过的图都要清理临时文件"
    assert not list(tmp_path.glob("dl_*"))


async def test_manual_limit_run_marks_done_when_enumeration_completes(tmp_path):
    """上限大于图床文件数时说明已经扫完，不应再标 partial 每次启动重跑。"""
    host = _MixinHost(tmp_path)
    host._dict_cfg = lambda group: {"bed_audit_on_start": True, "bed_audit_max_files": 3}

    await host.run_bed_audit(limit=3)

    assert host._load_bed_audit_state()["status"] == "done"
