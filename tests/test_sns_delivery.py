"""P8 投递契约：订阅域的 payload 交给微博域 `deliver_sns_payload` 后的落地形状。

这一层是"多平台订阅"和"微博推送"唯一的接缝：订阅域只产出 dict，文案渲染、
图片段、@全体与超时语义全部复用微博域的成熟实现。所以这里断言的重点不是
"能不能发出去"，而是**发出去的东西长什么样**：

1. 模板渲染（默认模板 / 自定义模板 / 模板写坏时退回默认）；
2. 图片段数量受 9 张上限与 `message_with_images` 开关约束；
3. `@全体` 三关（开关、关键词、账号白名单）都过、且只在群会话里才生效；
4. 超时算 `unknown`（可能已发出，重试会重复）、其余异常算 `failed`。
"""

from __future__ import annotations

import asyncio

import pytest

from .host_compat import chain_segments, image_ref

GROUP = "aiocqhttp:GroupMessage:12345"
PRIVATE = "aiocqhttp:FriendMessage:12345"


def _payload(**overrides):
    payload = {
        "platform": "抖音",
        "name": "示例抖音博主",
        "title": "新作品",
        "text": "今天更新了一条",
        "link": "https://www.douyin.com/video/7300000000000000000",
        "ts": 1_700_000_000,
        "images": [],
        "videos": [],
        "kind": "video",
        "at_all_keys": ["MS4wLjABAAAAtest", "示例抖音博主", "douyin"],
        "dedupe_key": "douyin:MS4wLjABAAAAtest:7300000000000000000",
    }
    payload.update(overrides)
    return payload


class _Recorder:
    """把 `context.send_message` 的入参留下来，并允许指定发送结果。"""

    def __init__(self, *, ok=True, delay=0.0, error=None):
        self.ok = ok
        self.delay = delay
        self.error = error
        self.chains = []
        self.sessions = []

    async def __call__(self, session, chain):
        self.sessions.append(session)
        self.chains.append(chain_segments(chain))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.ok


def _texts(chain):
    return [getattr(part, "text", "") for part in chain_segments(chain) if hasattr(part, "text")]


def _images(chain):
    return [ref for ref in (image_ref(seg) for seg in chain_segments(chain)) if ref]


def _has_at_all(chain):
    return any(getattr(part, "qq", "") == "all" for part in chain_segments(chain))


def _run(make_weibo_plugin, config=None, *, recorder=None):
    plugin = make_weibo_plugin(**(config or {}))
    plugin._http = None
    recorder = recorder or _Recorder()
    plugin.context.send_message = recorder
    return plugin, recorder


def test_default_template_renders_all_placeholders(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin)

    result = asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))

    assert result == "sent"
    body = "\n".join(_texts(recorder.chains[0]))
    assert "📢 抖音更新" in body
    assert "【示例抖音博主】" in body
    assert "今天更新了一条" in body
    assert _payload()["link"] in body
    # 时间戳必须被渲染成人能看的日期，而不是裸的 1700000000
    assert "1700000000" not in body


def test_custom_template_is_honoured(make_weibo_plugin):
    plugin, recorder = _run(
        make_weibo_plugin,
        {"sns": {"message_format": "{platform}|{name}|{text}"}},
    )

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "sent"
    assert _texts(recorder.chains[0])[0] == "抖音|示例抖音博主|今天更新了一条"


def test_broken_template_falls_back_instead_of_dropping(make_weibo_plugin):
    """用户把占位符写成 {wx} 时，推送不能消失——退回默认模板继续发。"""
    plugin, recorder = _run(make_weibo_plugin, {"sns": {"message_format": "{wx} 更新"}})

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "sent"
    body = "\n".join(_texts(recorder.chains[0]))
    assert "📢 抖音更新" in body
    assert "今天更新了一条" in body


def test_text_falls_back_to_title_when_body_missing(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin)

    asyncio.run(plugin.deliver_sns_payload(_payload(text="", title="只有标题"), GROUP))

    assert "只有标题" in "\n".join(_texts(recorder.chains[0]))


def test_images_are_capped_at_nine(make_weibo_plugin):
    urls = [f"https://p3.douyinpic.com/{i}.jpeg" for i in range(15)]
    plugin, recorder = _run(make_weibo_plugin)

    asyncio.run(plugin.deliver_sns_payload(_payload(images=urls), GROUP))

    assert len(_images(recorder.chains[0])) == 9


