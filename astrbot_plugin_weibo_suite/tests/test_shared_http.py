"""Shared guarded download discipline."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.shared import http as shared_http
from astrbot_plugin_weibo_suite.shared.http import ResponseTooLargeError


class FakeContent:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        content_type: str = "image/jpeg",
        content_length: int | None = None,
        content_encoding: str = "",
    ):
        self.content = FakeContent(chunks)
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding
        self.released = False

    def release(self) -> None:
        self.released = True


async def _install_response(monkeypatch, response: FakeResponse) -> None:
    async def fake_guarded_request(*_args, **_kwargs):
        return response

    monkeypatch.setattr(shared_http, "guarded_request", fake_guarded_request)


async def test_stream_to_file_writes_atomically(tmp_path: Path, monkeypatch):
    response = FakeResponse([b"abc", b"def"], content_length=6)
    await _install_response(monkeypatch, response)
    target = tmp_path / "image.jpg"

    size = await shared_http.stream_to_file(
        object(), "GET", "https://example.com/a.jpg", target, max_bytes=10
    )

    assert size == 6
    assert target.read_bytes() == b"abcdef"
    assert not target.with_name("image.jpg.part").exists()
    assert response.released


async def test_stream_to_file_rejects_declared_oversize(tmp_path: Path, monkeypatch):
    response = FakeResponse([b"x"], content_length=11)
    await _install_response(monkeypatch, response)

    with pytest.raises(ResponseTooLargeError):
        await shared_http.stream_to_file(
            object(), "GET", "https://example.com/a.jpg", tmp_path / "a.jpg", max_bytes=10
        )
    assert not (tmp_path / "a.jpg").exists()


async def test_stream_to_file_rejects_actual_oversize_and_cleans_part(tmp_path: Path, monkeypatch):
    response = FakeResponse([b"123456", b"78901"])
    await _install_response(monkeypatch, response)
    target = tmp_path / "a.jpg"

    with pytest.raises(ResponseTooLargeError):
        await shared_http.stream_to_file(
            object(), "GET", "https://example.com/a.jpg", target, max_bytes=10
        )
    assert not target.exists()
    assert not target.with_name("a.jpg.part").exists()


async def test_stream_to_file_rejects_html_disguise(tmp_path: Path, monkeypatch):
    response = FakeResponse([b"<html>blocked</html>"], content_type="text/html; charset=utf-8")
    await _install_response(monkeypatch, response)

    with pytest.raises(ValueError, match="HTML"):
        await shared_http.stream_to_file(
            object(), "GET", "https://example.com/a.jpg", tmp_path / "a.jpg", max_bytes=100
        )


async def test_stream_to_file_detects_truncation(tmp_path: Path, monkeypatch):
    response = FakeResponse([b"abc"], content_length=10)
    await _install_response(monkeypatch, response)

    with pytest.raises(ValueError, match="截断"):
        await shared_http.stream_to_file(
            object(), "GET", "https://example.com/a.jpg", tmp_path / "a.jpg", max_bytes=100
        )
    assert not (tmp_path / "a.jpg").exists()
