"""相册与 trace 投递共用的 NapCat 错误分类。"""

from __future__ import annotations

from astrbot_plugin_weibo_suite.shared.napcat import (
    NapCatError,
    _classify,
    _transient,
    b64_payload,
)


def test_transient_classifier_recognizes_gateway_error_and_http_status():
    assert _transient("HTTP error! status: 502")
    assert _transient("请求失败", OSError("connection reset"))


def test_classifier_marks_missing_action_and_file_problem():
    text, missing, unusable = _classify("不支持的API upload_image_to_qun_album")
    assert "v4.8.101" in text
    assert missing is True
    assert unusable is False

    text, missing, unusable = _classify("ENOENT: no such file or directory")
    assert text == "ENOENT: no such file or directory"
    assert missing is False
    assert unusable is True


def test_base64_payload_contract():
    assert b64_payload(b"abc") == "base64://YWJj"
    assert b64_payload(b"abc", prefix=False) == "YWJj"


def test_napcat_error_preserves_flags():
    error = NapCatError("x", action_missing=True, file_unusable=True)
    assert error.action_missing is True
    assert error.file_unusable is True
