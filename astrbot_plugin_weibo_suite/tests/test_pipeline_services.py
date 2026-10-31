"""P4 轨道 A：图床目录 / 特征+缩略图 / 批量向量写入的离线契约测试。

三组能力都在「一次解码/一次往返」上做减法，这里用合成图片 + 假 HTTP 客户端
把契约固定下来：不连网、不碰真实图床与 Qdrant。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
from PIL import Image, UnidentifiedImageError

from astrbot_plugin_weibo_suite.trace import features, image_bed
from astrbot_plugin_weibo_suite.trace.features import (
    compute_features_with_mime,
    compute_features_with_thumbnail,
)
from astrbot_plugin_weibo_suite.trace.image_bed import ImageBedClient
from astrbot_plugin_weibo_suite.trace.vector_search import (
    BATCH_EMBED_CONCURRENCY,
    VectorEngine,
    VectorPointInput,
)

# ----------------------------------------------------------------------
# 公共替身
# ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.status = status
        self._body = body


class _FakeRequest:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeHttpClient:
    """只记录请求并回固定响应体；prepare 照常调用以构建 FormData。"""

    def __init__(self, body: str):
        self.requests: list[tuple] = []
        self._body = body

    async def request(self, method, url, *, prepare=None, timeout=None):
        kwargs = prepare(url, method, False) if prepare is not None else {}
        self.requests.append((method, url, kwargs))
        return _FakeRequest(_FakeResponse(self._body))


async def _fake_read_text(resp: _FakeResponse) -> str:
    return resp._body


class _FakeS3:
    def __init__(self):
        self.puts: list[tuple[str, str, str]] = []

    async def put_object(self, bucket, key, data, content_type):
        self.puts.append((bucket, key, content_type))


def _make_bed(config: dict, data_dir: Path, fake_client=None) -> ImageBedClient:
    async def get_client():
        if fake_client is None:
            raise AssertionError("这条用例不应该发起 HTTP 请求")
        return fake_client

    return ImageBedClient(str(data_dir), config, get_client)


def _save_image(path: Path, size=(80, 60), *, mode: str = "RGB", **save_kwargs) -> Path:
    color = (255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0)
    Image.new(mode, size, color).save(path, **save_kwargs)
    return path


def _make_engine(config: dict | None = None) -> VectorEngine:
    async def get_client():
        raise AssertionError("向量引擎用例不应该发起真实 HTTP 请求")

    return VectorEngine(config or {"vector_search": {}}, get_client)


def _enabled_engine() -> VectorEngine:
    return _make_engine(
        {
            "vector_search": {
                "qdrant_url": "https://qdrant.example.com",
                "qdrant_api_key": "secret",
                "collection_name": "imgbed_images",
                "embed_base_url": "https://embed.example.com/v1",
                "embed_model": "image-model",
            }
        }
    )


# ----------------------------------------------------------------------
# A1 store(folder=...)
# ----------------------------------------------------------------------


async def test_store_local_puts_file_under_folder_subdir(tmp_path):
    source = _save_image(tmp_path / "src.png", format="PNG")
    client = _make_bed({"mode": "local"}, tmp_path / "plugin")

    stored = await client.store(str(source), folder=" 相册/2026/ ")
    assert stored.ok is True
    assert stored.file_path is not None
    landed = Path(stored.file_path)
    assert landed.is_file()
    assert landed.parent == Path(client.local_dir) / "相册" / "2026"

    root = await client.store(str(source))
    assert root.ok is True
    assert Path(root.file_path).parent == Path(client.local_dir)


async def test_store_local_rejects_escaping_folder(tmp_path):
    source = _save_image(tmp_path / "src.jpg")
    client = _make_bed({"mode": "local"}, tmp_path / "plugin")

    cases = (("../evil", "上级目录"), ("/etc/passwd", "绝对路径"), ("C:\\evil", "绝对路径"))
    for folder, needle in cases:
        stored = await client.store(str(source), folder=folder)
        assert stored.ok is False, folder
        assert needle in stored.message
        assert stored.file_path is None
    assert not (tmp_path / "plugin" / "evil").exists()


async def test_store_cfb_folder_overrides_upload_folder_query(tmp_path, monkeypatch):
    source = _save_image(tmp_path / "src.jpg")
    fake = _FakeHttpClient(json.dumps([{"src": "/file/a b.jpg"}]))
    client = _make_bed(
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
            "cfi_upload_folder": "defaults",
        },
        tmp_path / "plugin",
        fake,
    )
    monkeypatch.setattr(image_bed, "read_limited_text", _fake_read_text)

    stored = await client.store(str(source), folder=" /thumbnails/ ")
    assert stored.ok is True
    assert stored.url == "https://img.example.com/file/a b.jpg"
    query = parse_qs(urlparse(fake.requests[0][1]).query)
    assert query["uploadFolder"] == ["thumbnails"]

    # folder 为空时回落到 config 的 cfi_upload_folder（旧行为）
    await client.store(str(source))
    query = parse_qs(urlparse(fake.requests[1][1]).query)
    assert query["uploadFolder"] == ["defaults"]


async def test_store_r2_folder_becomes_object_key_prefix(tmp_path, monkeypatch):
    source = _save_image(tmp_path / "src.jpg")
    s3 = _FakeS3()
    client = _make_bed(
        {
            "mode": "cloudflare_r2",
            "r2_public_base_url": "https://pub.example.com",
        },
        tmp_path / "plugin",
    )
    monkeypatch.setattr(client, "_r2_client", lambda: (s3, "bucket"))

    stored = await client.store(str(source), folder="thumbnails/")
    assert stored.ok is True
    assert s3.puts[0][0] == "bucket"
    assert s3.puts[0][1].startswith("thumbnails/trace_")
    assert s3.puts[0][1].endswith(".jpg")
    assert stored.url is not None
    assert stored.url.startswith("https://pub.example.com/thumbnails/trace_")

    await client.store(str(source))
    assert s3.puts[1][1].startswith("trace_")
    assert not s3.puts[1][1].startswith("thumbnails/")


async def test_store_oci_folder_becomes_object_key_prefix(tmp_path, monkeypatch):
    source = _save_image(tmp_path / "src.jpg")
    s3 = _FakeS3()
    client = _make_bed(
        {
            "mode": "oracle_oci",
            "oci_public_base_url": "https://cdn.example.com/prefix",
        },
        tmp_path / "plugin",
    )
    monkeypatch.setattr(client, "_oci_client", lambda: (s3, "bucket"))

    stored = await client.store(str(source), folder="thumbnails")
    assert stored.ok is True
    assert s3.puts[0][1].startswith("thumbnails/trace_")
    assert stored.url is not None
    assert stored.url.startswith("https://cdn.example.com/prefix/thumbnails/")


async def test_store_generic_http_ignores_folder(tmp_path, monkeypatch, caplog):
    source = _save_image(tmp_path / "src.jpg")
    fake = _FakeHttpClient(json.dumps({"data": {"url": "https://img.example.com/a.jpg"}}))
    client = _make_bed(
        {"mode": "generic_http", "api_url": "https://img.example.com/upload"},
        tmp_path / "plugin",
        fake,
    )
    monkeypatch.setattr(image_bed, "read_limited_text", _fake_read_text)

    with caplog.at_level(logging.DEBUG):
        stored = await client.store(str(source), folder="thumbnails")

    assert stored.ok is True
    assert stored.url == "https://img.example.com/a.jpg"
    method, url, _kwargs = fake.requests[0]
    assert method == "POST"
    assert url == "https://img.example.com/upload"
    assert "已忽略 folder" in caplog.text


async def test_store_upload_failure_with_bad_folder_falls_back_to_local_root(
    tmp_path, monkeypatch
):
    source = _save_image(tmp_path / "src.jpg")
    client = _make_bed(
        {"mode": "generic_http", "api_url": "https://img.example.com/upload"},
        tmp_path / "plugin",
    )

    async def fail_upload(*_args, **_kwargs):
        raise RuntimeError("图床 500")

    monkeypatch.setattr(client, "_upload", fail_upload)
    stored = await client.store(str(source), folder="../evil")

    assert stored.ok is True
    assert stored.file_path is not None
    assert Path(stored.file_path).parent == Path(client.local_dir)


# ----------------------------------------------------------------------
# A2 compute_features_with_thumbnail
# ----------------------------------------------------------------------


def test_features_match_with_mime_exactly(tmp_path):
    cases = (
        (_save_image(tmp_path / "a.jpg", (320, 200)), "image/jpeg"),
        (_save_image(tmp_path / "b.png", (320, 200), format="PNG"), "image/png"),
        (_save_image(tmp_path / "c.png", (320, 200), mode="RGBA", format="PNG"), "image/png"),
    )
    for source, expected_mime in cases:
        feats, mime, thumb = compute_features_with_thumbnail(source)
        assert mime == expected_mime
        assert feats == compute_features_with_mime(source)[0]
        assert thumb.startswith(b"\xff\xd8")


def test_thumbnail_scales_long_side_without_upscaling(tmp_path):
    big = _save_image(tmp_path / "big.png", (2400, 1200), format="PNG")
    _, mime, thumb = compute_features_with_thumbnail(big)
    assert mime == "image/png"
    with Image.open(io.BytesIO(thumb)) as image:
        assert image.format == "JPEG"
        assert image.size == (720, 360)

    small = _save_image(tmp_path / "small.png", (120, 60), format="PNG")
    _, _, thumb_small = compute_features_with_thumbnail(small)
    with Image.open(io.BytesIO(thumb_small)) as image:
        assert image.size == (120, 60)


def test_thumbnail_honours_max_side_and_quality(tmp_path):
    rng = np.random.default_rng(7)
    data = rng.integers(0, 256, size=(240, 320, 3), dtype=np.uint8)
    source = tmp_path / "noise.png"
    Image.fromarray(data).save(source)

    _, _, low = compute_features_with_thumbnail(source, max_side=100, quality=20)
    _, _, high = compute_features_with_thumbnail(source, max_side=100, quality=90)
    assert len(low) < len(high)
    with Image.open(io.BytesIO(low)) as image:
        assert max(image.size) == 100


def test_thumbnail_drops_exif_and_icc(tmp_path):
    source = tmp_path / "with_meta.jpg"
    image = Image.new("RGB", (400, 300), (10, 120, 200))
    exif = image.getexif()
    exif[0x010F] = "P4Camera"
    image.save(source, format="JPEG", exif=exif.tobytes(), icc_profile=b"\x01" * 128)

    with Image.open(source) as original:  # 原图确实带元数据，否则本用例没意义
        assert original.getexif()
        assert "icc_profile" in original.info

    _, _, thumb = compute_features_with_thumbnail(source)
    with Image.open(io.BytesIO(thumb)) as image:
        assert not image.getexif()
        assert "icc_profile" not in image.info


def test_thumbnail_loads_image_only_once(tmp_path, monkeypatch):
    source = _save_image(tmp_path / "once.jpg", (400, 300))
    calls = []
    real_open = features.Image.open

    def counting_open(*args, **kwargs):
        calls.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(features.Image, "open", counting_open)
    compute_features_with_thumbnail(source)
    assert len(calls) == 1


def test_thumbnail_rejects_garbage_bytes_like_with_mime(tmp_path):
    bogus = tmp_path / "bogus.jpg"
    bogus.write_bytes(b"not an image")

    with pytest.raises(UnidentifiedImageError):
        compute_features_with_mime(str(bogus))
    with pytest.raises(UnidentifiedImageError):
        compute_features_with_thumbnail(str(bogus))


def test_thumbnail_pixel_gate_matches_with_mime(tmp_path, monkeypatch):
    source = _save_image(tmp_path / "big.jpg", (40, 30))
    monkeypatch.setattr(features, "_MAX_LOAD_PIXELS", 100)

    with pytest.raises(ValueError) as mime_error:
        compute_features_with_mime(str(source))
    with pytest.raises(ValueError) as thumb_error:
        compute_features_with_thumbnail(str(source))
    assert str(thumb_error.value) == str(mime_error.value)


# ----------------------------------------------------------------------
# A3 VectorPointInput / upsert_file_batch
# ----------------------------------------------------------------------


async def test_upsert_file_batch_writes_every_point_in_one_put(tmp_path, monkeypatch):
    engine = _enabled_engine()
    embeds = []
    puts = []

    async def embed_file(path, *, mime=None):
        embeds.append((path, mime))
        return [0.1, float(len(embeds))]

    async def request_json(method, url, *, json_body=None, headers=None):
        puts.append((method, url, json_body, headers))
        return {"result": {}}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [
        VectorPointInput(
            path="/tmp/002.jpg",
            point_key="phash:aa",
            image_url="https://img.example.com/x/002.jpg",
            thumb_url="https://img.example.com/thumb/002.jpg",
            file_size=1234,
            width=800,
            height=600,
            mime="image/jpeg",
            src="/file/002.jpg",
        ),
        VectorPointInput(
            path=str(_save_image(tmp_path / "003.webp", format="WEBP")),
            point_key="phash:bb",
            image_url="https://img.example.com/x/003.webp",
        ),
    ]
    written = await engine.upsert_file_batch(entries)

    assert written == 2
    assert len(puts) == 1
    method, url, body, headers = puts[0]
    assert method == "PUT"
    assert url == "https://qdrant.example.com/collections/imgbed_images/points?wait=true"
    assert headers == {"api-key": "secret"}
    points = body["points"]
    assert [point["id"] for point in points] == [
        engine.point_id_for("phash:aa"),
        engine.point_id_for("phash:bb"),
    ]
    first, second = points[0]["payload"], points[1]["payload"]
    assert first["thumb_url"] == "https://img.example.com/thumb/002.jpg"
    assert "thumb_url" not in second  # 空值不写字段
    # 合同：file_name 取本地文件名
    assert first["file_name"] == "002.jpg"
    assert second["file_name"] == "003.webp"
    assert first["mime"] == "image/jpeg"
    assert second["mime"] == "image/webp"  # 未传 mime 时按扩展名推断
    assert first["src"] == "/file/002.jpg"
    assert second["src"] == second["image_url"]
    assert first["size_bytes"] == 1234
    assert second["size_bytes"] == 0
    assert first["channel"] == ""
    # 整批共用同一时间戳
    assert first["created_at"] == second["created_at"]
    # mime 有值时透传给 embed，避免 embed_file 再解一次图
    assert embeds[0][1] == "image/jpeg"
    assert embeds[1][1] is None


async def test_upsert_file_batch_dedupes_by_point_id(monkeypatch):
    engine = _enabled_engine()
    puts = []

    async def embed_file(path, *, mime=None):
        return [0.5]

    async def request_json(method, url, *, json_body=None, headers=None):
        puts.append(json_body)
        return {"result": {}}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [
        VectorPointInput("/tmp/a.jpg", "phash:same", "https://img.example.com/a.jpg"),
        VectorPointInput("/tmp/b.jpg", "phash:same", "https://img.example.com/b.jpg"),
    ]
    assert await engine.upsert_file_batch(entries) == 1
    assert len(puts[0]["points"]) == 1
    # 去重保留后写的那条
    assert puts[0]["points"][0]["payload"]["image_url"] == "https://img.example.com/b.jpg"


async def test_upsert_file_batch_empty_list_returns_zero(monkeypatch):
    engine = _enabled_engine()
    called = []

    async def request_json(method, url, *, json_body=None, headers=None):
        called.append(url)
        return {}

    monkeypatch.setattr(engine, "_request_json", request_json)
    assert await engine.upsert_file_batch([]) == 0
    assert called == []


async def test_upsert_file_batch_disabled_engine_returns_zero(monkeypatch, caplog):
    engine = _make_engine({"vector_search": {}})
    assert engine.enabled is False
    called = []

    async def embed_file(path, *, mime=None):
        called.append(path)
        return [0.1]

    async def request_json(*_args, **_kwargs):
        called.append("put")
        return {}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [VectorPointInput("/tmp/a.jpg", "phash:aa", "https://img.example.com/a.jpg")]
    with caplog.at_level(logging.DEBUG):
        assert await engine.upsert_file_batch(entries) == 0
    assert called == []
    assert "批量登记" in caplog.text


async def test_upsert_file_batch_caps_embed_concurrency(monkeypatch):
    engine = _enabled_engine()
    state = {"active": 0, "peak": 0}
    seen = []

    async def embed_file(path, *, mime=None):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        seen.append(path)
        try:
            await asyncio.sleep(0.01)
            return [0.1]
        finally:
            state["active"] -= 1

    async def request_json(method, url, *, json_body=None, headers=None):
        return {"result": {}}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [
        VectorPointInput(f"/tmp/{index}.jpg", f"phash:{index}", f"https://i/{index}.jpg")
        for index in range(9)
    ]
    assert await engine.upsert_file_batch(entries) == 9
    assert len(seen) == 9
    assert state["peak"] == BATCH_EMBED_CONCURRENCY == 4


async def test_upsert_file_batch_propagates_embed_failure(monkeypatch):
    engine = _enabled_engine()
    puts = []

    async def embed_file(path, *, mime=None):
        if path.endswith("bad.jpg"):
            raise RuntimeError("embed 服务 500")
        return [0.1]

    async def request_json(method, url, *, json_body=None, headers=None):
        puts.append(url)
        return {}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [
        VectorPointInput("/tmp/ok.jpg", "phash:ok", "https://i/ok.jpg"),
        VectorPointInput("/tmp/bad.jpg", "phash:bad", "https://i/bad.jpg"),
    ]
    with pytest.raises(RuntimeError, match="embed 服务 500"):
        await engine.upsert_file_batch(entries)
    assert puts == []  # 有图失败就不该写半批


async def test_upsert_file_batch_propagates_put_failure(monkeypatch):
    engine = _enabled_engine()

    async def embed_file(path, *, mime=None):
        return [0.1]

    async def request_json(method, url, *, json_body=None, headers=None):
        raise RuntimeError("qdrant 503")

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    entries = [VectorPointInput("/tmp/a.jpg", "phash:aa", "https://i/a.jpg")]
    with pytest.raises(RuntimeError, match="qdrant 503"):
        await engine.upsert_file_batch(entries)


async def test_upsert_file_keeps_legacy_payload_shape(monkeypatch):
    """单条通道行为不变：file_name 仍优先取直链文件名，且不写 thumb_url。"""
    engine = _enabled_engine()
    puts = []

    async def embed_file(path, *, mime=None):
        return [0.2, 0.4]

    async def request_json(method, url, *, json_body=None, headers=None):
        puts.append(json_body)
        return {}

    monkeypatch.setattr(engine, "embed_file", embed_file)
    monkeypatch.setattr(engine, "_request_json", request_json)

    await engine.upsert_file(
        "/tmp/本地.jpg",
        "phash:cc",
        "https://img.example.com/a%20b.jpg",
        12,
        10,
        20,
    )
    payload = puts[0]["points"][0]["payload"]
    assert payload["file_name"] == "a b.jpg"
    assert "thumb_url" not in payload
    assert set(payload) == {
        "src",
        "image_url",
        "file_name",
        "mime",
        "size_bytes",
        "width",
        "height",
        "created_at",
        "channel",
    }
