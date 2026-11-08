"""微博接口口径回归：302 归因、限流不重试、时间线参数、长文优先级、混排媒体。"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import pytest

from astrbot_plugin_weibo_suite.weibo import parser
from astrbot_plugin_weibo_suite.weibo.runtime import (
    WeiboAuthError,
    WeiboFetchError,
    WeiboRiskError,
)

INDEX_URL = "https://m.weibo.cn/api/container/getIndex"


class _Url:
    """aiohttp 的 `resp.url` 只用到 `.host` 和 `str()`。"""

    def __init__(self, raw: str):
        self._raw = raw
        self.host = urlparse(raw).hostname or ""

    def __str__(self) -> str:
        return self._raw


class _Hop:
    """`resp.history` 里的元素是响应对象，取地址要经 `.url`。"""

    def __init__(self, raw: str):
        self.url = _Url(raw)


class _Resp:
    def __init__(
        self,
        *,
        status: int = 200,
        url: str = INDEX_URL,
        history: tuple[str, ...] = (),
        body: str = '{"ok": 1, "data": {}}',
    ):
        self.status = status
        self.url = _Url(url)
        self.history = [_Hop(item) for item in history]
        self._body = body

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise AssertionError("测试桩不模拟 4xx 的 raise_for_status")

    async def text(self) -> str:
        return self._body


class _Http:
    def __init__(self, *responses: _Resp):
        self._responses = list(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs) -> _Resp:
        self.calls += 1
        if not self._responses:
            raise AssertionError("请求次数超出预期")
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


def _plugin(make_weibo_plugin, http: _Http):
    plugin = make_weibo_plugin()
    plugin._http = http
    plugin._custom_cookies = {}
    plugin._visitor_cookies = {"SUB": "visitor", "XSRF-TOKEN": "csrf"}

    async def _noop(*_args, **_kwargs) -> None:
        return None

    plugin._validate_url = _noop
    plugin._pace = _noop
    return plugin


def test_302_to_passport_is_reported_as_auth_not_risk(make_weibo_plugin):
    """登录态失效时 m.weibo.cn 会 302 到 passport.*，不能当成 IP 级风控。"""
    http = _Http(_Resp(history=("https://passport.weibo.cn/signin/login?r=1",)))
    plugin = _plugin(make_weibo_plugin, http)
    with pytest.raises(WeiboAuthError):
        asyncio.run(plugin._get_text(INDEX_URL, {}, {"Referer": "https://m.weibo.cn/"}))


def test_redirected_html_is_fetch_error_but_direct_html_is_risk(make_weibo_plugin):
    """跳转后的 HTML 不是风控；没有跳转的 HTML 才进冷却。"""
    redirected = _plugin(
        make_weibo_plugin,
        _Http(
            _Resp(
                url="https://weibo.com/",
                history=("https://weibo.com/",),
                body="<html>not json</html>",
            )
        ),
    )
    with pytest.raises(WeiboFetchError) as exc:
        asyncio.run(redirected._api_get_once(INDEX_URL, {}, "https://m.weibo.cn/"))
    assert not isinstance(exc.value, WeiboRiskError)

    direct = _plugin(make_weibo_plugin, _Http(_Resp(body="<html>blocked</html>")))
    with pytest.raises(WeiboRiskError):
        asyncio.run(direct._api_get_once(INDEX_URL, {}, "https://m.weibo.cn/"))


def test_403_and_429_enter_cooldown_without_retry(make_weibo_plugin):
    """403/429 与 418/432 同属限流：不重试，直接进冷却。"""
    for status in (403, 429):
        http = _Http(_Resp(status=status))
        plugin = _plugin(make_weibo_plugin, http)
        with pytest.raises(WeiboRiskError):
            asyncio.run(plugin._api_get(INDEX_URL, {}, "https://m.weibo.cn/"))
        assert http.calls == 1
        assert plugin._blocked_until > 0


def test_timeline_page_does_not_send_the_unsupported_count_param(make_weibo_plugin):
    """`count` 不是 getIndex 参数，带上只会让"3 页 60 条"的估算失真。"""
    plugin = make_weibo_plugin()
    captured: dict[str, object] = {}

    async def fake_api_get(url, params, referer=None, extra_headers=None):
        captured.update(params)
        return {"data": {"cards": []}}

    plugin._api_get = fake_api_get
    asyncio.run(plugin._fetch_timeline_page("1234567890", 2))
    assert "count" not in captured
    assert captured["page"] == 2


def test_archive_long_text_prefers_extend_then_uses_bid(make_weibo_plugin):
    """先走 /statuses/extend；回落 weibo.com 用 bid + weibo.com 的 Referer。"""
    plugin = make_weibo_plugin()
    calls: list[tuple[object, ...]] = []

    async def fake_extend(mid):
        calls.append(("extend", mid))
        return None

    async def fake_api_get(url, params, referer=None, extra_headers=None):
        calls.append(("longtext", params.get("id"), referer))
        return {"data": {"longTextContent": "全文"}}

    plugin._fetch_long_text = fake_extend
    plugin._api_get = fake_api_get
    plugin._custom_cookies = {"SUB": "login", "XSRF-TOKEN": "csrf"}
    text = asyncio.run(plugin._archive_long_text("5001234", bid="AbCdEf"))
    assert text == "全文"
    assert calls[0] == ("extend", "5001234")
    assert calls[1] == ("longtext", "AbCdEf", "https://weibo.com/")


def test_archive_long_text_skips_weibo_endpoint_for_visitors(make_weibo_plugin):
    """游客态打 weibo.com 恒 -100：不发请求、不烧续期配额。"""
    plugin = make_weibo_plugin()
    calls: list[tuple[object, ...]] = []

    async def fake_extend(mid):
        calls.append(("extend", mid))
        return None

    async def fake_api_get(*_args, **_kwargs):
        calls.append(("longtext",))
        return {}

    plugin._fetch_long_text = fake_extend
    plugin._api_get = fake_api_get
    plugin._custom_cookies = {}
    assert asyncio.run(plugin._archive_long_text("5001234", bid="AbCdEf")) is None
    assert calls == [("extend", "5001234")]


def test_mix_media_pictures_and_video_count_without_page_info(make_weibo_plugin):
    """图文混排帖的图与视频都只挂在 mix_media_info 里，必须并入。"""
    plugin = make_weibo_plugin()
    post = plugin._parse_post(
        {
            "id": "5001",
            "bid": "MixAbc",
            "text": "图文混排",
            "user": {"id": 1, "screen_name": "博主"},
            "mix_media_info": {
                "items": [
                    {
                        "type": "pic",
                        "data": {
                            "pic": {
                                "pid": "p1",
                                "large": {"url": "https://wx1.sinaimg.cn/large/p1.jpg"},
                            }
                        },
                    },
                    {
                        "type": "video",
                        "data": {
                            "media_info": {
                                "play_url": "https://f.video.weibocdn.com/mix.mp4",
                                "mp4_720p_mp4": "https://f.video.weibocdn.com/mix720.mp4",
                                "duration": 10,
                            }
                        },
                    },
                ]
            },
        },
        "1",
    )
    assert [item["pid"] for item in post["pics"]] == ["p1"]
    assert post["is_video"] is True


def test_to_original_keeps_orj1080_instead_of_downgrading_to_large():
    """orj1080 是微博自己的原始档位，改写成 /large/ 会降级。"""
    url = "https://wx1.sinaimg.cn/orj1080/abc123.jpg"
    assert parser.to_original(url) == url
