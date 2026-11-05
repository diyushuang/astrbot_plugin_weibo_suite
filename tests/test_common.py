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


def test_sanitize_error_text_masks_added_whitelist_keys():
    text = (
        "client_secret=cs_abcd1234efgh authorization=deadbeefcafebabe "
        "x-api-key=xi_1234567890 session_token=st_abcdef123456 "
        "accesstoken=at_1234567890 cookie=ck_1234567890 "
        "set-cookie=sc_1234567890"
    )
    safe = sanitize_error_text(text, limit=400)

    for key in (
        "client_secret",
        "authorization",
        "x-api-key",
        "session_token",
        "accesstoken",
        "cookie",
        "set-cookie",
    ):
        assert key in safe
    for value in (
        "cs_abcd1234efgh",
        "deadbeefcafebabe",
        "xi_1234567890",
        "st_abcdef123456",
        "at_1234567890",
        "ck_1234567890",
        "sc_1234567890",
    ):
        assert value not in safe
    assert CREDENTIAL_PLACEHOLDER in safe


def test_sanitize_error_text_does_not_split_identifier_prefixed_keys():
    """前置边界不放宽：my_token / subtoken / not_a_token 不能被拦腰匹配。"""
    text = "my_token=abcdef123456 subtoken=deadbeef not_a_token=12345678"
    assert sanitize_error_text(text, limit=200) == text


def test_sanitize_error_text_weak_keys_keep_status_words_but_mask_credentials():
    kept = "auth=none auth=basic token=expired sign=invalid sign=positive token=x"
    assert sanitize_error_text(kept, limit=200) == kept

    hidden = "token=ghp_secretvalue sign=deadbeefcafebabe token=abc12345"
    safe = sanitize_error_text(hidden, limit=200)
    for value in ("ghp_secretvalue", "deadbeefcafebabe", "abc12345"):
        assert value not in safe
    assert "token=" in safe
    assert "sign=" in safe
    assert safe.count(CREDENTIAL_PLACEHOLDER) == 3


def test_sanitize_error_text_masks_authorization_scheme_and_token():
    """认证头的值带空格：整段（scheme + 令牌）都要消失，分隔符与键名保留。"""
    cases = {
        "authorization=Bearer eyJhbGciOiJIUzI1NiJ9.abc.def": "authorization=<凭据已隐藏>",
        "Authorization: Bearer abcdef123456token": "Authorization: <凭据已隐藏>",
        "AUTHORIZATION: BEARER eyJhbGciOiJIUzI1NiJ9.abc.def": "AUTHORIZATION: <凭据已隐藏>",
        "proxy-authorization=Basic dXNlcjpwYXNzd29yZA==": "proxy-authorization=<凭据已隐藏>",
        "authorization=Basic dXNlcjpwYXNzd29yZA==": "authorization=<凭据已隐藏>",
        'authorization: "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"': ('authorization: "<凭据已隐藏>"'),
    }
    for raw, expected in cases.items():
        safe = sanitize_error_text(raw, limit=200)
        assert safe == expected, safe
        assert "bearer" not in safe.casefold()
        assert "eyjhbGciO".casefold() not in safe.casefold()
        assert "dXNlcjpwYXNzd29yZA" not in safe


def test_sanitize_error_text_keeps_authorization_diagnostics():
    """没有受支持 scheme + 令牌的形态是排障信息，不能误打码。"""
    text = "authorization: failed auth: failed authorization=disabled Authorization: None auth=none"
    assert sanitize_error_text(text, limit=200) == text


def test_sanitize_error_text_masks_authorization_inside_url():
    """URL 查询串里的认证头不能只遮半截，也不能留下叠加的畸形占位符。"""
    cases = {
        "https://h.example.com/x?authorization=Bearer TOKEN failed": ("<地址已隐藏> failed"),
        "https://h.example.com/x?authorization=Basic dXNlcjpwYXNz failed": ("<地址已隐藏> failed"),
        "https://h.example.com/x?authorization=Bearer%20TOKEN failed": ("<地址已隐藏> failed"),
        "https://h.example.com/x?authorization=Bearer TOKEN&x=1": "<地址已隐藏>&x=1",
        "https://h.example.com/x?a=1&authorization=Basic dXNlcjpwYXNz": "<地址已隐藏>",
        "  https://h.example.com/x?authorization=Bearer TOKEN": "<地址已隐藏>",
        "GET https://h.example.com/x failed authorization=Bearer TOKEN 后继续": (
            "GET <地址已隐藏> failed authorization=<凭据已隐藏> 后继续"
        ),
    }
    for raw, expected in cases.items():
        safe = sanitize_error_text(raw, limit=300)
        assert safe == expected, safe
        assert "TOKEN" not in safe
        assert "dXNlcjpwYXNz" not in safe
        assert "<地址已隐藏><凭据已隐藏>" not in safe


def test_sanitize_error_text_masks_cookie_header_pairs():
    """Cookie 头只遮凭据值，非密钥键与分号/空格风格原样保留。"""
    raw = "Cookie: session=abc123; theme=dark; token=xyz"
    safe = sanitize_error_text(raw, limit=200)
    assert "abc123" not in safe
    assert "session=<凭据已隐藏>" in safe
    assert "theme=dark" in safe
    assert safe.startswith("Cookie: ")

    spaced = "Cookie: session = abc123 ; theme = dark"
    spaced_safe = sanitize_error_text(spaced, limit=200)
    assert "abc123" not in spaced_safe
    assert "session = <凭据已隐藏>" in spaced_safe
    assert "theme = dark" in spaced_safe

    quoted = 'Cookie: session="abc123"; theme=dark'
    quoted_safe = sanitize_error_text(quoted, limit=200)
    assert 'session="<凭据已隐藏>"' in quoted_safe
    assert "theme=dark" in quoted_safe


def test_sanitize_error_text_masks_set_cookie_and_keeps_attributes():
    """Set-Cookie 的 Path/HttpOnly/Expires 是属性，不是凭据；session 不能漏。"""
    raw = "Set-Cookie: session=xyz; Path=/; HttpOnly; Expires=Wed, 21 Oct 2015 07:28:00 GMT"
    safe = sanitize_error_text(raw, limit=300)
    assert "xyz" not in safe
    assert "session=<凭据已隐藏>" in safe
    assert "Path=/" in safe
    assert "HttpOnly" in safe
    assert "Expires=Wed, 21 Oct 2015 07:28:00 GMT" in safe


def test_sanitize_error_text_session_whitelist_uses_weak_key_rules():
    """裸 session= 与 session_token 并列，按弱键判据：真凭据遮、状态词保留。"""
    hidden = "session=abcdef123456 session=abc123"
    safe = sanitize_error_text(hidden, limit=200)
    for value in ("abcdef123456", "abc123"):
        assert value not in safe
    assert safe.count(CREDENTIAL_PLACEHOLDER) == 2
    assert "session=" in safe

    kept = "session=short session=ready"
    assert sanitize_error_text(kept, limit=200) == kept

    # session_token 仍然生效，且不会被 session 分支重复替换
    token_safe = sanitize_error_text("session_token=st_abcdef123456", limit=200)
    assert token_safe == "session_token=<凭据已隐藏>"

    # 无冒号的 cookie= 走通用规则，整段值（含内层 sessionid=）一起遮
    cookie_safe = sanitize_error_text("cookie=sessionid=abcdef123456", limit=200)
    assert cookie_safe == "cookie=<凭据已隐藏>"
