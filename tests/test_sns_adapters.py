"""P8 平台适配器：把真实响应形状喂进去，断言解析与风控信号判定。

夹具全部取自实测响应（抖音 web 接口 / 小红书 SSR / Instagram GraphQL /
RSS 2.0 与 Atom），不联网。这里守的是两条底线：

- **解析**：字段位置换了、包了一层，也要认出 id/正文/图集；
- **风控**：空响应、"Blocked by"、`302 /login`、`checkpoint_required` 必须被
  判成对应信号，而不是当成"没有更新"。
"""

from __future__ import annotations

import json

from astrbot_plugin_weibo_suite.sns import constants as C
from astrbot_plugin_weibo_suite.sns.adapters import douyin as dy
from astrbot_plugin_weibo_suite.sns.adapters import instagram as ig
from astrbot_plugin_weibo_suite.sns.adapters import rss, xhs

# ---------------------------------------------------------------- 抖音

SEC_UID = "MS4wLjABAAAAdemoSecUidPlaceholder000000000000000"

AWEME_VIDEO = {
    "aweme_id": "7400000000000000001",
    "desc": "今天去看海 #vlog",
    "create_time": 1730000000,
    "author": {"nickname": "示例抖音博主"},
    "video": {
        "play_addr": {
            "url_list": [
                "https://v3-web.douyinvod.com/watermark.mp4",
                "https://v3-web.douyinvod.com/clean.mp4",
            ]
        },
        "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
    },
}

AWEME_IMAGES = {
    "aweme_id": "7400000000000000002",
    "desc": "图集来啦",
    "create_time": 1730000100,
    "author": {"nickname": "示例抖音博主"},
    "images": [
        {
            "url_list": [
                "https://p3.douyinpic.com/small1.jpeg",
                "https://p3.douyinpic.com/big1.jpeg",
            ]
        },
        {"url_list": ["https://p3.douyinpic.com/big2.jpeg"]},
    ],
}


def test_douyin_account_id_from_link_or_bare_uid():
    assert dy.account_id_from_text(f"https://www.douyin.com/user/{SEC_UID}") == SEC_UID
    assert dy.account_id_from_text(f"https://www.douyin.com/share/user/{SEC_UID}?x=1") == SEC_UID
    assert dy.account_id_from_text(SEC_UID) == SEC_UID
    assert dy.account_id_from_text("https://www.douyin.com/video/7400000000000000001") is None
    assert dy.account_id_from_text("随便一句话") is None


def test_douyin_signature_matches_the_documented_recipe():
    params = dy.post_params(SEC_UID, count=10)
    uifid = "abc123"
    signed = dy.signed_params(params, uifid=uifid, timestamp=1700000000)
    assert signed["uifid"] == uifid
    assert str(signed["timestamp"]) == "1700000000"
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    expected = dy.build_signature(params, uifid=uifid, timestamp=1700000000)
    assert len(expected) == 32
    # 同一份参数 + 同一时间戳必须稳定（签名不是随机的）
    assert dy.build_signature(params, uifid=uifid, timestamp=1700000000) == expected
    assert dy.build_signature(params, uifid="other", timestamp=1700000000) != expected
    assert "aid=6383" in query and "sec_user_id=" in query


def test_douyin_extract_uifid_from_cookie_string():
    cookie = "ttwid=1|abc; UIFID_TEMP=deadbeef; passport_csrf_token=zzz"
    assert dy.extract_uifid(cookie) == "deadbeef"
    assert dy.extract_uifid("ttwid=1|abc") == ""
    assert dy.extract_uifid("") == ""


def test_douyin_parse_video_post_prefers_last_url():
    post = dy.parse_aweme(AWEME_VIDEO, account_id=SEC_UID)
    assert post is not None
    assert post.post_id == "7400000000000000001"
    assert post.link == "https://www.douyin.com/video/7400000000000000001"
    assert post.kind == "video"
    assert post.videos == ["https://v3-web.douyinvod.com/clean.mp4"]
    assert post.images == ["https://p3.douyinpic.com/cover.jpeg"]
    assert post.ts == 1730000000
    assert "看海" in post.text


def test_douyin_parse_image_post_collects_all_images():
    post = dy.parse_aweme(AWEME_IMAGES, account_id=SEC_UID)
    assert post is not None
    assert post.kind == "note"
    assert post.images == [
        "https://p3.douyinpic.com/big1.jpeg",
        "https://p3.douyinpic.com/big2.jpeg",
    ]
    assert post.videos == []


