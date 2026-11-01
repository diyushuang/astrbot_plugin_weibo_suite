"""P8 数据类型、状态文件与凭据存储。

这三块是"能不能安全落盘"的问题：状态文件要能容忍半截 JSON（进程被 kill 过），
Cookie 只能以脱敏形态出现在任何人类可见的地方（日志、面板、错误信息）。
"""

from __future__ import annotations

import json
import os

from astrbot_plugin_weibo_suite.sns import constants as C
from astrbot_plugin_weibo_suite.sns.identity import (
    IdentityStore,
    identity_status,
    missing_required_keys,
    parse_cookie_string,
    redact_cookie,
)
from astrbot_plugin_weibo_suite.sns.state import SnsState
from astrbot_plugin_weibo_suite.sns.types import (
    FetchResult,
    SnsAccount,
    SnsPost,
    account_key,
)

# ---------------------------------------------------------------- types


def test_account_key_and_dedupe_key_are_stable():
    assert account_key("douyin", "abc") == "douyin:abc"
    post = SnsPost(platform="xhs", account_id="u1", post_id="n1")
    assert post.dedupe_key == "xhs:u1:n1"
    # 同一个 post 反复构造，去重键必须一致（否则会重复推送）
    assert post.dedupe_key == SnsPost(platform="xhs", account_id="u1", post_id="n1").dedupe_key


def test_post_body_falls_back_to_title_and_truncates():
    item = SnsPost(platform="douyin", account_id="a", post_id="1", title="只有标题")
    assert item.body() == "只有标题"
    long = SnsPost(platform="douyin", account_id="a", post_id="2", text="字" * 500)
    assert len(long.body(text_max=50)) == 51  # 50 + 省略号
    assert "\n" not in SnsPost(platform="douyin", account_id="a", post_id="3", text="a\n b").body()


def test_post_round_trips_through_dict():
    item = SnsPost(
        platform="instagram",
        account_id="someone",
        post_id="C1",
        title="t",
        text="body",
        link="https://instagram.com/p/C1/",
        ts=1730000000,
        author="someone",
        images=["https://a/1.jpg"],
        videos=["https://a/v.mp4"],
        kind="video",
    )
    assert SnsPost.from_dict(item.to_dict()) == item


def test_fetch_result_success_marks_empty_and_failure_keeps_signal():
    ok = FetchResult.success([SnsPost(platform="douyin", account_id="a", post_id="1")])
    assert ok.ok is True
    assert ok.signal == ""
    empty = FetchResult.success([])
    assert empty.ok is True
    assert empty.signal == "ok.empty"
    boom = FetchResult.failure("douyin.empty_silent", "空响应")
    assert boom.ok is False
    assert boom.signal == "douyin.empty_silent"
    assert "只拿到空内容" in boom.label or "空" in boom.label


def test_account_due_and_cooling():
    account = SnsAccount(platform="douyin", account_id="a", next_check_ts=100)
    assert account.due(200) is True
    account.paused = True
    assert account.due(200) is False
    account.paused = False
    account.cooldown_until = 300
    assert account.cooling(200) is True
    assert account.due(200) is False
    assert account.due(400) is True


def test_account_seen_ids_are_deduped_and_trimmed():
    account = SnsAccount(platform="douyin", account_id="a")
    for i in range(C.SEEN_IDS_LIMIT + 10):
        account.mark_seen(f"p{i}")
    assert len(account.seen_ids) == C.SEEN_IDS_LIMIT
    # 最新的那条一定还在，最老的已经被挤掉
    assert account.seen(f"p{C.SEEN_IDS_LIMIT + 9}")
    assert account.seen("p0") is False
    account.mark_seen("p0")
    account.mark_seen("p0")
    assert account.seen_ids.count("p0") == 1


def test_account_round_trips_and_tolerates_missing_fields():
    account = SnsAccount(platform="xhs", account_id="u1", name="博主", seen_ids=["a", "b"])
    restored = SnsAccount.from_dict(account.to_dict())
    assert restored.key == account.key
    assert restored.seen_ids == ["a", "b"]
    bare = SnsAccount.from_dict({"platform": "rss", "account_id": "https://x/feed"})
    assert bare.platform == "rss"
    assert bare.enabled is True


# ---------------------------------------------------------------- state


