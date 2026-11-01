"""图床 / 缩略图 / 向量管线（weibo/pipeline.py）离线单测。

全部依赖注入 fake：图床、向量库、图库都是内存对象，归档库用真实
ArchiveStore（tmp_path），特征提取 monkeypatch 成纯函数——不联网、不 sleep。
"""

import asyncio
import contextlib
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_weibo_suite.weibo import pipeline as pipeline_mod
from astrbot_plugin_weibo_suite.weibo.archive_db import ArchiveStore
from astrbot_plugin_weibo_suite.weibo.pipeline import PipelineMixin, PipelineServices

MID = "5001"
UID = "1001"
AUTHOR = "博主甲"
CREATED = 1700000000  # 东八区 2023-11-15
POST_TEXT = "#旅行记# 今天去海边\n好开心"
DEFAULT_DIR = "未分类/【微博图集@博主甲】"
DEFAULT_FILE = "【微博@博主甲】2023-11-15-1：今天去海边.jpg"


# ---------------------------------------------------------------- fakes


class FakeBed:
    """记录 store(path, folder=...) 调用；fail_names 里的文件名返回 ok=False。"""

    def __init__(self, fail_names=()):
        self.calls = []
        self.fail_names = set(fail_names)

    async def store(self, path, *, folder=""):
        name = Path(path).name
        self.calls.append((str(path), str(folder)))
        if name in self.fail_names:
            return SimpleNamespace(ok=False, url=None, message="上传失败")
        return SimpleNamespace(
            ok=True, url=f"https://cdn.example/{len(self.calls)}/{name}", message=""
        )


class FakeVector:
    def __init__(self, error: str = ""):
        self.batches = []
        self.error = error

    async def upsert_file_batch(self, entries):
        self.batches.append(list(entries))
        if self.error:
            raise RuntimeError(self.error)
        return len(entries)


class FakeLibrary:
    def __init__(self, hits=None):
        self.rows = []
        self.hits = dict(hits or {})

    def find_by_phash(self, phash):
        return self.hits.get(phash)

    def add_if_absent(self, row):
        self.rows.append(dict(row))
        return (len(self.rows), None)


class FakeHost(PipelineMixin):
    """极简宿主：只提供 archive 层面被管线用到的方法。"""

    def __init__(self, store=None, services=None, config=None, data_dir=None):
        self.config = {"pipeline": dict(config or {})}
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._store = store
        self._pipeline_services = services
        self.downloads = []
        self.download_factory = None

    def archive_store(self):
        return self._store

    def archive_ready(self):
        return self._store is not None

    async def archive_download_post(self, mid):
        self.downloads.append(mid)
        if self.download_factory is not None:
            self.download_factory(mid)
        return {"mid": mid, "downloaded": 0, "failed": 0}


# ---------------------------------------------------------------- helpers


def _seed(hash_name: str) -> str:
    return hashlib.md5(hash_name.encode("utf-8")).hexdigest()[:8]


def _phash_for(path) -> str:
    return f"p{_seed(Path(path).stem)}"


def fake_features(path, hash_size, *, max_side=720, quality=72):
    seed = _seed(Path(path).stem)
    features = SimpleNamespace(
        phash=f"p{seed}", dhash=f"d{seed}", ahash=f"a{seed}", width=800, height=600
    )
    return features, "image/jpeg", b"thumb-" + seed.encode()


@pytest.fixture(autouse=True)
def _patch_features(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "_pipeline_compute_features", fake_features)


def make_store(tmp_path) -> ArchiveStore:
    return ArchiveStore(tmp_path / "archive.db", media_root=tmp_path / "media")


def add_post(
    store,
    tmp_path,
    mid=MID,
    *,
    uid=UID,
    author=AUTHOR,
    created=CREATED,
    text=POST_TEXT,
    count=2,
    state="downloaded",
    attempts=0,
    local=True,
):
    store.upsert_post(
        {
            "mid": mid,
            "uid": uid,
            "author": author,
            "created_at": created,
            "text": text,
            "pic_total": count,
        }
    )
    items = [
        {"pid": f"{mid}-{i}", "idx": i, "url": f"https://wx.example/{mid}/{i}.jpg", "ext": "jpg"}
        for i in range(count)
    ]
    store.upsert_media(mid, items)
    for item in items:
        if local and state != "pending":
            path = tmp_path / "media" / uid / mid / f"{item['pid']}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"img-" + item["pid"].encode())
            store.mark_media(mid, item["pid"], state, local_path=str(path))
        for _ in range(attempts):
            store.bump_media_attempt(mid, item["pid"], "seed")
    return items


