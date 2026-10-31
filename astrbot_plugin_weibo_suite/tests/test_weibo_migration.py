"""P2 Weibo-domain split and command-surface regression."""

from __future__ import annotations

import ast
import time
from pathlib import Path

from astrbot_plugin_weibo_suite.shared.common import (
    album_rule_token,
    cooldown_remaining_minutes,
    keyword_rules_upsert,
    merge_response_cookies,
    parse_cookie_string,
    parse_umo,
    redact_proxy,
)
from astrbot_plugin_weibo_suite.weibo import parser
from astrbot_plugin_weibo_suite.weibo.plugin import WeiboMonitorPlugin


def test_weibo_domain_has_expected_modules():
    root = Path(__file__).resolve().parents[1] / "weibo"
    expected = {
        "album.py",
        "api.py",
        "commands.py",
        "constants.py",
        "monitor.py",
        "parser.py",
        "plugin.py",
        "pusher.py",
        "runtime.py",
        "visitor.py",
    }
    assert expected <= {path.name for path in root.glob("*.py")}


# P2 平移的 17 条微博指令：只保证不丢，不锁死总数。
# P3 归档又加了 6 条，P4/P5 还会继续加；把总数写死会让每次扩展都踩红。
LEGACY_WEIBO_COMMANDS = {
    "weibo_help",
    "weibo_bind",
    "weibo_unbind",
    "weibo_add",
    "weibo_remove",
    "weibo_list",
    "weibo_album_bind",
    "weibo_album_unbind",
    "weibo_album_rules_cmd",
    "weibo_album_list",
    "weibo_keyword_album",
    "weibo_keyword_album_remove",
    "weibo_blogger_keyword_album",
    "weibo_blogger_keyword_album_remove",
    "weibo_keyword_album_list",
    "weibo_check",
    "weibo_status",
}


def test_all_legacy_weibo_commands_are_preserved():
    root = Path(__file__).resolve().parents[1] / "weibo"
    tree = ast.parse((root / "commands.py").read_text(encoding="utf-8"))
    methods: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            text = ast.unparse(decorator)
            if text.startswith("weibo.command("):
                methods.add(node.name)
    missing = LEGACY_WEIBO_COMMANDS - methods
    assert not missing, f"P2 平移的微博指令在后续扩展中丢失: {sorted(missing)}"


def test_mixins_compose_cooperatively(make_weibo_plugin):
    plugin = make_weibo_plugin(monitored_uids=["123456"], push_sessions=["aiocqhttp:group:1"])
    assert isinstance(plugin, WeiboMonitorPlugin)
    assert plugin._sessions() == ["aiocqhttp:group:1"]
    plugin._sync_accounts_from_config()
    assert "123456" in plugin.accounts


def test_post_parser_and_message_rendering(make_weibo_plugin):
    plugin = make_weibo_plugin(message_format="【{name}】{weibo}\n{link}", text_max_length=100)
    post = plugin._parse_post(
        {
            "id": "501",
            "bid": "AbCd",
            "created_at": "Sat Oct 31 12:00:00 +0800 2026",
            "text": "测试 [doge] #话题#",
            "user": {"id": 123456, "screen_name": "博主"},
            "pic_ids": ["p1"],
            "pic_num": 1,
            "pics": [
                {
                    "pid": "p1",
                    "large": {"url": "https://wx1.sinaimg.cn/large/p1.jpg"},
                }
            ],
        },
        "123456",
    )
    assert post is not None
    assert post["id"] == "501"
    assert post["pics"][0]["url"] == "https://wx1.sinaimg.cn/large/p1.jpg"
    assert post["pics"][0]["pid"] == "p1"
    assert "[doge]" in post["text"]
    assert "博主" in plugin._build_message(post)

    plugin.config["weibo_emoji_translate"] = True
    assert plugin._post_body(post) == "测试 🐶 #话题#"


