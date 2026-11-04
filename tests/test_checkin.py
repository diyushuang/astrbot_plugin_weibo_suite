"""每日签到：状态存储、语句加载与主流程兜底。"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image as PILImage
from PIL import ImageFont

from astrbot_plugin_weibo_suite.trace.checkin_card import (
    DEFAULT_QUOTES,
    POSTER_MAX_SIDE,
    QuoteLibrary,
    compose_poster,
    resolve_font_path,
    strip_emoji,
    wrap_text,
)
from astrbot_plugin_weibo_suite.trace.checkin_store import CheckinStore


def test_load_quotes_falls_back_on_non_utf8_file(tmp_path):
    """ANSI/GBK 保存的 quotes.txt 不能让 /签到 抛 UnicodeDecodeError。"""
    path = tmp_path / "quotes.txt"
    path.write_bytes(b"\xff\xfe\x00bad")

    library = QuoteLibrary(str(path), source="builtin")

    assert library._quotes == DEFAULT_QUOTES


def test_quote_library_local_text_priority_and_comment_lines(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text("文件里的句子\n", encoding="utf-8")

    library = QuoteLibrary(
        str(path),
        local_text="第一句\n# 注释\n\n第二句",
        source="builtin",
    )

    assert library._quotes == ("第一句", "第二句")


def test_checkin_store_streak_and_total(tmp_path):
    store = CheckinStore(str(tmp_path / "checkin.db"))
    today = date(2026, 10, 30)
    try:
        first = store.check_in("u1", today)
        assert (first.already, first.streak, first.total) == (False, 1, 1)

        repeat = store.check_in("u1", today)
        assert (repeat.already, repeat.streak, repeat.total) == (True, 1, 1)

        next_day = store.check_in("u1", today + timedelta(days=1))
        assert (next_day.already, next_day.streak, next_day.total) == (False, 2, 2)

        after_gap = store.check_in("u1", today + timedelta(days=3))
        assert (after_gap.already, after_gap.streak, after_gap.total) == (False, 1, 3)
    finally:
        store.close()


def test_compose_poster_returns_jpeg(tmp_path):
    font = resolve_font_path("")
    if font is None:
        pytest.skip("当前环境没有可用的中文字体")

    buffer = io.BytesIO()
    PILImage.effect_noise((320, 240), 48).convert("RGB").save(buffer, "JPEG")

    card = compose_poster(
        buffer.getvalue(),
        quote="今天的每一步，都算数。",
        footer="连续签到 3 天 · 累计 5 天",
    )

    assert card is not None
    assert card[:3] == b"\xff\xd8\xff"
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.format == "JPEG"


async def test_checkin_flow_uses_builtin_quote_when_library_raises(
    make_plugin, make_event, monkeypatch
):
    """语句库构造/取句失败时签到仍要发出卡片文案，而不是抛回事件管线。"""
    import astrbot_plugin_weibo_suite.trace.main as main_module

    plugin = make_plugin(checkin={"render_mode": "simple"})
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings):
        return (
            "https://img.example.com/file/thumb.jpg",
            "https://img.example.com/file/orig.jpg",
            "orig.jpg",
        )

    monkeypatch.setattr(plugin, "_random_image_candidate", fake_candidate)

    def broken_library():
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_quote_library", broken_library)
    monkeypatch.setattr(main_module.random, "choice", lambda _seq: "FALLBACK")

    jpeg = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(jpeg, "JPEG")

    async def fake_prepare(_blocks, *, compress):
        assert compress is True
        return [(jpeg.getvalue(), True)]

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", fake_prepare)

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    assert len(results) == 1
    assert results[0].kind == "chain"
    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "签到成功" in text
    assert "FALLBACK" in text


async def test_checkin_reports_unavailable_store(make_plugin, make_event, monkeypatch):
    """签到 DB 初始化失败时只提示不可用，插件其余功能不受影响。"""
    import astrbot_plugin_weibo_suite.trace.main as main_module

    def broken_store(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(main_module, "CheckinStore", broken_store)
    plugin = make_plugin()
    event = make_event("/签到", platform="telegram", sender_id="u1")

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    assert len(results) == 1
    assert "签到存储不可用" in results[0].text


def _write_weibo_map(weibo_dir, *, with_image: bool) -> None:
    import json
    from pathlib import Path

    weibo_dir = Path(weibo_dir)
    item = {
        "link": "https://weibo.com/1000000001/RnmlWjQgw",
        "code": "RnmlWjQgw",
        "account": "示例博主A",
        "published_at": "2026-10-19 10:10",
        "date": "20261019",
        "text": "今天的天气很适合出门\n路上顺手拍了几张照片",
        "image": (
            {
                "file_name": "【微博@示例博主A】20261019-01：秋日.jpg",
                "image_url": "https://img.example.com/file/orig.jpg",
                "thumb_url": "https://img.example.com/file/thumb.jpg",
                "score": 0.99,
            }
            if with_image
            else None
        ),
    }
    weibo_dir.mkdir(parents=True, exist_ok=True)
    (weibo_dir / "weibo_post_map.json").write_text(
        json.dumps({"version": 1, "generated_at": 1, "items": [item]}, ensure_ascii=False),
        encoding="utf-8",
    )


async def test_checkin_weibo_mode_uses_map_copy_and_image(
    make_plugin, make_event, data_dir, monkeypatch
):
    """微博模式：文案来自映射表，不显示链接，卡片/文字带博主与发博日期。"""
    plugin = make_plugin(checkin={"mode": "weibo", "render_mode": "simple"})
    _write_weibo_map(plugin.weibo_dir, with_image=True)
    event = make_event("/签到", platform="telegram", sender_id="u1")

    jpeg = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(jpeg, "JPEG")

    async def fake_prepare(_blocks, *, compress):
        assert compress is True
        return [(jpeg.getvalue(), True)]

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", fake_prepare)

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    assert len(results) == 1
    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "今天的天气很适合出门" in text
    assert "微博@示例博主A" in text
    assert "2026-10-19" in text
    assert "weibo.com" not in text


async def test_checkin_weibo_mode_falls_back_when_no_image(
    make_plugin, make_event, data_dir, monkeypatch
):
    """映射里的微博没有图时回退随机图 + 随机语句，签到不失败。"""
    import astrbot_plugin_weibo_suite.trace.main as main_module

    plugin = make_plugin(checkin={"mode": "weibo", "render_mode": "simple"})
    _write_weibo_map(plugin.weibo_dir, with_image=False)
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings):
        return (
            "https://img.example.com/file/thumb.jpg",
            "https://img.example.com/file/orig.jpg",
            "orig.jpg",
        )

    monkeypatch.setattr(plugin, "_random_image_candidate", fake_candidate)

    def broken_library():
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_quote_library", broken_library)
    monkeypatch.setattr(main_module.random, "choice", lambda _seq: "FALLBACK")

    jpeg = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(jpeg, "JPEG")

    async def fake_prepare(_blocks, *, compress):
        assert compress is True
        return [(jpeg.getvalue(), True)]

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", fake_prepare)

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "FALLBACK" in text


async def test_checkin_import_writes_file(make_plugin, make_event, data_dir, tmp_path):
    """管理员导入 CSV 后文件落到 weibo/，回复包含条数。"""
    import astrbot.api.message_components as Comp

    source = tmp_path / "微博原创文案.csv"
    source.write_text(
        "序号,发布时间,来源,文案,微博链接\n"
        '1,"2026-10-19 10:10","微博@示例博主A","今天的天气很适合出门","https://weibo.com/1000000001/RnmlWjQgw"\n',
        encoding="utf-8",
    )
    plugin = make_plugin()
    file_seg = Comp.File(name="微博原创文案.csv", file=str(source))
    event = make_event("/签到文案导入", chain=[file_seg])

    results = []
    async for result in plugin.checkin_import(event):
        results.append(result)

    assert len(results) == 1
    assert "已导入" in results[0].text
    assert "1 条文案" in results[0].text
    assert (Path(plugin.weibo_dir) / "微博原创文案.csv").is_file()


def test_compose_poster_downscales_huge_jpeg_before_gate():
    """超过 50MP 的 JPEG 要先 DCT 降采样再判闸门，不能整张跳过合成。

    线上实测 8736×11648 的手机长图曾被防炸闸门直接拒掉，卡片静默降级成
    没有边框的轻量模式；draft 之后同一张图能正常合成。
    """
    font = resolve_font_path("")
    if font is None:
        pytest.skip("当前环境没有可用的中文字体")

    buffer = io.BytesIO()
    # 7600×6700 = 50.92MP，刚超过 _MAX_LOAD_PIXELS；纯色图编解码都很快
    PILImage.new("RGB", (7600, 6700), (200, 180, 160)).save(buffer, "JPEG", quality=60)

    card = compose_poster(
        buffer.getvalue(),
        quote="长图也能合成",
        footer="连续签到 3 天",
        font_path=font,
    )

    assert card is not None, "降采样后应合成成功，而不是被像素闸门跳过"
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.format == "JPEG"
        assert image.width <= POSTER_MAX_SIDE + 300
        assert image.width < 7600


def test_wrap_text_treats_newline_as_hard_break():
    """微博文案自带换行：先按物理行切段，再按像素折行，行数预算全局共享。"""
    font = resolve_font_path("")
    if font is None:
        pytest.skip("当前环境没有可用的中文字体")
    measured = ImageFont.truetype(font, 24)

    lines = wrap_text("第一行\n第二行\n第三行", measured, 400, max_lines=3)
    assert lines == ["第一行", "第二行", "第三行"]
    assert all("\n" not in line for line in lines)

    budgeted = wrap_text("第一行\n第二行\n第三行", measured, 400, max_lines=2)
    assert len(budgeted) == 2
    assert budgeted[-1].endswith("…")


def test_strip_emoji_keeps_dingbat_whitelist_and_removes_zwj():
    """Dingbats 白名单保留；真 emoji、ZWJ 组合与变体选择符仍整串剔除。"""
    kept = "✓ ✔ ✂ ❤ ✉ ★"
    assert strip_emoji(kept) == kept
    assert strip_emoji("完成✅🎉") == "完成"
    assert strip_emoji("👨‍👩‍👧‍👦 一家") == "一家"
    assert strip_emoji("❤️ 喜欢") == "❤ 喜欢"


def test_compose_poster_strips_emoji_and_stays_within_line_budget():
    """emoji 在中文字体上是豆腐块，合成前剔除；卡片高度不超过行数预算。"""
    assert strip_emoji("坠入秋日棕调氛围里🍂") == "坠入秋日棕调氛围里"
    assert strip_emoji("出门看海 🌊✨") == "出门看海"
    # ★ ☀ 这类中文字体确实有字形的符号要保留
    assert strip_emoji("★ 收藏") == "★ 收藏"

    font = resolve_font_path("")
    if font is None:
        pytest.skip("当前环境没有可用的中文字体")

    buffer = io.BytesIO()
    PILImage.effect_noise((320, 240), 48).convert("RGB").save(buffer, "JPEG")

    card = compose_poster(
        buffer.getvalue(),
        quote="坠入秋日棕调氛围里🍂\n第二段✅✨\n第三段",
        footer="签到 3 天🎉",
        quote_max_lines=3,
        font_path=font,
    )

    assert card is not None
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.format == "JPEG"
        # 与 compose_poster 的排版契约对齐：3 行正文 + 2 行页脚是上限
        border = max(12, round(320 * 0.045))
        font_size = min(48, max(18, round(320 * 0.036)))
        line_height = font_size + round(font_size * 0.45)
        footer_size = max(14, round(font_size * 0.72))
        footer_line_height = footer_size + round(footer_size * 0.35)
        gap = max(8, round(font_size * 0.5))
        budget = 240 + border * 3 + line_height * 3 + gap + footer_line_height * 2
        assert 240 < image.height <= budget


def _stub_checkin_thumbnail(monkeypatch, plugin, *, thumb: str | None) -> list[str]:
    """让缩略图反查走真实对象、只固定返回的直链，记录被查询的 file id。"""
    seen: list[str] = []

    async def fake_thumb_url_for(file_id: str):
        seen.append(file_id)
        return thumb

    monkeypatch.setattr(plugin.vector, "qdrant_ready", True)
    monkeypatch.setattr(plugin.vector, "thumb_url_for", fake_thumb_url_for)
    return seen


async def test_checkin_card_source_forces_thumbnail_in_quote_mode(make_plugin, monkeypatch):
    """quote 模式发送态是原图时，卡片合成必须改用缩略图替身。"""
    plugin = make_plugin()
    original = "https://img.example.com/file/x.jpg"
    thumb = "https://img.example.com/file/thumbnails/1_x.jpg"
    seen = _stub_checkin_thumbnail(monkeypatch, plugin, thumb=thumb)

    source = await plugin._checkin_card_source(original, original)

    assert seen == ["x.jpg"]
    assert source == thumb


async def test_checkin_card_source_falls_back_when_thumbnail_missing(make_plugin, monkeypatch):
    """查不到缩略图时退回原图，绝不能让签到合成为此失败。"""
    plugin = make_plugin()
    original = "https://img.example.com/file/x.jpg"
    _stub_checkin_thumbnail(monkeypatch, plugin, thumb=None)

    assert await plugin._checkin_card_source(original, original) == original


async def test_checkin_card_source_ignores_send_thumbnail_switch(make_plugin, monkeypatch):
    """random_media.send_thumbnail=false 只影响 /随机图，签到照样用缩略图。"""
    plugin = make_plugin(random_media={"send_thumbnail": False})
    original = "https://img.example.com/file/x.jpg"
    thumb = "https://img.example.com/file/thumbnails/1_x.jpg"
    seen = _stub_checkin_thumbnail(monkeypatch, plugin, thumb=thumb)

    assert plugin._random_settings()["send_thumbnail"] is False
    assert await plugin._checkin_card_source(original, original) == thumb
    assert seen == ["x.jpg"]


async def test_checkin_card_source_skips_duplicate_lookup_after_send_checked(
    make_plugin, monkeypatch
):
    """发送态已查过替身且没查到：卡片链路不再查同一 id，仍回退原图。"""
    plugin = make_plugin()  # random_media.send_thumbnail 默认 True
    original = "https://img.example.com/file/x.jpg"
    seen = _stub_checkin_thumbnail(monkeypatch, plugin, thumb=None)
    settings = plugin._random_settings()

    # 发送链路第一次查询：没有替身，返回原图
    assert await plugin._random_send_url(original, settings) == original
    # 卡片链路带着「已查过」标记：直接回退，不再查第二次
    assert (
        await plugin._checkin_card_source(original, original, thumbnail_prechecked=True) == original
    )
    assert seen == ["x.jpg"]


async def test_random_send_url_default_keeps_send_thumbnail_switch(make_plugin, monkeypatch):
    """/随机图 走默认口径：thumbnail=None 时仍尊重 send_thumbnail 开关。"""
    plugin = make_plugin(random_media={"send_thumbnail": False})
    original = "https://img.example.com/file/x.jpg"
    thumb = "https://img.example.com/file/thumbnails/1_x.jpg"
    seen = _stub_checkin_thumbnail(monkeypatch, plugin, thumb=thumb)
    settings = plugin._random_settings()

    assert await plugin._random_send_url(original, settings) == original
    assert seen == []
    assert await plugin._random_send_url(original, settings, thumbnail=True) == thumb
    assert seen == ["x.jpg"]
