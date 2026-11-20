"""collector：账号解析、原创判定、归档触发与状态快照。"""

from __future__ import annotations

from astrbot_plugin_weibo_suite.weibo.pipeline_naming import (
    CollectorFlags,
    collector_accounts,
    collector_alias,
    collector_flags,
    collector_post_allowed,
    merged_category_map,
)


def _post(mid="500", uid="1001", **overrides):
    post = {
        "id": mid,
        "uid": uid,
        "author": "接口昵称",
        "text": "正文",
        "is_retweet": False,
        "orig_uid": "",
        "orig_name": "",
        "pics": [{"pid": "p1", "url": "https://wx1.sinaimg.cn/large/p1.jpg"}],
        "pic_total": 1,
        "is_long": False,
        "created_ts": 0.0,
    }
    post.update(overrides)
    return post


def test_accounts_parse_alias_category_and_skip_invalid():
    config = {
        "collector": {
            "accounts": [
                "2106192855|赵今麦|12、日常分享",
                "6670517617|赵今麦工作室|",
                "6670517617|后出现的覆盖|7、综艺节目",
                "",
                "不是uid|别名|分类",
                "12345",
            ]
        }
    }
    specs = collector_accounts(config)
    assert set(specs) == {"2106192855", "6670517617", "12345"}
    assert specs["2106192855"].alias == "赵今麦"
    assert specs["2106192855"].category == "12、日常分享"
    assert specs["6670517617"].alias == "后出现的覆盖"
    assert specs["6670517617"].category == "7、综艺节目"
    assert specs["12345"].alias == ""


def test_flags_clamp_and_parse_allow_list():
    flags = collector_flags(
        {
            "collector": {
                "enabled": "true",
                "include_retweets": "0",
                "allow_retweet_from_uids": "2106192855, 赵今麦工作室",
                "min_images": 99,
                "skip_push": "on",
            }
        }
    )
    assert flags.enabled is True
    assert flags.include_retweets is False
    assert flags.allow_retweet_from == ("2106192855", "赵今麦工作室")
    assert flags.min_images == 9
    assert flags.skip_push is True


def test_post_allowed_original_retweet_and_min_images():
    flags = CollectorFlags(enabled=True, min_images=2)
    assert collector_post_allowed(flags, is_retweet=False, pic_count=2)
    assert not collector_post_allowed(flags, is_retweet=False, pic_count=1)
    assert not collector_post_allowed(
        flags, is_retweet=True, orig_uid="999", orig_name="别人", pic_count=2
    )
    allow = CollectorFlags(enabled=True, min_images=1, allow_retweet_from=("2106192855",))
    assert collector_post_allowed(allow, is_retweet=True, orig_uid="2106192855", pic_count=1)
    assert not collector_post_allowed(allow, is_retweet=True, orig_name="赵今麦", pic_count=1)
    assert collector_post_allowed(
        CollectorFlags(enabled=True, include_retweets=True, min_images=1),
        is_retweet=True,
        pic_count=1,
    )


def test_merged_category_map_pipeline_wins_and_collector_fills():
    config = {
        "pipeline": {"categories": {"2106192855": "已有分类"}},
        "collector": {
            "enabled": True,
            "accounts": ["2106192855|赵今麦|新分类", "6670517617|工作室|7、综艺节目"],
        },
    }
    mapping = merged_category_map(config)
    assert mapping["2106192855"] == "已有分类"
    assert mapping["6670517617"] == "7、综艺节目"
    assert collector_alias(config, "2106192855") == "赵今麦"


async def test_collector_consider_forces_pics_and_archives_once(make_weibo_plugin):
    plugin = make_weibo_plugin(
        collector={
            "enabled": True,
            "accounts": ["1001|博主A|12、日常分享"],
        }
    )
    post = _post(pics=[], pic_total=3)
    ensured = []
    archived = []

    async def ensure(p, *, force=False):
        ensured.append(force)
        p["pics"] = [{"pid": f"p{i}"} for i in range(1, 4)]

    async def archive(p, *, source="monitor"):
        archived.append((p["id"], source, len(p.get("pics") or [])))
        return True

    plugin._ensure_full_pics = ensure
    plugin.archive_post = archive

    assert await plugin.collector_consider(post) is True
    assert ensured == [True]
    assert archived == [("500", "collector", 3)]
    assert post["_collector_collected"] is True
    assert post["_collector_uid"] == "1001"