def test_parser_pure_helpers():
    assert parser.translate_weibo_emojis("[doge][unknown]") == "🐶[unknown]"
    assert parser.extract_hashtags("#甲# 文本 #乙# #甲#") == ["甲", "乙"]
    assert parser._clean_text("  a\n\n\n b  ", 20) == "a\n\n b"


def test_normalize_item_backfills_old_state_shape():
    item = {"post_id": "1", "_send_active": True, "pics": None, "videos": None}
    normalized = WeiboMonitorPlugin._normalize_item(item)
    assert normalized["pics"] == []
    assert normalized["videos"] == []
    assert normalized["video_files"] == []
    assert normalized["raw_text"] == ""
    assert "_send_active" not in WeiboMonitorPlugin._dump_item(normalized)


async def test_help_command_remains_callable(make_weibo_plugin, make_event):
    plugin = make_weibo_plugin()
    event = make_event("微博帮助")
    results = [result async for result in plugin.weibo_help(event)]
    assert results and "微博监控指令" in results[0].text


def test_shared_common_single_source_helpers():
    assert redact_proxy("http://user:pass@example.com:8080") == "http://***@example.com:8080"
    assert parse_umo("aiocqhttp:GroupMessage:123") == ("aiocqhttp", "GroupMessage", "123")
    assert parse_umo("broken") == ("", "", "")
    assert parse_cookie_string("A=1; B=2; broken") == {"A": "1", "B": "2"}
    assert album_rule_token("10", "20", "相册:精选") == "10:20:相册:精选"
    assert cooldown_remaining_minutes(160, now=100) == 1
    assert cooldown_remaining_minutes(100, now=100) == 0

    rules = [(None, "福利", "1", "旧")]
    assert keyword_rules_upsert(rules, None, "福利", "1", "新") is True
    assert rules == [(None, "福利", "1", "新")]
    assert keyword_rules_upsert(rules, "9", "福利", "1", "博主") is False
    assert len(rules) == 2


def test_merge_response_cookies_ignores_foreign_domains():
    class Morsel(dict):
        value = "v"

    class Response:
        def __init__(self):
            self.cookies = {
                "SUB": Morsel(domain=".weibo.cn"),
                "FOREIGN": Morsel(domain=".example.com"),
                "LOCAL": Morsel(),
            }

    target = {"OLD": "x"}
    merge_response_cookies(target, Response())
    assert target == {"OLD": "x", "SUB": "v", "LOCAL": "v"}


async def test_video_completion_uses_debounced_state_save(make_weibo_plugin, data_dir, monkeypatch):
    from astrbot_plugin_weibo_suite.weibo import plugin as plugin_module

    plugin = make_weibo_plugin()
    plugin._data_dir = data_dir
    monkeypatch.setattr(plugin_module, "STATE_SAVE_DEBOUNCE", 0.01)
    plugin._save_state_debounced()
    plugin._save_state_debounced()
    assert plugin._state_dirty is True
    await plugin._state_save_task
    assert plugin._state_dirty is False
    assert (data_dir / "state.json").is_file()


async def test_at_all_notice_ttl_cleanup(make_weibo_plugin):
    plugin = make_weibo_plugin()
    now = time.time()
    plugin._at_all_notices = {"expired": 0.0, "fresh": now}
    plugin._note_at_all("ok", "普通推送", log=False)
    assert "expired" not in plugin._at_all_notices
    assert "fresh" in plugin._at_all_notices


def test_composite_exposes_both_domains(make_weibo_plugin):
    from astrbot_plugin_weibo_suite.composite import WeiboSuitePlugin
    from astrbot_plugin_weibo_suite.tests.conftest import FakeConfig, _Context

    plugin = WeiboSuitePlugin(_Context(), FakeConfig(monitored_uids=["123456"]))
    assert plugin._trace_plugin is not None
    assert hasattr(plugin, "weibo_help")
    assert hasattr(plugin, "trace")
    assert hasattr(plugin, "checkin")
    assert hasattr(plugin, "send_random_media")