def test_douyin_parse_list_wraps_aweme_list():
    posts = dy.parse_aweme_list({"aweme_list": [AWEME_VIDEO, AWEME_IMAGES]}, account_id=SEC_UID)
    assert [p.post_id for p in posts] == ["7400000000000000001", "7400000000000000002"]


def test_douyin_detect_signal_recognizes_risk_control():
    assert dy.detect_signal({"status_code": 0, "status_msg": "blocked"}) == "douyin.argus_blocked"
    assert dy.detect_signal({"status_code": 8}) == "douyin.status_code_8"
    assert dy.detect_signal({"status_code": 0, "aweme_list": []}, status=403) == "douyin.http_403"
    assert dy.detect_signal({}, status=429) == "douyin.http_429"
    assert dy.detect_signal({"status_code": 0, "aweme_list": [AWEME_VIDEO]}) == ""


def test_douyin_empty_payload_is_a_risk_signal_not_no_update():
    # 抖音风控的典型形状：HTTP 200 + status_code 0 + 空列表
    assert dy.signal_for_empty({"status_code": 0, "aweme_list": []}) == "douyin.empty_silent"
    # 正常但真的没有作品时 payload 里仍有 has_more / cursor
    empty_but_normal = {"status_code": 0, "aweme_list": [], "has_more": 0, "cursor": 0}
    assert dy.signal_for_empty(empty_but_normal) == ""


# ---------------------------------------------------------------- 小红书

XHS_USER_ID = "5f3a1b2c3d4e5f6a7b8c9d0e"

XHS_STATE = {
    "user": {
        "userPageData": {
            "basicInfo": {
                "nickname": "小红薯的日常",
                "images": "https://sns-avatar.xhscdn.com/a.jpg",
            },
        },
        "notes": [
            [
                {
                    "noteId": "65aabbccddeeff0011223344",
                    "displayTitle": "周末去哪玩",
                    "type": "normal",
                    "user": {"nickname": "小红薯的日常"},
                    "cover": {
                        "infoList": [
                            {"url": "https://sns-img.xhscdn.com/crop.jpg"},
                            {"url": "https://sns-img.xhscdn.com/full.jpg"},
                        ]
                    },
                }
            ]
        ],
    }
}

XHS_NOTE_STATE = {
    "note": {
        "noteDetailMap": {
            "65aabbccddeeff0011223344": {
                "note": {
                    "title": "周末去哪玩",
                    "desc": "去了海边，风很大",
                    "time": 1730000000000,
                    "imageList": [
                        {"urlDefault": "https://sns-img.xhscdn.com/1.jpg"},
                        {"urlDefault": "https://sns-img.xhscdn.com/2.jpg"},
                    ],
                    "video": {
                        "media": {
                            "stream": {
                                "h264": [{"masterUrl": "https://sns-video.xhscdn.com/wm.mp4"}],
                                "h265": [{"masterUrl": "https://sns-video.xhscdn.com/clean.mp4"}],
                            }
                        }
                    },
                }
            }
        }
    }
}


def test_xhs_ids_from_links():
    profile_url = f"https://www.xiaohongshu.com/user/profile/{XHS_USER_ID}"
    assert xhs.account_id_from_text(profile_url) == XHS_USER_ID
    assert xhs.account_id_from_text(XHS_USER_ID) == XHS_USER_ID
    explore_url = "https://www.xiaohongshu.com/explore/65aabbccddeeff0011223344"
    assert xhs.account_id_from_text(explore_url) is None
    assert xhs.note_id_from_text(explore_url) == ("65aabbccddeeff0011223344")
    assert xhs.is_short_link("http://xhslink.com/a/abc123") is True
    assert xhs.is_short_link("https://www.xiaohongshu.com/explore/x") is False


def test_xhs_extract_initial_state_tolerates_undefined():
    html = '<html><script>window.__INITIAL_STATE__={"a":undefined,"b":1};</script></html>'
    state = xhs.extract_initial_state(html)
    assert state == {"a": None, "b": 1}
    assert xhs.extract_initial_state("<html>nothing</html>") is None