def test_state_save_and_load_round_trip(tmp_path):
    state = SnsState(tmp_path)
    account = SnsAccount(platform="douyin", account_id="a", name="博主")
    state.add(account)
    state.record("info", platform="douyin", name="博主", text="hello")
    state.bump("douyin", "check")
    state.platform_paused_until["douyin"] = 12345.0
    state.save()

    restored = SnsState(tmp_path)
    restored.load()
    assert restored.get("douyin:a") is not None
    assert restored.get("douyin", "a") is not None
    assert restored.get("douyin:a").name == "博主"
    assert restored.events[0]["text"] == "hello"
    assert restored.stats["by_platform"]["douyin"]["check"] == 1
    assert restored.platform_paused_until["douyin"] == 12345.0


def test_state_file_is_owner_only(tmp_path):
    state = SnsState(tmp_path)
    state.add(SnsAccount(platform="douyin", account_id="a"))
    state.save()
    path = tmp_path / C.SNS_STATE_FILE
    assert path.exists()
    if os.name != "nt":  # Windows 上 chmod 语义不同，不做断言
        assert (path.stat().st_mode & 0o777) == 0o600


def test_state_load_tolerates_corrupt_file(tmp_path):
    (tmp_path / C.SNS_STATE_FILE).write_text("{ not json", encoding="utf-8")
    state = SnsState(tmp_path)
    state.load()  # 不抛异常
    assert state.accounts == {}
    assert state.stats["check"] == 0


def test_state_events_are_capped_and_truncated(tmp_path):
    state = SnsState(tmp_path)
    for _ in range(C.EVENTS_LIMIT + 20):
        state.record("push", platform="douyin", name="n" * 200, text="t" * 400)
    assert len(state.events) == C.EVENTS_LIMIT
    assert len(state.events[-1]["name"]) == 60
    assert len(state.events[-1]["text"]) == 120


def test_state_remove_by_composite_key(tmp_path):
    state = SnsState(tmp_path)
    state.add(SnsAccount(platform="xhs", account_id="u1"))
    assert state.remove("xhs:u1") is not None
    assert state.accounts == {}


# ---------------------------------------------------------------- identity


def test_parse_cookie_string_handles_noise_and_duplicates():
    raw = "Cookie: a1=xxx; web_session=yyy\r\nwebId=zzz; a1=last"
    parsed = parse_cookie_string(raw)
    assert parsed["a1"] == "last"  # 后者覆盖前者
    assert parsed["web_session"] == "yyy"
    assert parsed["webId"] == "zzz"
    assert "Cookie:" not in "".join(parsed)
    assert parse_cookie_string("garbage") == {}


def test_redact_cookie_never_leaks_the_secret():
    secret = "a1=0123456789abcdef; web_session=verysecrettoken"
    redacted = redact_cookie(secret)
    assert "verysecrettoken" not in redacted
    assert "0123456789abcdef" not in redacted
    assert "共" in redacted
    assert redact_cookie("") == ""


def test_identity_status_and_missing_keys():
    status = identity_status("a1=1; web_session=2")
    assert status["configured"] is True
    assert status["keys"] == 2  # 面板只显示"识别到几个字段"，不回显键名
    assert "verysecret" not in json.dumps(identity_status("web_session=verysecret"))
    assert missing_required_keys("xhs", "a1=1") == ["web_session"]
    assert missing_required_keys("douyin", "ttwid=1") == []
    assert missing_required_keys("instagram", "") == ["sessionid"]


def test_identity_store_persists_cookie_and_login_state(tmp_path):
    store = IdentityStore(tmp_path)
    assert store.stored_cookie("xhs") == ""
    store.save_cookie("xhs", "a1=1; web_session=2")
    assert store.stored_cookie("xhs") == "a1=1; web_session=2"

    fresh = IdentityStore(tmp_path)
    assert fresh.stored_cookie("xhs") == "a1=1; web_session=2"
    fresh.mark_login("xhs", True, note="手动登录")
    assert fresh.login_state("xhs")["logged_in"] is True
    assert fresh.login_state("xhs")["note"] == "手动登录"

    summary = fresh.summary("xhs", "")
    assert summary["cookie_configured"] is True
    assert summary["logged_in"] is True
    assert "web_session=2" not in json.dumps(summary)
    # P2-7 回归：摘要里**不能**再有本机绝对路径（它会经 /sns/overview 原样
    # 回传到浏览器）；面板只需要"目录建了没"这一个比特。
    assert "profile_dir" not in summary
    assert "has_profile" in summary
    assert isinstance(summary["has_profile"], bool)
    assert os.sep + C.SNS_BROWSER_DIR not in json.dumps(summary)
    assert not any(isinstance(v, str) and os.path.isabs(v) for v in summary.values())
    # Cookie 文件不该对所有用户可读
    cred = fresh.credentials_path
    assert cred.exists()
    if os.name != "nt":
        assert (cred.stat().st_mode & 0o777) == 0o600