def make_services(bed=None, vector=None, library=None, *, bed_mode="openlist", hash_size=16):
    return PipelineServices(
        bed=bed if bed is not None else FakeBed(),
        vector=vector if vector is not None else FakeVector(),
        library=library if library is not None else FakeLibrary(),
        hash_size=hash_size,
        bed_mode=bed_mode,
    )


def make_host(tmp_path, store, *, services=None, config=None):
    return FakeHost(store=store, services=services, config=config, data_dir=tmp_path / "data")


async def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


def media(store, mid, index):
    return store.list_media(mid)[index]


# ---------------------------------------------------------------- 单帖流程


async def test_run_post_without_store_reports_archive_disabled(tmp_path):
    host = make_host(tmp_path, None, services=make_services())
    result = await host.pipeline_run_post(MID)
    assert result["mid"] == MID
    assert result["error"] == "归档未启用"


async def test_run_post_without_media_returns_empty_stats(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, count=0)
    host = make_host(tmp_path, store, services=make_services())
    result = await host.pipeline_run_post(MID)
    assert result["processed"] == 0
    assert result["skipped"] == 0
    assert result["mode"] == "plugin"


async def test_run_post_plugin_mode_needs_bed_and_vector(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    services = PipelineServices(bed=FakeBed(), vector=None, bed_mode="openlist")
    host = make_host(tmp_path, store, services=services)
    result = await host.pipeline_run_post(MID)
    assert result["error"] == "图床管线未就绪"


async def test_cfb_mode_only_makes_originals_local(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    bed, vector, library = FakeBed(), FakeVector(), FakeLibrary()
    services = make_services(bed, vector, library, bed_mode="")
    host = make_host(tmp_path, store, services=services)
    result = await host.pipeline_run_post(MID)
    assert result == {
        "mid": MID,
        "mode": "cfb",
        "processed": 0,
        "skipped": 2,
        "failed": 0,
        "exhausted": 0,
        "uploaded": 0,
        "thumbed": 0,
        "vectored": 0,
        "errors": [],
    }
    assert bed.calls == []
    assert vector.batches == []
    assert host.downloads == []


async def test_cfb_mode_downloads_missing_originals_once(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, local=False)
    services = make_services(bed_mode="cloudflare_imgbed")
    host = make_host(tmp_path, store, services=services)

    def factory(mid):
        for row in store.list_media(mid):
            path = tmp_path / "media" / UID / mid / f"{row['pid']}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fresh")
            store.mark_media(mid, row["pid"], "downloaded", local_path=str(path))

    host.download_factory = factory
    result = await host.pipeline_run_post(MID)
    assert result["mode"] == "cfb"
    assert result["skipped"] == 2
    assert host.downloads == [MID]
    assert services.bed.calls == []


async def test_retry_after_bed_does_not_reupload_original(tmp_path):
    """已到 bed 的图补跑只补缩略图，避免图床出现重复对象。"""
    store = make_store(tmp_path)
    add_post(store, tmp_path, state="bed")
    bed, vector, library = FakeBed(), FakeVector(), FakeLibrary()
    host = make_host(tmp_path, store, services=make_services(bed, vector, library))

    result = await host.pipeline_run_post(MID)

    assert result["uploaded"] == 0
    assert result["thumbed"] == 2
    assert len(bed.calls) == 2
    assert all(folder == "thumbnails/" for _path, folder in bed.calls)
    assert all(row["state"] == "vector" for row in store.list_media(MID))


async def test_retry_after_thumb_only_resumes_vector(tmp_path):
    """缩略图已上传、向量失败：补跑不再碰图床，只重建向量。"""
    store = make_store(tmp_path)
    add_post(store, tmp_path, state="thumb")
    for index, row in enumerate(store.list_media(MID)):
        store.mark_media(
            MID, row["pid"], "thumb", thumb_url=f"https://cdn.example/thumb/{index}.jpg"
        )
    bed, vector, library = FakeBed(), FakeVector(), FakeLibrary()
    host = make_host(tmp_path, store, services=make_services(bed, vector, library))

    result = await host.pipeline_run_post(MID)

    assert result["uploaded"] == 0
    assert result["thumbed"] == 0
    assert result["vectored"] == 2
    assert bed.calls == []
    assert len(vector.batches) == 1


async def test_plugin_mode_runs_full_flow(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    bed, vector, library = FakeBed(), FakeVector(), FakeLibrary()
    services = make_services(bed, vector, library)
    host = make_host(
        tmp_path, store, services=services, config={"categories": {UID: "12、日常分享"}}
    )
    result = await host.pipeline_run_post(MID)

    assert result["processed"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["uploaded"] == 2
    assert result["thumbed"] == 2
    assert result["vectored"] == 2

    assert len(bed.calls) == 4
    assert bed.calls[0][1] == "12、日常分享/【微博图集@博主甲】"
    assert bed.calls[1][1] == "thumbnails/"
    assert Path(bed.calls[0][0]).name == "【微博@博主甲】2023-11-15-1：今天去海边.jpg"
    assert Path(bed.calls[1][0]).name == "【微博_博主甲】2023-11-15-1：今天去海边.jpg"

    assert len(vector.batches) == 1
    entries = vector.batches[0]
    assert len(entries) == 2
    assert [entry.point_key for entry in entries] == [
        "phash:" + _phash_for(store.list_media(MID)[0]["local_path"]),
        "phash:" + _phash_for(store.list_media(MID)[1]["local_path"]),
    ]
    assert entries[0].point_key.startswith("phash:")
    assert entries[0].src == "weibo"
    assert entries[0].mime == "image/jpeg"
    assert entries[0].file_size == len(b"img-" + b"5001-0")

    assert [row["state"] for row in store.list_media(MID)] == ["vector", "vector"]
    assert len(library.rows) == 2
    assert library.rows[0]["source"] == "weibo"
    assert library.rows[0]["note"] == MID
    assert library.rows[0]["image_url"].startswith("https://cdn.example/")

    stage_root = tmp_path / "data" / "pipeline_stage"
    assert stage_root.is_dir()
    assert list(stage_root.iterdir()) == []


async def test_plugin_mode_skips_rows_already_vector(tmp_path):
    store = make_store(tmp_path)
    items = add_post(store, tmp_path)
    path = store.get_media(MID, items[0]["pid"])["local_path"]
    store.mark_media(MID, items[0]["pid"], "vector", local_path=path)
    bed, vector = FakeBed(), FakeVector()
    host = make_host(tmp_path, store, services=make_services(bed, vector))
    result = await host.pipeline_run_post(MID)
    assert result["skipped"] == 1
    assert result["processed"] == 1
    assert len(bed.calls) == 2
    assert len(vector.batches[0]) == 1


async def test_library_hit_skips_upload_and_marks_vector(tmp_path):
    store = make_store(tmp_path)
    items = add_post(store, tmp_path)
    local = store.get_media(MID, items[0]["pid"])["local_path"]
    library = FakeLibrary({_phash_for(local): {"image_url": "https://cdn.example/dup.jpg"}})
    bed, vector = FakeBed(), FakeVector()
    host = make_host(tmp_path, store, services=make_services(bed, vector, library))
    result = await host.pipeline_run_post(MID)

    assert result["skipped"] == 1
    assert result["processed"] == 1
    assert len(bed.calls) == 2
    assert store.get_media(MID, items[0]["pid"])["state"] == "vector"
    assert len(vector.batches[0]) == 1
    assert [row["phash"] for row in library.rows] == [
        _phash_for(store.get_media(MID, items[1]["pid"])["local_path"])
    ]


async def test_library_hit_without_image_url_still_uploads(tmp_path):
    store = make_store(tmp_path)
    items = add_post(store, tmp_path)
    local = store.get_media(MID, items[0]["pid"])["local_path"]
    library = FakeLibrary({_phash_for(local): {"image_url": "  "}})
    host = make_host(tmp_path, store, services=make_services(FakeBed(), FakeVector(), library))
    result = await host.pipeline_run_post(MID)
    assert result["processed"] == 2
    assert len(library.rows) == 2


async def test_single_image_failure_does_not_stop_post(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    bed = FakeBed(fail_names={"1.jpg"})
    vector, library = FakeVector(), FakeLibrary()
    host = make_host(
        tmp_path,
        store,
        services=make_services(bed, vector, library),
        config={"naming_directory": "web", "naming_file": "{index}.{ext}"},
    )
    result = await host.pipeline_run_post(MID)

    assert result["failed"] == 1
    assert result["exhausted"] == 0
    assert result["processed"] == 1
    assert result["vectored"] == 1
    first = store.list_media(MID)[0]
    assert first["state"] == "downloaded"
    assert first["attempts"] == 1
    assert "上传失败" in first["error"]
    assert store.list_media(MID)[1]["state"] == "vector"
    assert len(vector.batches[0]) == 1
    assert len(result["errors"]) == 1


async def test_failure_reaching_max_attempts_is_exhausted(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, count=1, attempts=2)
    host = make_host(
        tmp_path,
        store,
        services=make_services(FakeBed(fail_names={"1.jpg"}), FakeVector(), FakeLibrary()),
        config={"naming_directory": "web", "naming_file": "{index}.{ext}"},
    )
    result = await host.pipeline_run_post(MID)
    assert result["exhausted"] == 1
    assert result["failed"] == 0
    assert store.list_media(MID)[0]["attempts"] == 3


async def test_vector_batch_failure_records_each_image(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    vector = FakeVector(error="boom")
    library = FakeLibrary()
    host = make_host(tmp_path, store, services=make_services(FakeBed(), vector, library))
    result = await host.pipeline_run_post(MID)

    assert result["error"].startswith("向量批量写入失败")
    assert result["failed"] == 2
    assert result["vectored"] == 0
    assert library.rows == []
    for row in store.list_media(MID):
        assert row["state"] == "thumb"
        assert row["attempts"] == 1
        assert "boom" in row["error"]


async def test_missing_local_downloads_once_then_processes(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, local=False)
    host = make_host(tmp_path, store, services=make_services())
    counted = []

    def factory(mid):
        counted.append(mid)
        for row in store.list_media(mid):
            path = tmp_path / "media" / UID / mid / f"{row['pid']}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fresh-" + row["pid"].encode())
            store.mark_media(mid, row["pid"], "downloaded", local_path=str(path))

    host.download_factory = factory
    result = await host.pipeline_run_post(MID)
    assert result["processed"] == 2
    assert host.downloads == [MID]
    assert counted == [MID]
    assert [row["state"] for row in store.list_media(MID)] == ["vector", "vector"]


# ---------------------------------------------------------------- 队列 / worker


def test_enqueue_rejects_when_disabled_or_not_started(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": False})
    assert host.pipeline_enqueue(MID) is False
    assert host.pipeline_enabled() is False

    host.config = {"pipeline": {"enabled": "true"}}
    assert host.pipeline_enabled() is True
    assert host.pipeline_enqueue(MID) is False  # 还没 pipeline_start

    host._pipeline_started = True
    assert host.pipeline_enqueue("") is False
    assert host.pipeline_enqueue(MID) is True


def test_enqueue_dedupes_and_tracks_queue_size(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host._pipeline_started = True
    assert host.pipeline_enqueue(MID) is True
    assert host.pipeline_enqueue(MID) is False
    assert host.pipeline_queue_size() == 1
    assert host._pipeline_pending == {MID}


def test_enqueue_rejects_when_queue_is_full(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host._pipeline_started = True
    host._pipeline_queue = asyncio.Queue(maxsize=1)
    host._pipeline_queue.put_nowait("other")
    assert host.pipeline_enqueue(MID) is False
    assert host._pipeline_pending == set()
    assert host.pipeline_queue_size() == 1


def test_notify_archived_four_reject_branches_and_accept(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=None, config={"enabled": True})
    host._pipeline_started = True
    assert host.pipeline_notify_archived(MID) is False  # 未 bind 服务

    host.pipeline_bind(make_services())
    host.config = {"pipeline": {"enabled": False}}
    assert host.pipeline_notify_archived(MID) is False  # 管线未启用

    host.config = {"pipeline": {"enabled": True, "auto_queue": False}}
    assert host.pipeline_notify_archived(MID) is False  # auto_queue 关闭

    host.config = {"pipeline": {"enabled": True, "enabled_uids": ["2002"]}}
    assert host.pipeline_notify_archived(MID) is False  # 博主不在白名单

    host.config = {"pipeline": {"enabled": True, "enabled_uids": [UID]}}
    assert host.pipeline_notify_archived(MID) is True
    assert host.pipeline_queue_size() == 1
    assert host.pipeline_notify_archived(MID) is False  # 已在队列里


def test_enqueue_tolerates_missing_config_section(tmp_path):
    host = FakeHost(store=make_store(tmp_path), services=make_services())
    host.config = {}
    assert host.pipeline_enabled() is False
    assert host.pipeline_enqueue(MID) is False
    assert host.pipeline_queue_size() == 0
    assert host.pipeline_stats()["enabled"] is False


async def test_pipeline_start_is_idempotent_and_worker_drains_queue(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host.pipeline_start()
    task = host._pipeline_task
    host.pipeline_start()
    assert host._pipeline_task is task
    try:
        assert host.pipeline_enqueue(MID) is True
        await wait_until(lambda: host.pipeline_queue_size() == 0)
        await wait_until(lambda: store.media_stats()["vector"] == 2)
        assert host._pipeline_pending == set()
        assert [row["state"] for row in store.list_media(MID)] == ["vector", "vector"]
    finally:
        await host.pipeline_stop()


async def test_pipeline_stop_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host.pipeline_start()
    host.pipeline_enqueue(MID)
    await host.pipeline_stop()
    assert host.pipeline_queue_size() == 0
    assert host._pipeline_pending == set()
    assert host._pipeline_task is None
    await host.pipeline_stop()  # 二次调用不再抛
    assert host.pipeline_queue_size() == 0


async def test_worker_backs_off_exponentially_and_resets(monkeypatch, tmp_path):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    class Shim:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        sleep = staticmethod(fake_sleep)

    monkeypatch.setattr(pipeline_mod, "asyncio", Shim())
    store = make_store(tmp_path)
    add_post(store, tmp_path)
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host._pipeline_started = True
    calls = []

    async def fake_run(mid):
        calls.append(mid)
        if mid in {"bad-1", "bad-2", "bad-3"}:
            raise RuntimeError("boom")
        return {"mid": mid}

    host.pipeline_run_post = fake_run
    task = asyncio.create_task(host._pipeline_worker())
    try:
        for mid in ("bad-1", "bad-2", "bad-3", "good-1"):
            assert host.pipeline_enqueue(mid) is True
        await wait_until(lambda: len(calls) == 4)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert calls == ["bad-1", "bad-2", "bad-3", "good-1"]
    assert delays == [5.0, 10.0, 20.0]


# ---------------------------------------------------------------- 补跑 / 统计


async def test_backfill_filters_enabled_uids_and_dedupes(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, "5001", uid="1001", count=1)
    add_post(store, tmp_path, "5002", uid="2002", count=1)
    host = make_host(
        tmp_path,
        store,
        services=make_services(),
        config={"enabled": True, "enabled_uids": ["1001"]},
    )
    host._pipeline_started = True
    first = await host.pipeline_backfill(limit=10)
    assert first == {"scanned": 2, "queued": 1}
    assert host.pipeline_queue_size() == 1
    second = await host.pipeline_backfill(limit=10)
    assert second == {"scanned": 2, "queued": 0}
    assert host.pipeline_queue_size() == 1


async def test_backfill_without_store_reports_error(tmp_path):
    host = FakeHost(store=None, services=make_services(), config={"enabled": True})
    assert await host.pipeline_backfill() == {"scanned": 0, "queued": 0, "error": "归档未启用"}


def test_pipeline_stats_and_status_text(tmp_path):
    store = make_store(tmp_path)
    add_post(store, tmp_path, count=1)
    add_post(store, tmp_path, "5002", count=1, state="vector")
    host = make_host(tmp_path, store, services=make_services(), config={"enabled": True})
    host._pipeline_last_error = "上次失败"
    stats = host.pipeline_stats()
    assert stats["enabled"] is True
    assert stats["mode"] == "plugin"
    assert stats["queue"] == 0
    assert stats["pending"] == 0
    assert stats["posts"] == 1  # 5002 已全部 vector，不算缺口
    assert stats["media"]["total"] == 2
    assert stats["media"]["vector"] == 1
    assert stats["last_error"] == "上次失败"

    text = host.format_pipeline_status()
    assert "图床管线：已启用" in text
    assert "插件写入" in text
    assert "缺口帖子：1 篇" in text
    assert "最近错误：上次失败" in text
