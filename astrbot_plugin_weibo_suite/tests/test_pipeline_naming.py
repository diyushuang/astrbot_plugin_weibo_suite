"""P4-B：weibo/pipeline_naming.py 的离线单测（合同「B」节）。

纯函数模块，不需要任何 fixture、网络或临时文件。
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from astrbot_plugin_weibo_suite.weibo.pipeline_naming import (
    DEFAULT_CATEGORY,
    DEFAULT_DIR_TEMPLATE,
    DEFAULT_FILE_TEMPLATE,
    MAX_SEGMENT_LEN,
    THUMBNAIL_FOLDER,
    NamingContext,
    category_map,
    naming_templates,
    normalize_relative_dir,
    render_directory,
    render_filename,
    resolve_category,
    sanitize_segment,
    thumbnail_folder,
    thumbnail_name,
    title_from_text,
)

UID = "1234567890"
NICK = "示例博主A"


def make_ctx(**overrides) -> NamingContext:
    fields = {
        "account": NICK,
        "category": "12、日常分享",
        "date": "2026-10-31",
        "index": 3,
        "title": "阳光很好",
    }
    fields.update(overrides)
    return NamingContext(**fields)


# ---------------- 常量与上下文 ----------------


def test_constants_match_contract():
    assert DEFAULT_DIR_TEMPLATE == "{category}/【微博图集@{account}】/"
    assert DEFAULT_FILE_TEMPLATE == "【微博@{account}】{date}-{index}：{title}.jpg"
    assert DEFAULT_CATEGORY == "未分类"
    assert THUMBNAIL_FOLDER == "thumbnails/"
    assert MAX_SEGMENT_LEN == 80


def test_naming_context_defaults_and_frozen():
    ctx = make_ctx()
    assert ctx.ext == "jpg"
    assert make_ctx(index=4) != ctx
    with pytest.raises(FrozenInstanceError):
        ctx.index = 4  # type: ignore[misc]


# ---------------- 模板读取 ----------------


def test_naming_templates_default_when_missing():
    assert naming_templates({}) == (DEFAULT_DIR_TEMPLATE, DEFAULT_FILE_TEMPLATE)
    assert naming_templates({"pipeline": None}) == (DEFAULT_DIR_TEMPLATE, DEFAULT_FILE_TEMPLATE)
    assert naming_templates({"pipeline": "x"}) == (DEFAULT_DIR_TEMPLATE, DEFAULT_FILE_TEMPLATE)


def test_naming_templates_custom_are_stripped_and_unrendered():
    config = {
        "pipeline": {
            "naming_directory": "  {category}/{account}/  ",
            "naming_file": " {date}_{index}.jpg ",
        }
    }
    assert naming_templates(config) == ("{category}/{account}/", "{date}_{index}.jpg")


@pytest.mark.parametrize("value", ["", "   ", 123, ["x"], None])
def test_naming_templates_blank_or_non_string_falls_back(value):
    config = {"pipeline": {"naming_directory": value, "naming_file": value}}
    assert naming_templates(config) == (DEFAULT_DIR_TEMPLATE, DEFAULT_FILE_TEMPLATE)


# ---------------- 分类映射 ----------------


def test_category_map_accepts_dict():
    config = {"pipeline": {"categories": {UID: "12、日常分享", NICK: "13、街拍"}}}
    assert category_map(config) == {UID: "12、日常分享", NICK: "13、街拍"}


def test_category_map_accepts_json_string():
    expected = {UID: "12、日常分享", NICK: "13、街拍"}
    raw = json.dumps(expected, ensure_ascii=False)
    assert category_map({"pipeline": {"categories": raw}}) == expected
    # 面板 textarea 里带首尾空白也照样解析
    assert category_map({"pipeline": {"categories": f"  {raw}  "}}) == expected


@pytest.mark.parametrize("raw", ["not-json", "[1, 2]", "", 42, None])
def test_category_map_rejects_invalid_payload(raw):
    assert category_map({"pipeline": {"categories": raw}}) == {}


def test_category_map_skips_invalid_items():
    raw = {UID: " 12、日常分享 ", "  ": "x", "k": "   ", "num": 3, 7: "x", "nick": "13、街拍"}
    assert category_map({"pipeline": {"categories": raw}}) == {
        UID: "12、日常分享",
        "nick": "13、街拍",
    }


def test_resolve_category_uid_wins_over_nickname():
    mapping = {UID: "12、日常分享", NICK: "13、街拍"}
    assert resolve_category(mapping, uid=UID, account=NICK) == "12、日常分享"


def test_resolve_category_falls_back_to_nickname_then_default():
    mapping = {UID: "12、日常分享", NICK: "13、街拍"}
    assert resolve_category(mapping, uid="999", account=NICK) == "13、街拍"
    assert resolve_category(mapping, uid="999", account="无名") == DEFAULT_CATEGORY
    assert resolve_category({}, uid=UID, account=NICK) == DEFAULT_CATEGORY


def test_resolve_category_ignores_blank_keys_and_values():
    mapping = {UID: "   "}
    assert resolve_category(mapping, uid="", account="") == DEFAULT_CATEGORY
    assert resolve_category(mapping, uid=UID, account="") == DEFAULT_CATEGORY


# ---------------- 清洗 ----------------


def test_sanitize_segment_replaces_illegal_and_control_chars():
    dirty = 'a<b>c:d"e|f?g*h' "\\" "i/j" "\x00k\r\n\tl"
    assert sanitize_segment(dirty) == "a b c d e f g h i j k l"


def test_sanitize_segment_collapses_whitespace_and_strips_dots():
    assert sanitize_segment("  ..名字..  ") == "名字"
    assert sanitize_segment("a   b\tc") == "a b c"
    assert sanitize_segment("...") == "未命名"
    assert sanitize_segment(" . . ") == "未命名"


def test_sanitize_segment_truncates_by_chars():
    assert sanitize_segment("长" * 200) == "长" * MAX_SEGMENT_LEN
    assert len(sanitize_segment("x" * 200)) == MAX_SEGMENT_LEN
    assert sanitize_segment("abcdefgh", limit=5) == "abcde"
    # 截断后落在点上要把点去掉
    assert sanitize_segment("abc.def", limit=4) == "abc"


def test_sanitize_segment_fallback_and_non_string():
    assert sanitize_segment("") == "未命名"
    assert sanitize_segment("   ") == "未命名"
    assert sanitize_segment("", fallback="空") == "空"
    assert sanitize_segment(None) == "未命名"
    assert sanitize_segment("..", fallback="") == ""


def test_sanitize_segment_keeps_fullwidth_punctuation_and_at():
    text = "１２：@博主【图集】"
    assert sanitize_segment(text) == text


# ---------------- 标题抽取 ----------------


def test_title_from_text_takes_first_nonempty_line():
    text = "\n\n  #话题#  今天  天气 很好 \n第二行"
    assert title_from_text(text) == "今天 天气 很好"


def test_title_from_text_strips_leading_hashtag_and_mention():
    assert title_from_text("#话题# @小明 你好呀") == "你好呀"
    assert title_from_text("@小明  你好") == "你好"


def test_title_from_text_truncates_and_falls_back():
    assert title_from_text("一二三四五六七八九十", limit=3) == "一二三"
    assert title_from_text("", fallback="未命名") == "未命名"
    assert title_from_text("   \n  ", fallback="未命名") == "未命名"
    assert title_from_text(None, fallback="未命名") == "未命名"


# ---------------- 目录渲染 ----------------


def test_render_directory_default_template():
    assert (
        render_directory(DEFAULT_DIR_TEMPLATE, make_ctx())
        == "12、日常分享/【微博图集@示例博主A】"
    )


def test_render_directory_keeps_unknown_placeholder():
    assert (
        render_directory("{category}/{foo}/{account}", make_ctx())
        == "12、日常分享/{foo}/示例博主A"
    )


def test_render_directory_drops_parent_segments():
    assert render_directory("{category}/../{account}/", make_ctx()) == "12、日常分享/示例博主A"
    assert render_directory("../", make_ctx()) == DEFAULT_CATEGORY


def test_render_directory_empty_template_falls_back():
    assert render_directory("", make_ctx()) == DEFAULT_CATEGORY
    assert render_directory("   ", make_ctx()) == DEFAULT_CATEGORY


def test_render_directory_normalizes_separators():
    assert render_directory("//{category}//{account}//", make_ctx()) == "12、日常分享/示例博主A"
    assert render_directory(r"{category}\{account}", make_ctx()) == "12、日常分享/示例博主A"


def test_render_directory_sanitizes_values_to_prevent_extra_levels():
    ctx = make_ctx(category="a/b", account="x*y:z")
    assert render_directory("{category}/{account}", ctx) == "a b/x y z"


def test_render_directory_truncates_long_values():
    ctx = make_ctx(account="账" * 200)
    assert render_directory("{category}/{account}", ctx) == "12、日常分享/" + "账" * MAX_SEGMENT_LEN


# ---------------- 文件名渲染 ----------------


def test_render_filename_default_template():
    assert (
        render_filename(DEFAULT_FILE_TEMPLATE, make_ctx())
        == "【微博@示例博主A】2026-10-31-3：阳光很好.jpg"
    )


def test_render_filename_keeps_fullwidth_colon():
    name = render_filename("{title}.jpg", make_ctx(title="第1：阳光很好"))
    assert name == "第1：阳光很好.jpg"


def test_render_filename_forbids_path_separator():
    name = render_filename("{title}.jpg", make_ctx(title="a/b"))
    assert "/" not in name
    assert name == "a b.jpg"


def test_render_filename_keeps_unknown_placeholder():
    assert render_filename("{title}-{foo}.jpg", make_ctx()) == "阳光很好-{foo}.jpg"


def test_render_filename_empty_render_falls_back():
    assert render_filename("", make_ctx()) == "image.jpg"
    assert render_filename("///", make_ctx()) == "image.jpg"


@pytest.mark.parametrize(
    ("ext", "expected"),
    [("png", "阳光很好.png"), (".png", "阳光很好.png"), ("", "阳光很好"), (None, "阳光很好")],
)
def test_render_filename_ext_handling(ext, expected):
    assert render_filename("{title}", make_ctx(ext=ext)) == expected


def test_render_filename_does_not_duplicate_extension():
    assert render_filename("a.jpg", make_ctx(ext="jpg")) == "a.jpg"
    assert render_filename("a.jpg", make_ctx(ext="JPG")) == "a.jpg"


def test_render_filename_replaces_conflicting_image_extension():
    name = render_filename(DEFAULT_FILE_TEMPLATE, make_ctx(ext="png"))
    assert name.endswith(".png")
    assert ".jpg.png" not in name


# ---------------- 缩略图与目录规整 ----------------


def test_thumbnail_name_replaces_at():
    assert (
        thumbnail_name("【微博@示例博主A】2026-10-31-3：阳光很好.jpg")
        == "【微博_示例博主A】2026-10-31-3：阳光很好.jpg"
    )
    assert thumbnail_name("") == ""


def test_thumbnail_folder():
    assert thumbnail_folder() == THUMBNAIL_FOLDER == "thumbnails/"


def test_normalize_relative_dir_rules():
    assert normalize_relative_dir("a//b/") == "a/b"
    assert normalize_relative_dir("/a/b") == "a/b"
    assert normalize_relative_dir(r"a\b") == "a/b"
    assert normalize_relative_dir("a/../b") == "a/b"
    assert normalize_relative_dir("../..") == ""
    assert normalize_relative_dir("  a / b  ") == "a/b"
    assert normalize_relative_dir(None) == ""
