"""游客 cookies 不再明文落 state.json（P2-6）。"""

from __future__ import annotations

import json
import os

from astrbot_plugin_weibo_suite.weibo.constants import VISITOR_FILE

COOKIES = {"SUB": "SECRET_SUB_VALUE", "SUBP": "SECRET_SUBP"}


def _plugin(make_weibo_plugin, data_dir):
    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    plugin._visitor_cookies = dict(COOKIES)
    plugin._visitor_ts = 1700000000.0
    plugin._visitor_blob = ""
    return plugin


def test_state_json_holds_no_cookie_values(make_weibo_plugin, data_dir):
    plugin = _plugin(make_weibo_plugin, data_dir)
    plugin._save_state()

    raw = (data_dir / "state.json").read_text(encoding="utf-8")
    assert "SECRET_SUB_VALUE" not in raw
    assert "SECRET_SUBP" not in raw
    assert "sub" not in json.loads(raw), "state.json 里不应再有 visitor 段"


def test_visitor_cookies_go_to_own_file(make_weibo_plugin, data_dir):
    plugin = _plugin(make_weibo_plugin, data_dir)
    plugin._save_state()

    path = data_dir / VISITOR_FILE
    assert path.is_file()
    blob = json.loads(path.read_text(encoding="utf-8"))
    assert blob["cookies"]["SUB"] == "SECRET_SUB_VALUE"
    assert blob["ts"] == 1700000000.0


def test_visitor_file_is_owner_only_on_posix(make_weibo_plugin, data_dir):
    plugin = _plugin(make_weibo_plugin, data_dir)
    plugin._save_state()

    if os.name == "nt":  # Windows 无 POSIX 权限位，chmod 是空操作
        return
    mode = (data_dir / VISITOR_FILE).stat().st_mode & 0o777
    assert mode == 0o600


def test_legacy_state_visitor_is_migrated_and_scrubbed(make_weibo_plugin, data_dir):
    """老库把 cookies 写在 state.json：读的时候要搬去独立文件并从 state 删掉。"""
    legacy = {
        "visitor": {"cookies": dict(COOKIES), "ts": 1600000000.0, "ua": "Mozilla/5.0"},
        "pending": [],
    }
    (data_dir / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    plugin._load_state()

    assert plugin._visitor_cookies.get("SUB") == "SECRET_SUB_VALUE"
    assert (data_dir / VISITOR_FILE).is_file()
    # 迁移完成后立刻回写，state.json 里不再残留 cookie
    plugin._save_state()
    assert "SECRET_SUB_VALUE" not in (data_dir / "state.json").read_text(encoding="utf-8")


def test_save_is_noop_when_blob_unchanged(make_weibo_plugin, data_dir, monkeypatch):
    """同样的身份反复存盘不该一直写文件（推送循环每轮都会走一遍）。"""
    plugin = _plugin(make_weibo_plugin, data_dir)
    plugin._save_state()
    calls = []
    real_replace = type(data_dir / VISITOR_FILE).replace

    def spy(self, target):
        calls.append(str(target))
        return real_replace(self, target)

    monkeypatch.setattr(type(data_dir / VISITOR_FILE), "replace", spy)
    plugin._save_state()
    assert [c for c in calls if c.endswith(VISITOR_FILE)] == [], "内容未变时不应重写 visitor 文件"
