"""qq_upload 上传通道：通道选择（纯函数）+ 接管/兜底行为。

背景：AstrBot 的 qqofficial 适配器对图片恒走 `file_data` 二进制直传，QQ 侧上限
5MB，超过即报「上传文件大小超过限制」，且异常发生在 respond stage——插件侧
捕获不到，只能在发送前选对通道。这里覆盖三件事：

1. 配置归一化（默认值、非法值回退、非 dict 兜底）；
2. 通道选择（固定通道不改道；auto 按体积/可达性分流；体积未知时不冒险直传）；
3. 公网直链判定（内网/回环/单标签主机名一律不可用于 URL 上传）。
4. 接管流程：成功时自己发 msg_type=7；失败时按 on_failure 交回或回文字说明；
   固定直链通道拿不到公网直链时降级分片（不发一次必败的 URL 上传）。
"""

from __future__ import annotations

import logging
import types

import pytest

from astrbot_plugin_weibo_suite.trace.image_delivery import (
    choose_qq_upload_channel,
    classify_qq_upload_error,
    is_public_http_url,
    qq_upload_failure_hint,
    qq_upload_settings,
)
from astrbot_plugin_weibo_suite.trace.qq_upload_mixin import (
    cap_qq_media_blocks,
    qq_safe_local_image,
)

MB = 1024 * 1024


# ----------------------------------------------------------------------
# 配置归一化
# ----------------------------------------------------------------------


def test_settings_defaults():
    """默认：auto 分流、直传 3MB、URL 软限 20MB、auto 优先 URL、兜底交回原逻辑。"""
    mode, direct_max, url_max, prefer, max_upload, failure = qq_upload_settings({})

    assert mode == "auto"
    assert direct_max == 3 * MB
    assert url_max == 20 * MB
    assert prefer == "url"
    assert max_upload == 100 * MB
    assert failure == "fallback"


def test_settings_normalizes_bad_values():
    """大小写/空白/非法枚举都归一到合法值，数值越界按区间夹紧。"""
    mode, direct_max, _, prefer, max_upload, failure = qq_upload_settings(
        {
            "mode": "  CHUNKED ",
            "direct_max_mb": "abc",
            "auto_prefer": "nope",
            "max_upload_mb": 0,
            "on_failure": "boom",
        }
    )

    assert mode == "bytes"
    assert direct_max == 3 * MB, "非法数值回退默认值，不能变成 0（0 表示从不直传）"
    assert prefer == "url"
    assert max_upload == 1 * MB, "分片下载上限有下限 1MB，填 0 会被抬到下限"
    assert failure == "fallback"


def test_qq_settings_tolerates_non_dict():
    """手工把配置组写坏（非 dict）时按默认值处理，不抛异常。"""
    assert qq_upload_settings(None)[0] == "auto"
    assert qq_upload_settings(["mode"])[0] == "auto"


def test_qq_settings_share_the_unified_vocabulary():
    """两个平台的通道名必须是同一套：五个取值 + 同一批旧写法别名。"""
    for mode in ("auto", "native", "bytes", "url", "url-scaled"):
        assert qq_upload_settings({"mode": mode})[0] == mode

    # 旧写法（各平台历史上的不同叫法）统一映射到同一套新名
    assert qq_upload_settings({"mode": "direct"})[0] == "native"
    assert qq_upload_settings({"mode": "chunked"})[0] == "bytes"
    assert qq_upload_settings({"mode": "local-compress"})[0] == "auto"
    assert qq_upload_settings({"mode": "scaled-url"})[0] == "url-scaled"
    assert qq_upload_settings({"mode": "original-url"})[0] == "url"
    assert qq_upload_settings({"auto_prefer": "chunked"})[3] == "bytes"


# ----------------------------------------------------------------------
# 通道选择
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["native", "url", "bytes"])
def test_fixed_channel_never_reroutes(mode):
    """固定通道不因体积/可达性改道——用户钉死就该遵守，失败由 on_failure 处理。"""
    assert choose_qq_upload_channel(mode=mode, size_bytes=50 * MB, url_usable=False) == mode
    assert choose_qq_upload_channel(mode=mode, size_bytes=1, url_usable=True) == mode