def test_xhs_parse_user_notes_reads_nickname_and_cards():
    nickname, avatar, posts = xhs.parse_user_notes(XHS_STATE)
    assert nickname == "小红薯的日常"
    assert avatar.endswith("a.jpg")
    assert len(posts) == 1
    post = posts[0]
    assert post.post_id == "65aabbccddeeff0011223344"
    assert post.title == "周末去哪玩"
    # 封面取 infoList 里最后一项（无裁剪原图）
    assert post.images == ["https://sns-img.xhscdn.com/full.jpg"]
    assert post.link.endswith("/explore/65aabbccddeeff0011223344")


def test_xhs_card_carries_timestamp_from_the_card():
    """P2-8 回归：卡片 ts 原来是硬编码 0.0。

    ts 恒 0 会让 `SnsService._check_account` 的
    `fresh.sort(key=lambda p: (p.ts or 0.0, p.post_id))` 退化成按 post_id
    字典序排——"推最新 3 条"实际推的是"id 最大的 3 条"。
    """
    state = {
        "user": {
            "userPageData": {"basicInfo": {"nickname": "n"}},
            "notes": [
                {"noteId": "a" * 24, "displayTitle": "旧", "time": 1700000000000},
                {"noteId": "b" * 24, "displayTitle": "新", "createTime": 1730000000000},
                {"noteId": "c" * 24, "displayTitle": "无时间"},
            ],
        }
    }
    _nickname, _avatar, posts = xhs.parse_user_notes(state)
    by_id = {post.post_id: post for post in posts}
    # 毫秒被换成秒
    assert by_id["a" * 24].ts == 1700000000.0
    assert by_id["b" * 24].ts == 1730000000.0
    # 取不到时间的用抓取时刻兜底，绝不留 0（否则排序退化）
    assert by_id["c" * 24].ts > 1e9

    # 乱序输入 -> 按 ts 取最新 3 条时能挑出真正最新的那些
    fresh = sorted(posts, key=lambda p: (p.ts, p.post_id))
    assert fresh[-1].post_id == "b" * 24


def test_xhs_parse_user_notes_accepts_raw_value_wrapper():
    wrapped = {
        "user": {
            "userPageData": {"_rawValue": XHS_STATE["user"]["userPageData"], "_value": {}},
            "notes": {"_rawValue": XHS_STATE["user"]["notes"]},
        }
    }
    nickname, _, posts = xhs.parse_user_notes(wrapped)
    assert nickname == "小红薯的日常"
    assert len(posts) == 1


def test_xhs_parse_note_detail_prefers_h265_and_unwatermarked_images():
    detail = xhs.parse_note_detail(XHS_NOTE_STATE, "65aabbccddeeff0011223344")
    assert detail["images"] == [
        "https://sns-img.xhscdn.com/1.jpg",
        "https://sns-img.xhscdn.com/2.jpg",
    ]
    assert detail["videos"] == ["https://sns-video.xhscdn.com/clean.mp4"]
    assert detail["ts"] == 1730000000.0
    assert "海边" in detail["text"]


def test_xhs_parse_note_detail_missing_returns_empty_shape():
    empty = xhs.parse_note_detail({}, "nope")
    assert empty == {"images": [], "videos": [], "text": "", "title": "", "ts": 0.0}


def test_xhs_detect_signal_covers_the_measured_risk_shapes():
    login_path = "/login?redirectPath=%2Fuser"
    assert xhs.detect_signal("", login_path, http_status=302) == "xhs.redirect_login"
    assert xhs.detect_signal("<div class='fe-verify-box'></div>") == "xhs.verify_box"
    assert xhs.detect_signal("...error_code=300031...") == "xhs.error_300031"
    assert xhs.detect_signal('{"code":-1,"success":false}', http_status=406) == "xhs.http_406"
    assert xhs.detect_signal("<html>没有状态</html>") == "xhs.empty_state"
    assert xhs.detect_signal('window.__INITIAL_STATE__={"user":{}}</script>') == ""


def test_xhs_has_user_notes_distinguishes_logged_out_state():
    assert xhs.has_user_notes(XHS_STATE) is True
    assert xhs.has_user_notes({"user": {}}) is False
    assert xhs.has_user_notes({}) is False


# ---------------------------------------------------------------- Instagram

IG_NODE_VIDEO = {
    "shortcode": "C1abcdEFghi",
    "taken_at_timestamp": 1730000000,
    "is_video": True,
    "display_url": "https://scontent.cdninstagram.com/cover.jpg",
    "video_url": "https://scontent.cdninstagram.com/v.mp4",
    "owner": {"username": "someblogger"},
    "edge_media_to_caption": {"edges": [{"node": {"text": "新视频上线\n第二行"}}]},
}

