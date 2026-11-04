"""覆盖 shared.common 与 shared.http 的公共辅助能力。

包括 QQ 图床域名的精确匹配与伪造后缀拒绝、错误文本的脱敏与截断，
以及 prepare_headers 在重定向时逐跳重算 Referer。
"""

from urllib.parse import urlparse

from astrbot_plugin_weibo_suite.shared.common import (
    ADDRESS_PLACEHOLDER,
    CREDENTIAL_PLACEHOLDER,
    is_qq_image_bed_host,
    sanitize_error_text,
)
from astrbot_plugin_weibo_suite.shared.http import prepare_headers


def test_qq_image_bed_host_matches_exact_and_real_subdomains():
    assert is_qq_image_bed_host("multimedia.nt.qq.com.cn")
    assert is_qq_image_bed_host("gchat.qpic.cn")
    assert is_qq_image_bed_host("MEDIA.GCHAT.QPIC.CN")
    assert is_qq_image_bed_host("multimedia.nt.qq.com.cn.")


def test_qq_image_bed_host_rejects_forged_suffixes_and_other_hosts():
    assert not is_qq_image_bed_host("eviltqpic.cn")
    assert not is_qq_image_bed_host("evil.nt.qq.com.cn.example.com")
    assert not is_qq_image_bed_host("qq.com.cn")
    assert not is_qq_image_bed_host("")


def test_referer_is_reevaluated_for_each_redirect_hop():
    def prepare(url, _method, _cross_origin):
        headers = {}
        if is_qq_image_bed_host(urlparse(url).hostname or ""):
            headers["Referer"] = "https://gchat.qpic.cn/"
        return {"headers": headers}

    qq_request = prepare_headers(
        prepare, "https://multimedia.nt.qq.com.cn/image", "GET", cross_origin=False
    )
    external_redirect = prepare_headers(
        prepare, "https://example.com/image", "GET", cross_origin=True
    )
    assert qq_request["headers"]["Referer"] == "https://gchat.qpic.cn/"
    assert "Referer" not in external_redirect["headers"]


def test_sanitize_error_text_hides_urls_and_ipv4():
    text = "请求失败: http://10.0.0.5:7658/random?token=secret 172.16.0.1:8080"
    safe = sanitize_error_text(text, limit=80)

    assert "10.0.0.5" not in safe
    assert "172.16.0.1" not in safe
    assert "secret" not in safe
    assert "<地址已隐藏>" in safe


def test_sanitize_error_text_truncates_and_defaults():
    assert sanitize_error_text("") == "未知错误（详情见机器人日志）"
    assert sanitize_error_text("x" * 300, limit=20).endswith("…")


def test_sanitize_error_text_hides_bare_secret_key_values_but_keeps_keys():
    """没有 scheme 的签名串也要打码：只替换值，键名保留便于定位。"""
    text = (
        "upload failed: X-Amz-Signature=deadbeefcafebabe "
        "x-amz-credential=AKIA/20261104 X-Amz-Security-Token=bigtokenbearer "
        "access_token=ghp_secretvalue api_key=sk-live-123456 "
        "refresh_token=rt_1234 pwd=hunter2 token=abcdef secret=s3cr3tvalue"
    )
    safe = sanitize_error_text(text, limit=300)

    for value in (
        "deadbeefcafebabe",
        "AKIA/20261104",
        "bigtokenbearer",
        "ghp_secretvalue",
        "sk-live-123456",
        "rt_1234",
        "hunter2",
        "abcdef",
        "s3cr3tvalue",
    ):
        assert value not in safe
    for key in (
        "X-Amz-Signature",
        "x-amz-credential",
        "X-Amz-Security-Token",
        "access_token",
        "api_key",
        "refresh_token",
        "pwd",
        "token",
        "secret",
    ):
        assert key in safe
    assert CREDENTIAL_PLACEHOLDER in safe


def test_sanitize_error_text_url_query_is_hidden_by_url_rule_only():
    """URL 内的同名参数由整条 URL 规则吃掉，不再被键值规则切碎。"""
    text = (
        "GET https://internal.example.com/v1/embed?"
        "X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=deadbeefcafebabe&X-Amz-Expires=60 失败"
    )
    safe = sanitize_error_text(text, limit=300)

    assert "X-Amz-Signature" not in safe
    assert "X-Amz-Credential" not in safe
    assert "internal.example.com" not in safe
    assert ADDRESS_PLACEHOLDER in safe
    assert CREDENTIAL_PLACEHOLDER not in safe
    assert "<地址已隐藏><凭据已隐藏>" not in safe


def test_sanitize_error_text_keeps_benign_and_short_key_values():
    """普通 key=value 与 1-3 字符短值不打码，避免诊断信息消失。"""
    text = "page=1 uid=12345 retry=3 key=value token=x token=abc sign=ab"
    assert sanitize_error_text(text, limit=200) == text


def test_sanitize_error_text_quoted_value_does_not_cross_into_next_text():
    text = 'password="hunter2hunter2" note="keepme" secret=\'abcd1234efgh\''
    safe = sanitize_error_text(text, limit=200)

    assert "hunter2hunter2" not in safe
    assert "abcd1234efgh" not in safe
    assert 'password="<凭据已隐藏>"' in safe
    assert "secret='<凭据已隐藏>'" in safe
    assert 'note="keepme"' in safe


def test_sanitize_error_text_mixed_url_and_bare_query_use_two_placeholders():
    text = (
        "image_url=https://bed.example.com/file/a.jpg?token=abc123123 "
        "access_token=ghp_secretvalue X-Amz-Signature=deadbeefcafebabe"
    )
    safe = sanitize_error_text(text, limit=300)

    assert ADDRESS_PLACEHOLDER in safe
    assert CREDENTIAL_PLACEHOLDER in safe
    assert "bed.example.com" not in safe
    assert "abc123123" not in safe
    assert "ghp_secretvalue" not in safe
    assert "deadbeefcafebabe" not in safe
    assert "access_token=" in safe
    assert "X-Amz-Signature=" in safe