def test_auto_small_image_stays_direct():
    """小图仍走原生直传：最快，且能叠加本地压缩。"""
    assert choose_qq_upload_channel(mode="auto", size_bytes=1 * MB) == "native"


def test_auto_direct_max_zero_disables_direct():
    """直传上限填 0 表示从不直传。"""
    assert choose_qq_upload_channel(mode="auto", size_bytes=1, direct_max_bytes=0) == "bytes"


def test_auto_big_image_defaults_to_url():
    """默认 auto_prefer=url：公网直链可用时只发一条直链，不占本机上行、不落盘。"""
    assert choose_qq_upload_channel(mode="auto", size_bytes=8 * MB, url_usable=True) == "url"


def test_auto_prefers_chunked_when_configured():
    """把偏好改成 chunked 时，即使 URL 可用也走分片（纯内网/保守部署）。"""
    assert (
        choose_qq_upload_channel(
            mode="auto", size_bytes=8 * MB, url_usable=True, auto_prefer="bytes"
        )
        == "bytes"
    )


def test_auto_prefers_url_when_configured_and_reachable():
    assert (
        choose_qq_upload_channel(mode="auto", size_bytes=8 * MB, url_usable=True, auto_prefer="url")
        == "url"
    )


def test_auto_skips_url_when_link_is_intranet():
    """拿不到公网直链（内网直连）时，URL 上传必然失败，直接分片。"""
    assert (
        choose_qq_upload_channel(
            mode="auto", size_bytes=8 * MB, url_usable=False, auto_prefer="url"
        )
        == "bytes"
    )


def test_auto_url_respects_soft_limit():
    """超过 20MB 软限的图走 URL 会被平台降级成文件卡片，故直接分片。"""
    assert (
        choose_qq_upload_channel(
            mode="auto", size_bytes=25 * MB, url_usable=True, auto_prefer="url"
        )
        == "bytes"
    )


def test_auto_unknown_size_never_direct():
    """体积未知时不判直传——万一是大图就会撞 5MB。"""
    assert choose_qq_upload_channel(mode="auto", size_bytes=None, url_usable=False) == "bytes"
    assert (
        choose_qq_upload_channel(mode="auto", size_bytes=None, url_usable=True, auto_prefer="url")
        == "url"
    )


# ----------------------------------------------------------------------
# 公网直链判定
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://img.example.com/file/a.jpg",
        "http://1.2.3.4/a.jpg",
        "https://[2001:4860:4860::8888]/a.jpg",
        # 有些部署会把公网段 IP 当作内网直连地址使用（RFC1918 只有 10/8、172.16/12、
        # 192.168/16），句法层面判不出来——内网判定交给 _public_media_url 按配置比对
        "http://93.184.216.34:7658/file/a.jpg",
    ],
)
def test_public_url_accepted(url):
    assert is_public_http_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/a.jpg",
        "http://172.16.0.1:7658/a.jpg",
        "http://192.168.1.9/a.jpg",
        "http://127.0.0.1/a.jpg",
        "http://[::1]/a.jpg",
        "http://169.254.1.1/a.jpg",
        "http://localhost/a.jpg",
        "http://astrbot:7658/a.jpg",
        "http://imgbed.local/a.jpg",
        "ftp://img.example.com/a.jpg",
        "file:///tmp/a.jpg",
        "",
    ],
)
def test_intranet_or_invalid_rejected(url):
    """私有段/回环/链路本地/单标签主机名/非 http(s) 一律判定不可用于 URL 上传。"""
    assert is_public_http_url(url) is False


# ----------------------------------------------------------------------
# 图床直链 → 可用于 URL 上传的公网地址
# ----------------------------------------------------------------------


def test_public_url_swaps_intranet_link_back_to_base(make_plugin):
    """内网直连下命中的是内网直链：按 base_url 换回公网源。"""
    plugin = make_plugin(
        random_media={
            "base_url": "https://img.example.com",
            "lan_base_url": "http://192.0.2.10:7658",
        }
    )
    settings = plugin._random_settings()

    result = plugin._public_media_url("http://192.0.2.10:7658/file/a.jpg", settings)

    assert result == "https://img.example.com/file/a.jpg"


