"""`/溯源重扫` 的批量并发与 pHash 去重回归。"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_weibo_suite.trace import library_mixin


def _fake_features_factory(lock: threading.Lock, state: dict):
    """用可控 sleep 的假特征计算，观察批次内是否真的并发。"""

    def fake(path: str, hash_size: int):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        data = Path(path).read_bytes()
        with lock:
            state["active"] -= 1
        digest = hashlib.sha256(data).hexdigest()
        return SimpleNamespace(
            phash=digest,
            dhash=digest[:16],
            ahash=digest[:16],
            width=1,
            height=1,
        )

    return fake


async def test_rescan_runs_bounded_concurrency_and_dedupes_phash(
    make_plugin, make_event, tmp_path, monkeypatch
):
    """重扫应并发解码（峰值 ≥ 2），且同内容的文件只入库一次。"""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    for index in range(3):
        (scan_dir / f"{index}.jpg").write_bytes(f"image-{index}".encode())
    (scan_dir / "copy.jpg").write_bytes((scan_dir / "0.jpg").read_bytes())

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    monkeypatch.setattr(library_mixin, "compute_features", _fake_features_factory(lock, state))

    plugin = make_plugin(scan_dirs=[str(scan_dir)])
    event = make_event("/溯源重扫")

    results = [item async for item in plugin.rescan(event)]

    text = results[-1].text
    assert "共 4 张" in text
    assert "新增 3" in text
    assert "跳过 1" in text
    assert "失败 0" in text
    assert plugin.library.count() == 3
    assert state["peak"] >= 2, "重扫应在批次内并发解码"
    # 进度必须走 yield（event.send 只接受 MessageChain，不能传 MessageEventResult）
    assert any("开始扫描" in item.text for item in results), "应 yield 开始进度"
    assert not event.sent


async def test_parse_force_flag_only_accepts_the_keyword_itself(make_plugin):
    """开关必须是指令之后的一个独立词：目录名里的 force、forceful 都不算。"""
    plugin = make_plugin()

    assert plugin._parse_force_flag("/溯源重扫 force") is True
    assert plugin._parse_force_flag("/溯源重扫 强制") is True
    assert plugin._parse_force_flag("/溯源重扫，重建") is True
    assert plugin._parse_force_flag("/溯源重扫 force。") is True

    assert plugin._parse_force_flag("/溯源重扫") is False
    assert plugin._parse_force_flag("/溯源重扫 forceful") is False
    assert plugin._parse_force_flag("/溯源重扫 unforced") is False
    # 本次修复的原始触发条件：路径里带 force 不再被当成重建开关
    assert plugin._parse_force_flag(r"/溯源重扫 D:\force\pics") is False
    assert plugin._parse_force_flag("/溯源重扫 force 顺便看看") is False
    assert plugin._parse_force_flag("") is False
    assert plugin._parse_force_flag(None) is False


async def test_rescan_force_replaces_scan_rows_in_one_transaction(
    make_plugin, make_event, tmp_path, monkeypatch
):
    """强制重建要整体替换 scan 来源：失效行消失、新行入库、register 行不动。"""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    for index in range(2):
        (scan_dir / f"{index}.jpg").write_bytes(f"image-{index}".encode())

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    monkeypatch.setattr(library_mixin, "compute_features", _fake_features_factory(lock, state))

    plugin = make_plugin(scan_dirs=[str(scan_dir)])
    plugin.library.add_many(
        [
            {
                "phash": "ab" * 32,
                "dhash": "ab" * 8,
                "ahash": "ab" * 8,
                "width": 1,
                "height": 1,
                "file_size": 1,
                "image_url": "",
                "file_path": str(scan_dir / "gone.jpg"),
                "source": "scan",
                "note": "gone.jpg",
            },
            {
                "phash": "cd" * 32,
                "dhash": "cd" * 8,
                "ahash": "cd" * 8,
                "width": 1,
                "height": 1,
                "file_size": 1,
                "image_url": "",
                "file_path": "C:/kept.jpg",
                "source": "register",
                "note": "kept.jpg",
            },
        ]
    )

    results = [item async for item in plugin.rescan(make_event("/溯源重扫 force"))]

    text = results[-1].text
    assert "新增 2" in text
    assert {Path(p).name for p in plugin.library.get_paths_for_source("scan")} == {
        "0.jpg",
        "1.jpg",
    }
    # 强制重建只碰 scan 来源，登记的原图不能被顺手删掉
    assert plugin.library.get_paths_for_source("register") == {"C:/kept.jpg"}
    assert plugin.library.count() == 3
