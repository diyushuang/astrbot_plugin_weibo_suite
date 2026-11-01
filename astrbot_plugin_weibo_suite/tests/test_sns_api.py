"""P8 面板契约：订阅域 7 条接口的入参/出参形状与安全边界。

这层是用户唯一不靠聊天指令就能操作订阅的入口，所以除了"能不能用"，还锁两件
事：**Cookie 永远不出现在响应里**，以及**非法入参一律 4xx 而不是落盘一个坏值**。
后者尤其重要——守卫参数写坏（比如把 `fetch_mode` 写成 `browsers`）会让整个
订阅域静默降级成 HTTP 通道，风控压力立刻上升。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from astrbot_plugin_weibo_suite.sns import constants as C
from astrbot_plugin_weibo_suite.views_backend import sns_api


class FakeConfig(dict):
    def save_config(self):
        return None


class FakePlugin:
    def __init__(self, data_dir):
        self.config = FakeConfig()
        self._data_dir = data_dir
        self.sns = None

    def _sessions(self):
        return ["aiocqhttp:GroupMessage:12345"]

    def sns_service(self):
        if self.sns is None:
            from astrbot_plugin_weibo_suite.sns.service import SnsService

            self.sns = SnsService(self)
        return self.sns


@pytest.fixture
def api(tmp_path, monkeypatch):
    """返回 (SnsAPI, FakeRequest 构造器)。json_response 直接透传 dict 便于断言。"""
    monkeypatch.setattr(sns_api, "json_response", lambda payload: payload)
    monkeypatch.setattr(
        sns_api,
        "error_response",
        lambda message, code=400: {"status": "error", "message": message, "code": code},
    )

    body: dict = {"payload": {}}

    class FakeRequest:
        @staticmethod
        async def json(default=None):
            return body["payload"]

    monkeypatch.setattr(sns_api, "web_request", FakeRequest)
    instance = sns_api.SnsAPI(FakePlugin(tmp_path), "suite", "v0.8.0")
    return instance, body


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 读


def test_overview_carries_everything_the_panel_renders(api):
    instance, _body = api
    out = _run(instance.overview())

    assert out["status"] == "ok"
    for key in ("accounts", "platforms", "guard", "latency", "browser", "events", "stats"):
        assert key in out, f"面板要渲染 {key}，接口必须给"
    assert json.dumps(out)  # 必须可序列化，否则前端拿到的是 500


def test_overview_lists_all_three_platforms_even_with_no_accounts(api):
    instance, _body = api
    out = _run(instance.overview())

    assert {row["platform"] for row in out["platforms"]} == set(C.PLATFORMS)
    assert out["accounts"] == []


def test_credentials_never_echo_a_cookie(api):
    """最难挽回的一类事故：面板把 Cookie 明文回显进浏览器历史/日志。"""
    instance, _body = api
    secret = "a1=AKDJFLAKSDJFLAKSDJF; web_session=0400698abcdef123456; acw_tc=xyz"
    instance.p.config["sns"] = {"xhs_cookie": secret}

    out = _run(instance.credentials())
    serialized = json.dumps(out, ensure_ascii=False)

    assert out["status"] == "ok"
    assert "0400698abcdef123456" not in serialized
    assert "AKDJFLAKSDJFLAKSDJF" not in serialized
    row = next(r for r in out["rows"] if r["platform"] == "xhs")
    assert row["cookie_configured"] is True
    assert row["missing_keys"] == []


def test_credentials_reports_missing_fields_without_values(api):
    instance, _body = api
    instance.p.config["sns"] = {"xhs_cookie": "a1=only-this"}

    out = _run(instance.credentials())
    row = next(r for r in out["rows"] if r["platform"] == "xhs")

    assert row["missing_keys"] == ["web_session"]
    assert "only-this" not in json.dumps(out)
    # 面板只需要知道「浏览器目录建了没」，不需要知道本机绝对路径
    assert isinstance(row["profile_ready"], bool)
    assert "profile_dir" not in row
    assert "sns" + os.sep + "browser" not in json.dumps(out)


def test_guard_get_returns_current_thresholds(api):
    instance, _body = api
    out = _run(instance.guard())

    assert out["status"] == "ok"
    assert out["guard"]["base_cooldown"] > 0
    assert set(out["guard"]) >= {"base_cooldown", "cooldown_cap", "health_pause", "health_degrade"}
    assert set(out["latency"]) == set(C.PLATFORMS)


# ------------------------------------------------------------------ 写


def test_guard_post_persists_changed_values(api):
    instance, body = api
    body["payload"] = {"base_interval_douyin": 600, "cooldown_base_seconds": 900}

    out = _run(instance.guard())

    assert out["status"] == "ok"
    assert set(out["changed"]) == {"base_interval_douyin", "cooldown_base_seconds"}
    assert instance.p.config["sns"]["base_interval_douyin"] == 600
    # 真的生效，而不只是存进了配置
    assert instance.service().base_interval("douyin") >= 600


def test_guard_post_rejects_bad_fetch_mode(api):
    instance, body = api
    body["payload"] = {"fetch_mode": "browsers"}

    out = _run(instance.guard())

    assert out["status"] == "error"
    assert "fetch_mode" in out["message"]
    assert "fetch_mode" not in instance.p.config.get("sns", {})


def test_guard_post_rejects_uncastable_int(api):
    instance, body = api
    body["payload"] = {"cooldown_base_seconds": "很久"}

    out = _run(instance.guard())

    assert out["status"] == "error"
    assert "cooldown_base_seconds" in out["message"]


def test_guard_post_ignores_unknown_keys(api):
    """未知键不能悄悄写进配置（AstrBot 下次启动会按 schema 删掉，等于假开关）。"""
    instance, body = api
    body["payload"] = {"hide_everything": True}

    out = _run(instance.guard())

    assert out["status"] == "error"
    assert "hide_everything" not in instance.p.config.get("sns", {})


def test_accounts_add_then_toggle_then_remove(api):
    instance, body = api
    body["payload"] = {
        "action": "add",
        "platform": "抖音",
        "target": "https://www.douyin.com/user/MS4wLjABAAAAtester",
    }
    out = _run(instance.accounts())
    assert out["status"] == "ok"

    account = next(iter(instance.service().state.accounts.values()))
    body["payload"] = {"action": "toggle", "key": account.key}
    out = _run(instance.accounts())
    assert out == {"status": "ok", "key": account.key, "enabled": False}

    body["payload"] = {"action": "toggle", "key": account.key}
    _run(instance.accounts())
    assert account.enabled is True
    # 重新启用要立刻排一次检查，否则用户得等一整个间隔
    assert account.next_check_ts > 0

    body["payload"] = {"action": "remove", "platform": "douyin", "key": account.key}
    out = _run(instance.accounts())
    assert out["status"] == "ok"
    assert instance.service().state.accounts == {}


def test_accounts_add_rejects_unknown_platform(api):
    instance, body = api
    body["payload"] = {"action": "add", "platform": "bilibili", "target": "https://x/1"}

    out = _run(instance.accounts())

    assert out["status"] == "error"


def test_accounts_add_rejects_link_that_is_not_a_profile(api):
    instance, body = api
    body["payload"] = {
        "action": "add",
        "platform": "douyin",
        "target": "https://www.douyin.com/video/7300000000000000000",
    }

    out = _run(instance.accounts())

    assert out["status"] == "error"
    assert instance.service().state.accounts == {}


def test_accounts_panel_payload_shape_uses_platform_and_account_id(api):
    """面板提交的是 `{platform, account_id}`：停用与移除都必须能定位到号。

    回归用例：面板按钮原本发 `account_id`，而接口只读 `key`，导致点「停用」
    永远回 404「没有这个订阅」、点「移除」永远回 400「缺少平台或账号」。
    """
    instance, body = api
    body["payload"] = {
        "action": "add",
        "platform": "douyin",
        "target": "https://www.douyin.com/user/MS4wLjABAAAAtester",
    }
    assert _run(instance.accounts())["status"] == "ok"

    account = next(iter(instance.service().state.accounts.values()))
    body["payload"] = {
        "action": "toggle",
        "platform": account.platform,
        "account_id": account.account_id,
    }
    out = _run(instance.accounts())
    assert out == {"status": "ok", "key": account.key, "enabled": False}

    body["payload"] = {
        "action": "remove",
        "platform": account.platform,
        "account_id": account.account_id,
    }
    out = _run(instance.accounts())
    assert out["status"] == "ok"
    assert instance.service().state.accounts == {}


def test_accounts_toggle_unknown_key_is_404(api):
    instance, body = api
    body["payload"] = {"action": "toggle", "key": "douyin:nope"}

    out = _run(instance.accounts())

    assert out["status"] == "error"
    assert out["code"] == 404


def test_accounts_unknown_action_is_rejected(api):
    instance, body = api
    body["payload"] = {"action": "explode"}

    out = _run(instance.accounts())

    assert out["status"] == "error"


def test_session_mark_records_login_state(api):
    instance, body = api
    body["payload"] = {"action": "mark", "platform": "instagram", "logged_in": True}

    out = _run(instance.session())

    assert out["status"] == "ok"
    assert instance.service().identity.login_state("instagram")["logged_in"] is True


def test_session_unknown_platform_is_rejected(api):
    instance, body = api
    body["payload"] = {"action": "mark", "platform": "myspace"}

    out = _run(instance.session())

    assert out["status"] == "error"


def test_preview_renders_template_without_sending(api):
    instance, body = api
    instance.p.config["sns"] = {"message_format": "{platform}|{name}|{text}"}
    body["payload"] = {"platform": "xhs"}

    out = _run(instance.preview())

    assert out["status"] == "ok"
    assert out["text"].startswith("小红书|")


def test_preview_reports_broken_template(api):
    instance, body = api
    instance.p.config["sns"] = {"message_format": "{wx} 坏了"}
    body["payload"] = {"platform": "douyin"}

    out = _run(instance.preview())

    assert out["status"] == "error"
    assert out["text"] == ""


def test_check_now_with_nothing_due_is_not_an_error(api):
    instance, body = api
    body["payload"] = {}

    out = _run(instance.check_now())

    assert out["status"] == "ok"
    assert out["checked"] == 0
    assert isinstance(out["detail"], list)
