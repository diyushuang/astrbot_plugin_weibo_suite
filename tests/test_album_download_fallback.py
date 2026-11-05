"""相册下载候选回退、.part 清理与磁盘写线程化（审查 H1 / H2 回归）。

覆盖三类失败语义：瞬时故障换候选（含 alt_url）、鉴权/风控状态码直接终止、
全部候选失败统一抛 WeiboFetchError 并清理残件；另外直接验证流式下载把
open/write 丢进线程池、超限在写满之前停住。
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from yarl import URL

from astrbot_plugin_weibo_suite.weibo.parser import MediaTooLarge, WeiboFetchError

PRIMARY = "https://wx1.sinaimg.cn/large/p1.jpg"
ALT = "https://wx2.sinaimg.cn/original/p1.jpg"


def _pic(**overrides) -> dict:
    pic = {"pid": "p1", "url": PRIMARY, "alt_url": ALT, "ext": "jpg"}
    pic.update(overrides)
    return pic


def _http_error(status: int) -> aiohttp.ClientResponseError:
    info = aiohttp.RequestInfo(
        URL("https://wx1.sinaimg.cn/large/p1.jpg"),
        "GET",
        {},
        URL("https://wx1.sinaimg.cn/large/p1.jpg"),
    )
    return aiohttp.ClientResponseError(info, (), status=status, message=f"HTTP {status}")


async def _no_validate(_url: str) -> None:
    return None


def _part_of(dest):
    return dest.with_name(dest.name + ".part")


async def test_transient_failure_falls_back_to_alt_url(make_weibo_plugin, monkeypatch, tmp_path):
    """原图地址断连要自动换 alt_url，成功后不留 .part。"""
    plugin = make_weibo_plugin()
    tried: list[str] = []

    async def fake_stream(url, dest, **_kwargs):
        tried.append(url)
        if url == PRIMARY:
            raise aiohttp.ClientError("connection reset")
        await asyncio.to_thread(dest.write_bytes, b"x" * 2048)

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    dest = tmp_path / "p1.jpg"
    saved = await plugin._download_pic(_pic(), dest)

    assert saved == dest
    assert dest.read_bytes() == b"x" * 2048
    assert tried == [PRIMARY, ALT]
    assert not _part_of(dest).exists()


async def test_auth_error_aborts_without_trying_alt_url(make_weibo_plugin, monkeypatch, tmp_path):
    """403 是确定性拒绝：换地址只会继续触雷，必须当场终止。"""
    plugin = make_weibo_plugin()
    tried: list[str] = []

    async def fake_stream(url, dest, **_kwargs):
        tried.append(url)
        raise _http_error(403)

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    dest = tmp_path / "p1.jpg"
    with pytest.raises(WeiboFetchError):
        await plugin._download_pic(_pic(), dest)

    assert tried == [PRIMARY], "鉴权类错误不应继续尝试 alt_url"
    assert not _part_of(dest).exists()


async def test_404_falls_back_to_alt_url(make_weibo_plugin, monkeypatch, tmp_path):
    """404 是候选地址失效（签名过期），换 alt_url 仍值得一试。"""
    plugin = make_weibo_plugin()
    tried: list[str] = []

    async def fake_stream(url, dest, **_kwargs):
        tried.append(url)
        if url == PRIMARY:
            raise _http_error(404)
        await asyncio.to_thread(dest.write_bytes, b"y" * 2048)

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    dest = tmp_path / "p1.jpg"
    saved = await plugin._download_pic(_pic(), dest)

    assert saved == dest
    assert tried == [PRIMARY, ALT]


async def test_all_candidates_fail_raises_and_cleans_part(make_weibo_plugin, monkeypatch, tmp_path):
    """两个候选都失败：统一抛 WeiboFetchError，残件必须清掉。"""
    plugin = make_weibo_plugin()

    async def fake_stream(url, dest, **_kwargs):
        dest.write_bytes(b"half")  # 模拟已写半截
        raise WeiboFetchError("boom")

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    dest = tmp_path / "p1.jpg"
    with pytest.raises(WeiboFetchError):
        await plugin._download_pic(_pic(), dest)

    assert not dest.exists()
    assert not _part_of(dest).exists()


async def test_tiny_response_falls_back_to_alt_url(make_weibo_plugin, monkeypatch, tmp_path):
    """首个候选只回了不到 1KB 的占位内容，要继续换 alt_url。"""
    plugin = make_weibo_plugin()
    tried: list[str] = []

    async def fake_stream(url, dest, **_kwargs):
        tried.append(url)
        size = 500 if url == PRIMARY else 2048
        await asyncio.to_thread(dest.write_bytes, b"z" * size)

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    dest = tmp_path / "p1.jpg"
    saved = await plugin._download_pic(_pic(), dest)

    assert saved == dest
    assert dest.stat().st_size == 2048
    assert tried == [PRIMARY, ALT]


async def test_live_gif_transient_error_falls_back_to_none(
    make_weibo_plugin, monkeypatch, tmp_path
):
    """实况图视频段超时：返回 None 让上层回落封面静图，不留半成品。"""
    plugin = make_weibo_plugin()

    async def fake_stream(_url, _dest, **_kwargs):
        raise asyncio.TimeoutError("video timeout")

    monkeypatch.setattr(plugin, "_validate_media_url", _no_validate)
    monkeypatch.setattr(plugin, "_stream_to_file", fake_stream)

    pic = {"pid": "p1", "video_url": "https://f.video.weibocdn.com/p1.mp4"}
    result = await plugin._live_gif(pic, tmp_path)

    assert result is None
    assert not (tmp_path / "p1.mp4").exists()
    assert not (tmp_path / "p1.gif").exists()


class _FakeContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes], *, status: int = 200, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.content = _FakeContent(chunks)

    def raise_for_status(self):
        if self.status >= 400:
            raise _http_error(self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeHttp:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def get(self, *_args, **_kwargs):
        return self._response


async def test_stream_to_file_offloads_disk_writes_to_thread(
    make_weibo_plugin, monkeypatch, tmp_path
):
    """open/write 必须走线程池：否则慢盘上逐个 await 会拖住整个事件循环。"""
    plugin = make_weibo_plugin()
    plugin._blocked_until = 0.0

    async def no_pace():
        return None

    monkeypatch.setattr(plugin, "_media_pace", no_pace)
    plugin._http = _FakeHttp(_FakeResponse([b"a" * 2048]))

    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    def spy(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", ""))
        return real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    dest = tmp_path / "p1.jpg"
    got = await plugin._stream_to_file(PRIMARY, dest)

    assert got == 2048
    assert dest.read_bytes() == b"a" * 2048
    assert "open" in calls and "write" in calls


async def test_stream_to_file_stops_at_size_limit(make_weibo_plugin, monkeypatch, tmp_path):
    """超过上限立即停，不把剩余分块继续写盘。"""
    plugin = make_weibo_plugin()
    plugin._blocked_until = 0.0

    async def no_pace():
        return None

    monkeypatch.setattr(plugin, "_media_pace", no_pace)
    plugin._http = _FakeHttp(_FakeResponse([b"x" * 700, b"y" * 700]))

    dest = tmp_path / "big.jpg"
    with pytest.raises(MediaTooLarge):
        await plugin._stream_to_file(PRIMARY, dest, max_bytes=1000)

    assert dest.stat().st_size == 700, "超过上限后的分块不应再落盘"