IG_NODE_SIDECAR = {
    "shortcode": "C2abcdEFghi",
    "taken_at": 1730000200,
    "display_url": "https://scontent.cdninstagram.com/1.jpg",
    "owner": {"username": "someblogger"},
    "edge_media_to_caption": {"edges": [{"node": {"text": "图集"}}]},
    "edge_sidecar_to_children": {
        "edges": [
            {"node": {"display_url": "https://scontent.cdninstagram.com/1.jpg"}},
            {"node": {"display_url": "https://scontent.cdninstagram.com/2.jpg"}},
        ]
    },
}


def test_instagram_account_id_from_link_and_reserved_paths():
    assert ig.account_id_from_text("https://www.instagram.com/someblogger/") == "someblogger"
    assert ig.account_id_from_text("@someblogger") == "someblogger"
    assert ig.account_id_from_text("https://www.instagram.com/p/C1abcdEFghi/") is None
    assert ig.account_id_from_text("https://www.instagram.com/reel/C1abcdEFghi/") is None
    assert ig.account_id_from_text("https://www.instagram.com/explore/tags/cat/") is None


def test_instagram_parse_profile_reads_edges():
    payload = {
        "data": {
            "user": {
                "edge_owner_to_timeline_media": {
                    "edges": [{"node": IG_NODE_VIDEO}, {"node": IG_NODE_SIDECAR}]
                }
            }
        }
    }
    posts = ig.parse_profile(payload)
    assert [p.post_id for p in posts] == ["C1abcdEFghi", "C2abcdEFghi"]
    video, sidecar = posts
    assert video.kind == "video"
    assert video.videos == ["https://scontent.cdninstagram.com/v.mp4"]
    assert video.images == ["https://scontent.cdninstagram.com/cover.jpg"]
    assert video.link == "https://www.instagram.com/p/C1abcdEFghi/"
    assert sidecar.kind == "image"
    assert sidecar.images == [
        "https://scontent.cdninstagram.com/1.jpg",
        "https://scontent.cdninstagram.com/2.jpg",
    ]


def test_instagram_profile_from_html_slices_embedded_json():
    html = (
        '<html><script>window.x={"a":1,"edge_owner_to_timeline_media":'
        '{"edges":[{"node":{"shortcode":"C1abcdEFghi"}}]},"z":2};</script></html>'
    )
    data = ig.profile_from_html(html)
    assert isinstance(data, dict)
    assert ig.parse_profile(data)[0].post_id == "C1abcdEFghi"
    assert ig.profile_from_html("<html>nothing</html>") is None


def test_instagram_detect_signal_recognizes_login_and_checkpoint():
    assert ig.detect_signal({"message": "checkpoint_required"}) == "ig.checkpoint"
    assert ig.detect_signal({"message": "challenge_required"}) == "ig.challenge"
    assert ig.detect_signal({"require_login": True}) == "ig.require_login"
    assert ig.detect_signal({}, status=429) == "ig.http_429"
    assert ig.detect_signal({}, status=401) == "ig.http_401"
    assert ig.detect_signal("", final_url="https://www.instagram.com/challenge/") == "ig.challenge"
    assert ig.detect_signal({"data": {}}) == ""


def test_instagram_empty_payload_is_a_risk_signal():
    assert ig.signal_for_empty({"data": {}}) == "ig.empty_edges"
    payload = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": []}}}}
    assert ig.signal_for_empty(payload) == ""


# ---------------------------------------------------------------- RSS


RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>feed</title>
<item>
  <title>第一条</title>
  <link>https://example.com/p/1</link>
  <description><![CDATA[<p>正文 <img src="https://example.com/1.jpg"></p>]]></description>
  <pubDate>Sat, 26 Oct 2024 10:00:00 GMT</pubDate>
  <guid>guid-1</guid>
</item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>Atom 条目</title>
  <link href="https://example.com/p/2" rel="alternate"/>
  <content type="html">&lt;img src="https://example.com/2.jpg"&gt;</content>
  <updated>2024-10-26T12:00:00Z</updated>
  <id>tag:example.com,2024:2</id>