def test_public_url_rejects_when_base_is_also_intranet(make_plugin):
    """base_url 自己就配成了内网地址：换源后仍是内网主机，不能用于 URL 上传。"""
    plugin = make_plugin(
        random_media={
            "base_url": "http://192.0.2.10:7658",
            "lan_base_url": "http://192.0.2.10:7658",
        }
    )
    settings = plugin._random_settings()

    assert plugin._public_media_url("http://192.0.2.10:7658/file/a.jpg", settings) is None


def test_public_url_keeps_third_party_public_link(make_plugin):
    """第三方公网直链原样返回。"""
    plugin = make_plugin(random_media={"base_url": "https://img.example.com"})
    settings = plugin._random_settings()

    assert (
        plugin._public_media_url("https://cdn.other.com/a.jpg", settings)
        == "https://cdn.other.com/a.jpg"
    )


def test_public_url_rejects_single_label_host(make_plugin):
    """单标签主机名（只在局域网解析）不可用于 URL 上传。"""
    plugin = make_plugin(random_media={"base_url": "https://img.example.com"})
    settings = plugin._random_settings()

    assert plugin._public_media_url("http://astrbot:7658/file/a.jpg", settings) is None


def test_public_url_swap_keeps_path_and_query(make_plugin):
    """换源只换协议+主机端口，路径与查询串原样保留（含端口与百分号编码）。"""
    plugin = make_plugin(
        random_media={
            "base_url": "https://img.example.com:8443",
            "lan_base_url": "http://192.0.2.10:7658",
        }
    )
    settings = plugin._random_settings()

    result = plugin._public_media_url(
        "http://192.0.2.10:7658/file/a%20b.jpg?width=800&fallback=original", settings
    )

    assert result == "https://img.example.com:8443/file/a%20b.jpg?width=800&fallback=original"


# ----------------------------------------------------------------------
# 接管流程（真实方法 + 假 botpy 客户端）
# ----------------------------------------------------------------------


