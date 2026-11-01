"""归档 JSON 种子导入（archive_import_json）离线用例。

样例形如另一项目导出格式：{id, mb, c, t, lt, trunc, pics}。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.archive import ArchiveMixin

UID = "1000000001"


class FakePlugin(ArchiveMixin):
    """导入路径只需要数据目录与 store，不需要网络桩。"""

    def __init__(self, data_dir) -> None:
        self.config = {"archive": {"enabled": True, "download_originals": False}}
        self._data_dir = Path(data_dir)
        self._archive = None
        self._archive_dl_queue = None
        self._archive_worker_task = None
        self._backfill_stop = False
        self._backfill_task = None
        self._backfill_active = False


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
async def fake(data_dir):
    plugin = FakePlugin(data_dir)
    await plugin.archive_open()
    try:
        yield plugin
    finally:
        await plugin.archive_close()


SAMPLE = [
    {"id": "501", "mb": "Mb01", "c": "Sat Mar 08 16:51:30 +0800 2025", "t": "第一条", "lt": 0},
    {
        "id": "502",
        "mb": "Mb02",
        "c": "Sun Mar 09 08:00:00 +0800 2025",
        "t": "长文" * 100,
        "lt": 1,
        "trunc": 1,
        "pics": [
            {
                "pid": "p1",
                "url": "https://wx1.sinaimg.cn/large/p1.jpg",
                "ext": "jpg",
                "kind": "pic",
            }
        ],
    },
    {"t": "没有 id 的条目"},
]


async def test_import_json_counts_and_parses_text(fake, data_dir):
    file = write_json(data_dir / f"{UID}_original_full.json", SAMPLE)

    stats = await fake.archive_import_json(str(file))

    assert stats["uid"] == UID
    assert stats["total"] == 3
    assert stats["inserted"] == 2
    assert stats["updated"] == 0
    assert stats["skipped"] == 1
    assert stats["media_rows"] == 1
    assert Path(stats["file"]) == file

    store = fake.archive_store()
    assert store.count_posts(UID) == 2
    post = store.get_post("502")
    assert post["text"] == "长文" * 100
    assert post["is_long"] == 1
    assert post["source"] == "json"
    assert post["link"] == f"https://weibo.com/{UID}/Mb02"
    assert post["created_at"] > 1700000000
    # 没给 pic_total 时保持 -1（未知），不能被改写成实际图数
    assert post["pic_total"] == -1
    assert [pic["pid"] for pic in post["pics"]] == ["p1"]
    media = store.list_media("502")
    assert len(media) == 1
    assert media[0]["state"] == "pending"
    assert media[0]["url"] == "https://wx1.sinaimg.cn/large/p1.jpg"

    assert fake.format_import(stats).startswith(f"导入 {file}：uid {UID} / 共 3 条 / 新增 2")


async def test_import_json_meta_file_owns_uid(fake, data_dir):
    write_json(data_dir / "1111111_seed.json", [{"id": "601", "t": "meta 里的 uid"}])
    write_json(data_dir / "1111111.meta.json", {"uid": "2222222", "screenName": "测试号"})

    stats = await fake.archive_import_json("1111111_seed.json")

    assert stats["uid"] == "2222222"
    post = fake.archive_store().get_post("601")
    assert post["uid"] == "2222222"
    assert post["author"] == "测试号"


async def test_import_json_dict_wrapper_and_reimport(fake, data_dir):
    payload = {"uid": "3333333", "posts": [{"id": "601", "t": "包裹"}, {"id": "601", "t": "包裹"}]}
    file = write_json(data_dir / "dump.json", payload)

    stats = await fake.archive_import_json(str(file))

    assert stats["uid"] == "3333333"
    assert stats["total"] == 2
    assert stats["inserted"] == 1
    assert stats["updated"] == 1
    assert fake.archive_store().count_posts("3333333") == 1

    again = await fake.archive_import_json(str(file))

    assert again["inserted"] == 0
    assert again["updated"] == 2
    assert again["media_rows"] == 0
    assert fake.archive_store().count_posts("3333333") == 1


async def test_import_json_reports_errors(fake, data_dir):
    missing = await fake.archive_import_json(str(data_dir / "nope.json"))
    assert missing["error"].startswith("文件不存在")

    no_uid = write_json(data_dir / "plain.json", [{"id": "1", "t": "x"}])
    assert (await fake.archive_import_json(str(no_uid)))["error"] == "无法判定 uid"

    weird = write_json(data_dir / "weird.json", {"foo": 1})
    assert "列表" in (await fake.archive_import_json(str(weird)))["error"]


async def test_import_json_requires_ready_store(data_dir):
    plugin = FakePlugin(data_dir)
    file = write_json(data_dir / f"{UID}_seed.json", [{"id": "701", "t": "x"}])

    assert await plugin.archive_import_json(str(file)) == {"error": "归档未启用"}


async def test_import_json_handles_explicit_pic_total(fake, data_dir):
    file = write_json(
        data_dir / f"{UID}_pics.json",
        [
            {
                "id": "801",
                "t": "自述 3 图",
                "pic_total": 3,
                "pics": [
                    {"pid": "p1", "url": "https://wx1.sinaimg.cn/large/p1.jpg", "ext": "jpg"},
                    {"pid": "p2", "url": "https://wx1.sinaimg.cn/large/p2.jpg", "ext": "jpg"},
                ],
            }
        ],
    )

    await fake.archive_import_json(str(file))

    post = fake.archive_store().get_post("801")
    assert post["pic_total"] == 3
    assert len(post["pics"]) == 2
    report = await fake.archive_reconcile(UID)
    assert report["gap_counts"]["pic_count_mismatch"] == 1
