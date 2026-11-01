"""覆盖图床客户端的 URL 与键值推导校验。

包括 JSON 路径取值、公开 URL 编码（保留斜杠），以及 OCI/Cloudflare/S3
的密钥与路径推导在同源、同桶、越界场景下的拒绝，和 S3 错误详情不做实体转义。
"""

from astrbot_plugin_weibo_suite.trace.image_bed import (
    ImageBedClient,
    _public_url,
    extract_json_path,
)
from astrbot_plugin_weibo_suite.trace.s3_store import _s3_error_detail, uri_encode_path


async def get_client():
    raise AssertionError("HTTP client should not be used in these tests")


def make_client(config, data_dir):
    return ImageBedClient(str(data_dir), config, get_client)


def test_extract_json_path_supports_nested_values_and_arrays():
    data = {"data": {"links": [{"url": "https://img.example.com"}]}}
    assert extract_json_path(data, "data.links.0.url") == "https://img.example.com"
    assert extract_json_path(data, "data.missing.0.url") is None


def test_public_url_encodes_object_key_but_preserves_slash():
    assert _public_url("https://img.example.com/", "a b/с+.jpg") == (
        "https://img.example.com/a%20b/%D1%81%2B.jpg"
    )


def test_oci_key_from_url_requires_same_origin_and_configured_bucket(tmp_path):
    client = make_client(
        {
            "mode": "oracle_oci",
            "oci_namespace": "namespace",
            "oci_region": "ap-osaka-1",
            "oci_bucket": "bucket",
            "oci_public_bucket": True,
        },
        tmp_path,
    )
    valid = (
        "https://objectstorage.ap-osaka-1.oraclecloud.com/n/namespace/b/bucket/o/a%20b.jpg?token=1"
    )
    key, matched = client._oci_key_from_url(valid)
    assert matched is True
    assert key == "a b.jpg"

    invalid = "https://evil.example.com/n/namespace/b/bucket/o/a.jpg"
    assert client._oci_key_from_url(invalid) == ("", False)


def test_src_path_for_cfb_uses_same_origin_and_path_only(tmp_path):
    client = make_client(
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
        },
        tmp_path,
    )
    assert client.src_path_for("https://img.example.com/file/a%20b.jpg?x=1") == "/file/a b.jpg"
    assert client.src_path_for("https://evil.example.com/file/a.jpg") == (
        "https://evil.example.com/file/a.jpg"
    )


async def test_generic_upload_failure_falls_back_to_local_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image-data")
    client = make_client({"mode": "generic_http"}, tmp_path / "plugin")

    async def fail_upload(_path):
        raise RuntimeError("https://secret.example/upload token=secret failed")

    monkeypatch.setattr(client, "_upload", fail_upload)
    stored = await client.store(str(source))

    assert stored.ok is True
    assert stored.url is None
    assert stored.file_path is not None
    assert "详情见日志" in stored.message
    assert "secret" not in stored.message
    assert stored.file_path.startswith(str(tmp_path / "plugin" / "images"))


async def test_cfb_delete_requires_same_origin_and_strips_query(tmp_path, monkeypatch):
    client = make_client(
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
            "cfi_token": "token",
        },
        tmp_path,
    )
    deleted = []

    async def fake_delete(token, file_id):
        deleted.append((token, file_id))
        return True

    monkeypatch.setattr(client, "_cf_delete", fake_delete)
    assert await client.delete_remote("https://img.example.com/file/a%20b.jpg?x=1")
    assert deleted == [("token", "a b.jpg")]
    assert await client.delete_remote("https://evil.example.com/file/a.jpg") is False


def test_s3_path_encoding_and_error_detail_refuse_entities():
    assert uri_encode_path("/bucket/a b+%.jpg") == "/bucket/a%20b%2B%25.jpg"
    malicious = b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Error/>'
    assert "<!ENTITY" in _s3_error_detail(malicious)


class _FakeResponse:
    """只实现 read_limited_text 用到的那点表面：status / headers / content.read。"""

    def __init__(self, status: int, body: str):
        self.status = status
        self.headers = {}
        self.content = self
        self._pending = [body.encode("utf-8")]

    async def read(self, _size: int) -> bytes:
        return self._pending.pop(0) if self._pending else b""

    def get_encoding(self) -> str:
        return "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeHttpClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, prepare=None, timeout=None):
        kwargs = prepare(url, method) if callable(prepare) else {}
        self.calls.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
        return self._responses.pop(0)


def _cf_client(tmp_path, fake, auth_code="SECRET123"):
    async def getter():
        return fake

    return ImageBedClient(
        str(tmp_path),
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
            "cfi_auth_code": auth_code,
        },
        getter,
    )


async def test_cf_upload_sends_auth_code_as_header_not_in_query(tmp_path):
    """鉴权码只能进请求头：出现在查询串就等于跟着 URL 进了访问日志与异常栈。"""
    src = tmp_path / "a.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0")
    fake = _FakeHttpClient([_FakeResponse(200, '[{"src": "/file/a.jpg"}]')])
    client = _cf_client(tmp_path, fake)

    url = await client._upload_cf(str(src))

    assert url == "https://img.example.com/file/a.jpg"
    assert len(fake.calls) == 1
    assert fake.calls[0]["headers"].get("X-Auth-Code") == "SECRET123"
    assert "authCode" not in fake.calls[0]["url"]
    assert "SECRET123" not in fake.calls[0]["url"]


async def test_cf_upload_falls_back_to_query_param_for_old_sites(tmp_path):
    """老站点只认 ?authCode=：被 401 拒一次后必须自动回落，而不是整单上传失败。"""
    src = tmp_path / "a.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0")
    fake = _FakeHttpClient(
        [
            _FakeResponse(401, '{"error":"unauthorized"}'),
            _FakeResponse(200, '[{"src": "/file/a.jpg"}]'),
        ]
    )
    client = _cf_client(tmp_path, fake)

    url = await client._upload_cf(str(src))

    assert url == "https://img.example.com/file/a.jpg"
    assert len(fake.calls) == 2
    assert "X-Auth-Code" in fake.calls[0]["headers"]
    assert "authCode=SECRET123" in fake.calls[1]["url"]
    # 回落只做一次，且不能把请求头也带上（否则等于两条路一起漏）
    assert "X-Auth-Code" not in fake.calls[1]["headers"]


async def test_cf_upload_does_not_fall_back_on_non_auth_errors(tmp_path):
    """500 不是鉴权问题，回落只会把错误信息冲掉。"""
    src = tmp_path / "a.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0")
    fake = _FakeHttpClient([_FakeResponse(500, "boom")])
    client = _cf_client(tmp_path, fake)

    import pytest

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await client._upload_cf(str(src))
    assert len(fake.calls) == 1