async def test_collector_consider_skips_retweet_without_allow(make_weibo_plugin):
    plugin = make_weibo_plugin(
        collector={
            "enabled": True,
            "accounts": ["1001|博主A|12、日常分享"],
        }
    )
    post = _post(is_retweet=True, orig_uid="999", orig_name="原博主", pics=[{"pid": "p1"}])
    archive_calls = []

    async def archive(p, *, source="monitor"):
        archive_calls.append(source)
        return True

    plugin.archive_post = archive
    assert await plugin.collector_consider(post) is False
    assert archive_calls == []


async def test_ensure_full_pics_force_fetches_empty_card(make_weibo_plugin):
    plugin = make_weibo_plugin(collector={"enabled": True, "accounts": ["1001|博主A|分类"]})
    calls = []

    async def fetch(pid, what="详情"):
        calls.append(pid)
        return {
            "pics": [
                {"pid": "p1", "large": {"url": "https://wx1.sinaimg.cn/large/p1.jpg"}},
                {"pid": "p2", "large": {"url": "https://wx1.sinaimg.cn/large/p2.jpg"}},
            ],
            "pic_num": 2,
        }

    plugin._fetch_status_detail = fetch
    post = _post(pics=[], pic_total=0)
    await plugin._ensure_full_pics(post, force=True)
    assert calls == ["500"]
    assert len(post["pics"]) == 2
    assert post["_pics_full_checked"] is True


async def test_collector_status_reports_directory_and_counts(make_weibo_plugin):
    class Store:
        def count_posts(self, uid):
            return 4 if uid == "1001" else 2

        def media_stats(self, uid):
            return {"total": 9, "pending": 0, "download": 0, "vector": 8, "failed": 1}

    plugin = make_weibo_plugin(
        collector={
            "enabled": True,
            "accounts": ["1001|赵今麦|12、日常分享", "1002|工作室|"],
        },
        pipeline={"enabled": True},
    )
    plugin.accounts = {"1001": {"name": "赵今麦angel"}}
    plugin.archive_store = lambda: Store()
    status = plugin.collector_status()

    assert status["enabled"] is True
    assert status["accounts"][0]["directory"] == "12、日常分享/【微博图集@赵今麦】"
    assert status["accounts"][0]["random_command"] == "/随机图 12、日常分享/【微博图集@赵今麦】"
    assert status["accounts"][0]["posts"] == 4
    assert status["accounts"][0]["monitored"] is True
    assert status["accounts"][1]["monitored"] is False
    assert status["accounts"][0]["images"] == 9
    assert status["accounts"][0]["states"]["vector"] == 8


def test_pipeline_collector_allows_original_and_allowed_retweet(make_weibo_plugin):
    plugin = make_weibo_plugin(
        collector={
            "enabled": True,
            "accounts": ["1001|博主A|分类"],
            "allow_retweet_from_uids": ["2106192855"],
        }
    )
    original = _post(uid="1001", pics=[{"pid": "p1"}, {"pid": "p2"}])
    assert plugin._pipeline_collector_allows("1001", post=original) is True
    blocked = _post(uid="1001", is_retweet=True, orig_uid="999", pics=[{"pid": "p1"}])
    assert plugin._pipeline_collector_allows("1001", post=blocked) is False
    allowed = _post(uid="1001", is_retweet=True, orig_uid="2106192855", pics=[{"pid": "p1"}])
    assert plugin._pipeline_collector_allows("1001", post=allowed) is True
    # 回填行没有 orig_uid，只能从 raw_json 的 orig_name 识别例外
    row = {
        "uid": "1001",
        "is_retweet": 1,
        "media_total": 1,
        "raw_json": '{"orig_name": "2106192855"}',
    }
    assert plugin._pipeline_collector_allows("1001", row=row) is True
    # 非 collector 账号不受影响
    assert plugin._pipeline_collector_allows("2002", post=blocked) is True


async def test_collector_disabled_is_noop(make_weibo_plugin):
    plugin = make_weibo_plugin(collector={"enabled": False, "accounts": ["1001|博主A|分类"]})
    called = []

    async def archive(p, *, source="monitor"):
        called.append(source)
        return True

    plugin.archive_post = archive
    assert await plugin.collector_consider(_post()) is False
    assert called == []