class _FakeQQApi:
    """botpy BotAPI 替身：只记录发消息调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post_group_message(self, **kwargs):
        self.calls.append(("group", kwargs))
        return {"id": "msg"}

    async def post_c2c_message(self, **kwargs):
        self.calls.append(("c2c", kwargs))
        return {"id": "msg"}


class _FakeQQBot:
    def __init__(self) -> None:
        self.api = _FakeQQApi()
        self._http = object()


def _qq_event(make_event, *, group="G1"):
    """构造 qq_official 事件：带 botpy 客户端与 raw_message（群或 C2C）。"""
    bot = _FakeQQBot()
    event = make_event("", platform="qq_official", origin=group or "U1", bot=bot)
    event.message_obj.message_id = "ROBOT1.0_test"
    if group:
        event.message_obj.raw_message = types.SimpleNamespace(group_openid=group)
    else:
        author = types.SimpleNamespace(user_openid="U1")
        event.message_obj.raw_message = types.SimpleNamespace(author=author)
    return event, bot.api


async def test_takeover_sends_media_with_msg_type_7(make_plugin, make_event, monkeypatch):
    """分片通道接管后自己发 msg_type=7，配文走 content。"""
    plugin = make_plugin(qq_upload={"mode": "bytes"})
    event, api = _qq_event(make_event)

    async def fake_upload(_event, _block, channel, _public, _target, _max):
        assert channel == "bytes"
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    handled = await plugin._qq_upload_takeover(
        event, "🖼️ a.jpg", [{"url": "http://192.0.2.10:7658/file/a.jpg"}]
    )

    assert handled is True
    assert len(api.calls) == 1
    kind, payload = api.calls[0]
    assert kind == "group"
    assert payload["msg_type"] == 7
    assert payload["media"] == "MEDIA"
    assert payload["content"] == "🖼️ a.jpg"
    assert payload["msg_id"] == "ROBOT1.0_test"


async def test_takeover_c2c_uses_openid(make_plugin, make_event, monkeypatch):
    """C2C 会话改用 post_c2c_message(openid=...)。"""
    plugin = make_plugin(qq_upload={"mode": "bytes"})
    event, api = _qq_event(make_event, group=None)

    async def fake_upload(*_args, **_kwargs):
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)
    assert await plugin._qq_upload_takeover(event, "", [{"url": "https://a/b.jpg"}]) is True
    assert api.calls[0][0] == "c2c"
    assert api.calls[0][1]["msg_type"] == 7


async def test_direct_mode_never_takes_over(make_plugin, make_event):
    """mode=direct 表示维持原行为：不接管、不发消息，交回适配器。"""
    plugin = make_plugin(qq_upload={"mode": "native"})
    event, api = _qq_event(make_event)

    handled = await plugin._qq_upload_takeover(event, "标题", [{"url": "https://a/b.jpg"}])

    assert handled is False
    assert api.calls == []


async def test_failure_falls_back_before_anything_sent(make_plugin, make_event, monkeypatch):
    """一张都没发出去时按 fallback 交回原回传逻辑，且不再补发文字。"""
    plugin = make_plugin(qq_upload={"mode": "bytes", "on_failure": "fallback"})
    event, api = _qq_event(make_event)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("上传文件大小超过限制")

    monkeypatch.setattr(plugin, "_qq_upload_block", boom)

    handled = await plugin._qq_upload_takeover(event, "标题", [{"url": "https://a/b.jpg"}])

    assert handled is False
    assert api.calls == []


async def test_failure_notify_sends_text_instead(make_plugin, make_event, monkeypatch):
    """on_failure=notify：不交回，回一条纯文本说明。"""
    plugin = make_plugin(qq_upload={"mode": "bytes", "on_failure": "notify"})
    event, api = _qq_event(make_event)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("分片合并失败")

    monkeypatch.setattr(plugin, "_qq_upload_block", boom)

    handled = await plugin._qq_upload_takeover(event, "标题", [{"url": "https://a/b.jpg"}])

    assert handled is True
    kind, payload = api.calls[0]
    assert kind == "group"
    assert payload["msg_type"] == 0
    assert "图片发送失败" in payload["content"]


@pytest.mark.parametrize(
    "block",
    [
        {"url": "http://192.0.2.10:7658/file/a.jpg", "file_name": "a.jpg"},
        {"path": "converted.jpg", "file_name": "a.jpg"},
    ],
    ids=["intranet-url", "local-path"],
)
async def test_url_mode_without_public_link_degrades_to_chunked(
    make_plugin, make_event, monkeypatch, caplog, block
):
    """固定 URL 通道但拿不到公网直链（内网源 / 只剩本地 path）：降级分片。

    这两个情况 URL 上传都没有可用入参，照原通道发出去必然失败、还会连记
    多条 WARN。图源本机就有（path 直接可用，内网 URL 本机也能下载），
    降级分片后只留一条 INFO，图片照常投递。
    """
    plugin = make_plugin(
        qq_upload={"mode": "url"},
        random_media={"base_url": "", "lan_base_url": "http://192.0.2.10:7658"},
    )
    event, api = _qq_event(make_event)
    seen: dict = {}

    async def fake_upload(_event, _block, channel, *_args):
        seen["channel"] = channel
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    with caplog.at_level(logging.INFO, logger="astrbot"):
        handled = await plugin._qq_upload_takeover(event, "标题", [block])

    assert handled is True, "降级后应正常投递，而不是交回原逻辑"
    assert seen["channel"] == "bytes"
    assert api.calls[0][1]["msg_type"] == 7
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings == [], (
        f"可预判的公网直链缺失不该记 WARN: {[record.getMessage() for record in warnings]}"
    )
    assert "改走分片上传" in caplog.text


async def test_auto_small_image_hands_back_to_native_path(make_plugin, make_event, monkeypatch):
    """auto 判为 direct 时不接管（整批交回），保持小图原有的零额外开销。"""
    plugin = make_plugin(qq_upload={"mode": "auto"})
    event, api = _qq_event(make_event)
    monkeypatch.setattr(plugin, "_qq_block_size", _size(1 * MB))

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is False
    assert api.calls == []


async def test_auto_large_image_takes_over(make_plugin, make_event, monkeypatch):
    """auto 判为分片时接管并发图。"""
    plugin = make_plugin(qq_upload={"mode": "auto"})
    event, api = _qq_event(make_event)
    monkeypatch.setattr(plugin, "_qq_block_size", _size(30 * MB))

    async def fake_upload(*_args, **_kwargs):
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True
    assert api.calls[0][1]["msg_type"] == 7


# ----------------------------------------------------------------------
# 1.8.2：官方错误码分类、被动回复上限与格式体检
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,kind",
    [
        ("40093002", "quota"),
        ("850018", "muted"),
        ("850026", "url_unreachable"),
        ("304082", "url_unreachable"),
        ("304083", "url_unreachable"),
        ("22009", "rate_limit"),
        ("11253", "no_permission"),
        ("850031", "too_large"),
        ("850019", "unsupported"),
    ],
)
def test_classify_qq_upload_error_by_code(code, kind):
    """业务错误码是稳定契约：按文档归类，而不是靠文案猜。"""
    assert classify_qq_upload_error(RuntimeError(f"api error, code={code}")) == kind


def test_classify_qq_upload_error_prefers_code_over_text():
    """文案里同时出现「超时」与额度错误码时，以错误码为准。"""
    exc = RuntimeError("request timeout: 40093002 超过今天发送文件容量上限")

    assert classify_qq_upload_error(exc) == "quota"


def test_qq_upload_failure_hint_only_for_known_kinds():
    assert "额度" in qq_upload_failure_hint("quota")
    assert qq_upload_failure_hint("unknown") == ""


def test_cap_qq_media_blocks_keeps_first_five_images_and_text():
    """超过被动回复上限时只留前 5 张图，纯文本 block 原样保留且不改输入。"""
    blocks = [{"url": f"https://img.example.com/file/{index}.jpg"} for index in range(7)]
    blocks.append({"text": "说明"})

    kept, note = cap_qq_media_blocks(blocks)

    assert len([block for block in kept if block.get("url")]) == 5
    assert {"text": "说明"} in kept
    assert "另有 2 张未发送" in note
    assert len(blocks) == 8, "不能改动调用方传入的 blocks"


async def test_auto_url_failure_retries_as_chunked(make_plugin, make_event, monkeypatch):
    """auto 下 URL 上传失败（腾讯拉不到）按官方建议改走分片，而不是直接压缩降级。"""
    plugin = make_plugin(
        qq_upload={"mode": "auto"},
        random_media={"base_url": "https://img.example.com"},
    )
    event, api = _qq_event(make_event)
    monkeypatch.setattr(plugin, "_qq_block_size", _size(8 * MB))
    channels: list[str] = []

    async def fake_upload(_event, _block, channel, *_args):
        channels.append(channel)
        if channel in {"url", "url-scaled"}:
            raise RuntimeError("850026 下载原始文件失败")
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True
    assert channels == ["url", "bytes"], "URL 失败后应改分片重试一次"
    assert api.calls[0][1]["msg_type"] == 7


async def test_upload_fatal_error_notifies_without_fallback(make_plugin, make_event, monkeypatch):
    """额度用完/禁言这类错误换通道也必败：直接回说明，不再交回压缩降级链。"""
    plugin = make_plugin(qq_upload={"mode": "bytes", "on_failure": "fallback"})
    event, api = _qq_event(make_event)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("40093002 超过今天发送文件容量上限")

    monkeypatch.setattr(plugin, "_qq_upload_block", boom)

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True, "致命错误必须自行收尾，不能交回原逻辑"
    kind, payload = api.calls[0]
    assert kind == "group"
    assert payload["msg_type"] == 0
    assert "额度" in payload["content"]


async def test_chunked_upload_corrects_fake_extension(make_plugin, tmp_path):
    """分片上传前按真实内容纠正扩展名：PNG 顶着 .jpg 会被 QQ 判 850019。"""
    plugin = make_plugin()
    path = tmp_path / "fake.jpg"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    upload_path, name, converted = await qq_safe_local_image(
        plugin, str(path), "fake.jpg", 100 * MB
    )

    assert upload_path == str(path)
    assert name == "fake.png"
    assert converted == ""


async def test_chunked_upload_transcodes_non_whitelisted_format(make_plugin, tmp_path):
    """TIFF 等非白名单格式先转 JPEG 再分片，转码产物由调用方清理。"""
    from PIL import Image as PILImage

    plugin = make_plugin()
    path = tmp_path / "photo.tif"
    PILImage.effect_noise((32, 32), 32).convert("RGB").save(path, format="TIFF")

    upload_path, name, converted = await qq_safe_local_image(
        plugin, str(path), "photo.tif", 100 * MB
    )

    assert converted, "可解码的非白名单格式必须转码"
    assert upload_path == converted
    assert name.endswith(".jpg")
    plugin._remove_quiet(converted)


async def test_yield_delivery_stops_event_after_takeover(make_plugin, make_event, monkeypatch):
    """接管成功后 _yield_delivery 必须标记已发送并 stop_event，防 respond stage 重发。"""
    plugin = make_plugin(qq_upload={"mode": "bytes"})
    event, _api = _qq_event(make_event)

    async def identity(blocks):
        return blocks

    async def fake_upload(*_args, **_kwargs):
        return "MEDIA"

    monkeypatch.setattr(plugin, "_offbox_rewrite_blocks", identity)
    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": "http://192.0.2.10:7658/file/a.jpg"}]
        )
    ]

    assert results == [], "接管路径自己发消息，不应再 yield 消息链"
    assert event.stopped is True
    assert plugin._event_already_sent(event) is True


async def test_url_scaled_channel_uploads_scaled_link(make_plugin, make_event, monkeypatch):
    """url-scaled 通道交给腾讯的是图床缩放版直链（与 OneBot 的 url-scaled 同名同义）。"""
    plugin = make_plugin(
        qq_upload={"mode": "url-scaled"},
        random_media={"base_url": "https://img.example.com"},
        image_delivery={"max_side": 1280, "quality": 80},
    )
    event, _api = _qq_event(make_event)
    seen: dict = {}

    async def fake_url_upload(_event, url, _target):
        seen["url"] = url
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_via_url", fake_url_upload)

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True
    assert "width=1280" in seen["url"], "缩放通道必须把缩放参数带上"


def _size(value):
    async def _fake(_block):
        return value

    return _fake


# ----------------------------------------------------------------------
# URL 通道失败冷却（auto 自动改分片，固定通道不受影响）
# ----------------------------------------------------------------------


async def test_url_failure_starts_cooldown(make_plugin, make_event, monkeypatch):
    """URL 上传失败一次就记冷却：公网直链拿得到、腾讯却拉不到时别反复白撞。"""
    plugin = make_plugin(
        qq_upload={"mode": "url", "on_failure": "notify"},
        random_media={"base_url": "https://img.example.com"},
    )
    event, _api = _qq_event(make_event)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("下载文件失败")

    monkeypatch.setattr(plugin, "_qq_upload_block", boom)
    assert plugin._qq_url_cooling() is False

    await plugin._qq_upload_takeover(event, "标题", [{"url": "https://img.example.com/file/a.jpg"}])

    assert plugin._qq_url_cooling() is True


async def test_cooldown_pushes_auto_to_chunked(make_plugin, make_event, monkeypatch):
    """冷却期内 auto 跳过 URL 通道，直接分片。"""
    plugin = make_plugin(
        qq_upload={"mode": "auto"},
        random_media={"base_url": "https://img.example.com"},
    )
    event, _api = _qq_event(make_event)
    plugin._note_qq_url_unsupported(RuntimeError("拉不到"))
    monkeypatch.setattr(plugin, "_qq_block_size", _size(8 * MB))

    seen: dict = {}

    async def fake_upload(_event, _block, channel, *_args):
        seen["channel"] = channel
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    handled = await plugin._qq_upload_takeover(
        event, "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True
    assert seen["channel"] == "bytes"


async def test_fixed_url_channel_ignores_cooldown(make_plugin, make_event, monkeypatch):
    """固定 url 通道不因冷却改道：用户钉死就该照做，失败由 on_failure 决定。"""
    plugin = make_plugin(
        qq_upload={"mode": "url"},
        random_media={"base_url": "https://img.example.com"},
    )
    event, _api = _qq_event(make_event)
    plugin._note_qq_url_unsupported(RuntimeError("拉不到"))

    seen: dict = {}

    async def fake_upload(_event, _block, channel, *_args):
        seen["channel"] = channel
        return "MEDIA"

    monkeypatch.setattr(plugin, "_qq_upload_block", fake_upload)

    await plugin._qq_upload_takeover(event, "标题", [{"url": "https://img.example.com/file/a.jpg"}])

    assert seen["channel"] == "url"
