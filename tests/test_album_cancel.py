"""相册下载/转码的取消语义（P1-2 / P2-5 回归）。

两个缺陷都只在"任务被取消"时暴露：ffmpeg 子进程不回收会变成常驻满核孤儿，
下载批次把 CancelledError 当成"这张图跳过"会让调用方拿着半批目录继续上传。
"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_weibo_suite.weibo import album as album_mod


class FakeProc:
    """假 ffmpeg 进程：wait() 永不自然返回，kill() 才让它结束。"""

    def __init__(self, finish_on_kill: bool = True):
        self.returncode = None
        self.kills = 0
        self.waits = 0
        self._finish_on_kill = finish_on_kill
        self._release = asyncio.Event()

    async def wait(self):
        self.waits += 1
        if self.returncode is not None:
            return self.returncode
        await self._release.wait()
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9
        if self._finish_on_kill:
            self._release.set()


def _install_proc(monkeypatch, proc: FakeProc) -> None:
    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


async def test_to_gif_cancel_kills_child_and_returns_sem(make_weibo_plugin, monkeypatch, tmp_path):
    plugin = make_weibo_plugin()
    plugin._ffmpeg = "ffmpeg"
    proc = FakeProc()
    _install_proc(monkeypatch, proc)
    before = plugin._cpu_sem._value
    mp4, gif = tmp_path / "a.mp4", tmp_path / "a.gif"
    mp4.write_bytes(b"x")

    task = asyncio.create_task(plugin._to_gif(mp4, gif))
    await asyncio.sleep(0.02)  # 让它进到 wait_for(proc.wait())
    assert proc.waits == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.kills == 1, "取消路径必须 kill 掉 ffmpeg 子进程"
    assert proc.waits >= 2, "kill 之后仍要 wait，否则留下僵尸进程"
    assert proc.returncode == -9
    assert plugin._cpu_sem._value == before, "取消后并发槽必须还回去"


async def test_to_gif_timeout_kills_child_and_returns_false(
    make_weibo_plugin, monkeypatch, tmp_path
):
    plugin = make_weibo_plugin()
    plugin._ffmpeg = "ffmpeg"
    proc = FakeProc()
    _install_proc(monkeypatch, proc)
    monkeypatch.setattr(album_mod, "GIF_TRANSCODE_TIMEOUT", 0.01)
    before = plugin._cpu_sem._value

    ok = await plugin._to_gif(tmp_path / "a.mp4", tmp_path / "a.gif")

    assert ok is False
    assert proc.kills == 1
    assert proc.waits >= 2
    assert plugin._cpu_sem._value == before


async def test_download_pics_propagates_cancellation(make_weibo_plugin, monkeypatch):
    plugin = make_weibo_plugin(album_live_gif=False)
    assert plugin._ffmpeg in ("", None)

    async def fake_download(pic, path):
        raise asyncio.CancelledError

    monkeypatch.setattr(plugin, "_download_pic", fake_download)
    folder = plugin._album_tmp_root()
    folder.mkdir(parents=True, exist_ok=True)
    pics = [{"pid": "p1", "url": "http://example.com/1.jpg", "ext": "jpg"}]

    with pytest.raises(asyncio.CancelledError):
        await plugin._download_pics(pics, folder)