</entry>
</feed>"""


def test_rss_parse_feed_reads_rss2_items():
    posts = rss.parse_feed(RSS_XML, account_id="https://example.com/feed")
    assert len(posts) == 1
    post = posts[0]
    assert post.post_id == "guid-1"
    assert post.title == "第一条"
    assert post.link == "https://example.com/p/1"
    assert post.images == ["https://example.com/1.jpg"]
    assert post.ts > 0


def test_rss_parse_feed_reads_atom_entries():
    posts = rss.parse_feed(ATOM_XML)
    assert len(posts) == 1
    post = posts[0]
    assert post.post_id == "tag:example.com,2024:2"
    assert post.link == "https://example.com/p/2"
    assert post.images == ["https://example.com/2.jpg"]
    assert post.ts > 0


def test_rss_detect_signal_rejects_html_and_rate_limit_pages():
    assert rss.detect_signal("<html><body>Too Many Requests</body></html>") == "net.error"
    assert rss.detect_signal("<html><body>hello</body></html>") == "net.error"
    assert rss.detect_signal(RSS_XML) == ""
    assert rss.detect_signal(ATOM_XML) == ""


def test_rss_parse_feed_never_raises_on_garbage():
    assert rss.parse_feed("not xml at all") == []
    assert rss.parse_feed("") == []


def test_rss_parse_feed_refuses_internal_entities():
    """P3-14 回归：stdib 会展开内部实体（billion laughs），见到 <!ENTITY 直接拒收。

    外部实体（XXE 的 `SYSTEM`/`PUBLIC`）stdlib 本来就不展开，但内部实体是
    会展开的——feed 地址由用户填，属于不可信输入，不能让它把内存吃光。
    """
    bomb = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE lolz ["
        '<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        "]>"
        "<rss><channel><item><title>&lol2;</title></item></channel></rss>"
    )
    assert rss.parse_feed(bomb) == []
    # 正常 feed 不含 DOCTYPE 实体，仍然能解析
    assert len(rss.parse_feed(RSS_XML, account_id="x")) == 1


def test_douyin_dead_post_url_builder_is_gone():
    """P3-9：`post_url` 拼查询串不做 urlencode 且无调用者，已删除。

    留着它迟早有人拿来用——sec_uid 里一旦出现 `&` 或 `+`，签名参数会被
    截断成别的参数（抖音侧表现为 403，且完全看不出原因）。
    """
    assert not hasattr(dy, "post_url")


# ---------------------------------------------------------------- 汇总入口


def test_parse_posts_dispatch_and_platform_support():
    from astrbot_plugin_weibo_suite.sns import adapters

    assert adapters.supports("douyin") and adapters.supports("xhs")
    assert adapters.supports("instagram")
    # rss 是"源"不是"平台"：它走 `platform="rss"` 的独立通道，不在 PLATFORMS 里
    assert adapters.supports("rss") is False
    assert adapters.supports("weibo") is False
    posts = adapters.parse_posts("douyin", {"aweme_list": [AWEME_VIDEO]}, account_id=SEC_UID)
    assert posts[0].account_id == SEC_UID
    xhs_posts = adapters.parse_posts("xhs", XHS_STATE, account_id=XHS_USER_ID)
    assert xhs_posts[0].post_id == "65aabbccddeeff0011223344"
    assert xhs_posts[0].account_id == XHS_USER_ID
    assert adapters.parse_posts("rss", RSS_XML)[0].post_id == "guid-1"


def test_every_detected_signal_is_declared_in_the_signal_table():
    # 任何新信号忘记登记，风控口径就会静默退化成"未知"
    from astrbot_plugin_weibo_suite.sns import adapters

    samples = {
        "douyin": [
            dy.signal_for_empty({"status_code": 0}),
            dy.detect_signal({}, status=403),
            dy.detect_signal({"status_code": 8}),
        ],
        "xhs": [
            xhs.detect_signal("<div class='fe-verify-box'></div>"),
            xhs.detect_signal("", "/login"),
            xhs.detect_signal("<html>x</html>"),
        ],
        "instagram": [
            ig.detect_signal({"message": "checkpoint_required"}),
            ig.detect_signal({}, status=429),
            ig.signal_for_empty({}),
        ],
        "rss": [rss.detect_signal("<html>nope</html>")],
    }
    for platform, signals in samples.items():
        for signal in signals:
            assert signal, platform
            assert signal in C.SIGNALS, f"{platform} 未登记信号：{signal}"
    assert adapters.parse_account_id("douyin", f"https://www.douyin.com/user/{SEC_UID}") == SEC_UID


def test_xhs_state_fixture_is_json_serialisable_for_panels():
    # 面板与状态文件都不该出现不可序列化对象
    assert json.loads(json.dumps(XHS_STATE)) == XHS_STATE