def test_image_switch_off_sends_text_only(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin, {"sns": {"message_with_images": False}})

    asyncio.run(plugin.deliver_sns_payload(_payload(images=["https://a/1.jpeg"]), GROUP))

    assert _images(recorder.chains[0]) == []


def test_blank_image_urls_are_skipped(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin)

    asyncio.run(plugin.deliver_sns_payload(_payload(images=["", None, "https://a/1.jpeg"]), GROUP))

    assert len(_images(recorder.chains[0])) == 1


# ---------------------------------------------------------------- @全体


def test_at_all_off_means_no_at(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin, {"sns": {"at_all": False}})

    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))

    assert _has_at_all(recorder.chains[0]) is False


def test_at_all_on_in_group_works(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin, {"sns": {"at_all": True}})

    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))

    assert _has_at_all(recorder.chains[0]) is True


def test_at_all_never_applies_to_private_chat(make_weibo_plugin):
    plugin, recorder = _run(make_weibo_plugin, {"sns": {"at_all": True}})

    asyncio.run(plugin.deliver_sns_payload(_payload(), PRIVATE))

    assert _has_at_all(recorder.chains[0]) is False


def test_at_all_keyword_must_match(make_weibo_plugin):
    plugin, recorder = _run(
        make_weibo_plugin,
        {"sns": {"at_all": True, "at_all_keywords": ["抽奖"]}},
    )

    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))
    assert _has_at_all(recorder.chains[0]) is False

    asyncio.run(plugin.deliver_sns_payload(_payload(text="抽奖开始"), GROUP))
    assert _has_at_all(recorder.chains[1]) is True


def test_at_all_uid_whitelist_matches_any_key(make_weibo_plugin):
    """白名单同时认 sec_uid、昵称、平台名三种写法，命中一个就够。"""
    plugin, recorder = _run(
        make_weibo_plugin,
        {"sns": {"at_all": True, "at_all_uids": ["别的博主"]}},
    )

    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))
    assert _has_at_all(recorder.chains[0]) is False

    plugin2, recorder2 = _run(
        make_weibo_plugin,
        {"sns": {"at_all": True, "at_all_uids": ["示例抖音博主"]}},
    )

    asyncio.run(plugin2.deliver_sns_payload(_payload(), GROUP))
    assert _has_at_all(recorder2.chains[0]) is True


def test_at_all_respects_daily_quota(make_weibo_plugin):
    """24 小时配额用尽后退化成普通消息，而不是整条推送失败。"""
    plugin, recorder = _run(
        make_weibo_plugin,
        {"sns": {"at_all": True}, "at_all_daily_limit": 1},
    )
    plugin._at_all_sent = []

    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))
    assert _has_at_all(recorder.chains[0]) is True

    plugin._at_all_mark_sent()
    asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP))
    assert _has_at_all(recorder.chains[1]) is False
    assert "今天更新了一条" in "\n".join(_texts(recorder.chains[1]))


# ---------------------------------------------------------------- 结果语义


def test_timeout_is_reported_as_unknown(make_weibo_plugin):
    """超时时消息可能已经出群：算 unknown，让上层不要重试。"""
    plugin, _ = _run(make_weibo_plugin)

    async def boom(*_args, **_kwargs):
        raise asyncio.TimeoutError

    plugin._send_with_timeout = boom

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "unknown"


def test_send_exception_is_reported_as_failed(make_weibo_plugin):
    recorder = _Recorder(error=RuntimeError("网络断了"))
    plugin, _ = _run(make_weibo_plugin, recorder=recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "failed"


def test_send_false_is_reported_as_failed(make_weibo_plugin):
    recorder = _Recorder(ok=False)
    plugin, _ = _run(make_weibo_plugin, recorder=recorder)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "failed"


def test_payload_without_sns_group_still_sends(make_weibo_plugin):
    """config 里没有 sns 段（老配置）时也要能发，不能 KeyError。"""
    plugin, recorder = _run(make_weibo_plugin)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(), GROUP)) == "sent"
    assert recorder.chains


@pytest.mark.parametrize("kind", ["video", "note", "rss"])
def test_any_kind_is_deliverable(make_weibo_plugin, kind):
    plugin, recorder = _run(make_weibo_plugin)

    assert asyncio.run(plugin.deliver_sns_payload(_payload(kind=kind), GROUP)) == "sent"
    assert _texts(recorder.chains[0])
