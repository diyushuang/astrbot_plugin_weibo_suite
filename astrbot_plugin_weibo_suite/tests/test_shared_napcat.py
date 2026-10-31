"""NapCat error classification shared by album and trace delivery."""

from __future__ import annotations

from astrbot_plugin_weibo_suite.shared.napcat import (
    NapCatError,
    _b64_payload,
    _classify,
    _transient,
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
    assert _b64_payload(b"YWJj") == "base64://YWJj"


def test_napcat_error_preserves_flags():
    error = NapCatError("x", action_missing=True, file_unusable=True)
    assert error.action_missing is True
    assert error.file_unusable is True
