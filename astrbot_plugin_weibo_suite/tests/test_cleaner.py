"""P5 文案清洗回归：Node 黄金基准 + 文本层/Emoji 层审计门禁。"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.weibo.cleaner import (
    EMOJI_PROPS_PATH,
    WB_EMOJI,
    WB_SYNONYM_OF,
    clean_mblog_text,
    clean_text,
    escape_md_line_starts,
    force_emoji_presentation,
    load_emoji_props,
    replace_emotion_codes,
    strip_links,
    strip_topics,
)

FIXTURE = Path(__file__).with_name("fixtures") / "copy_golden.json"
GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES: list[dict] = GOLDEN["cases"]
CASE_IDS = [f"{case['uid']}-{case['mid']}" for case in CASES]

ZWJ = "\u200d"
VS16 = "\ufe0f"
SKIN_TONES = "\U0001f3fb\U0001f3fc\U0001f3fd\U0001f3fe\U0001f3ff"
NEVER_VS16 = {0x23, 0x2A, 0xA9, 0xAE, 0x2122, *range(0x30, 0x3A)}

PAIRED_TOPIC_RE = re.compile(r"[#＃][^#＃\n]*[#＃]")
HTML_MARKUP_RE = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.IGNORECASE)
TRAILING_POINTER_RE = re.compile(
    r"(?:戳链接解锁|一键直达\S{0,6}|点击进入|长按识别|扫码查看|快戳|👉|"
    r"➡\ufe0f?|戳|前往|直达)[：:]?\s*$"
)
SHARE_BOILERPLATE_RE = re.compile(r"[（(]\s*分享自\s*@[^）)]*[）)]")


def _golden_output(case: dict) -> str:
    return clean_mblog_text(case["raw"], case["long_text"])


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_cleaner_matches_node_golden(case: dict) -> None:
    expected = case["expected"]
    got = _golden_output(case)
    if got == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            [expected],
            [got],
            fromfile=f"node:{case['uid']}/{case['mid']}",
            tofile=f"python:{case['uid']}/{case['mid']}",
            lineterm="",
        )
    )
    pytest.fail(f"清洗结果与 Node 黄金基准不一致\n{diff}", pytrace=False)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_text_layer_audit_b1_to_b9(case: dict) -> None:
    got = _golden_output(case)

    # B1/B2：成对话题与残留 # 都不能进入签到文案。
    assert not PAIRED_TOPIC_RE.search(got), f"B1 {case['mid']}: {got!r}"
    assert "#" not in got and "＃" not in got, f"B2 {case['mid']}: {got!r}"

    # B3/B5/B6：URL、HTML 和长文占位文案均不应残留。
    assert not any(token in got.lower() for token in ("http", "t.cn", "s.weibo.com")), (
        f"B3 {case['mid']}: {got!r}"
    )
    assert not HTML_MARKUP_RE.search(got), f"B5 {case['mid']}: {got!r}"
    assert "全文" not in got, f"B6 {case['mid']}: {got!r}"

    # B9：引导语只在失去所指时删除；正文里的“快戳链接”是原文，不能被误删。
    assert not SHARE_BOILERPLATE_RE.search(got), f"B9-share {case['mid']}: {got!r}"
    assert not any(TRAILING_POINTER_RE.search(line) for line in got.splitlines()), (
        f"B9-pointer {case['mid']}: {got!r}"
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_emoji_layer_audit_c1_to_c11(case: dict) -> None:
    got = _golden_output(case)
    props = load_emoji_props()
    modifiers = frozenset(props["modifiers"])
    modifier_bases = frozenset(props["modifierBase"])
    needs_vs16 = frozenset(props["needsVS16"]) - NEVER_VS16

    assert got.count(ZWJ) >= case["raw"].count(ZWJ), f"C1 {case['mid']}"
    assert VS16 + VS16 not in got, f"C2 {case['mid']}: {got!r}"
    assert "\ufffd" not in got, f"C4 {case['mid']}: {got!r}"
    assert not re.search(rf"{VS16}[{SKIN_TONES}]", got), f"C11 {case['mid']}: {got!r}"

    for index, char in enumerate(got):
        codepoint = ord(char)
        if codepoint in modifiers:
            assert index > 0 and ord(got[index - 1]) in modifier_bases, (
                f"C5 {case['mid']}: 肤色修饰符前导不是 modifierBase，位置 {index}: {got!r}"
            )
        if codepoint not in needs_vs16 or index + 1 >= len(got):
            continue
        following = got[index + 1]
        if ord(following) in modifiers:
            continue
        assert following == VS16, (
            f"C9 {case['mid']}: U+{codepoint:04X} 缺 VS16，位置 {index}: {got!r}"
        )


def test_all_mappings_are_covered_and_synonyms_are_explicit() -> None:
    codes = {
        match.group(0)
        for case in CASES
        for match in re.finditer(r"\[[^\[\]\n]{1,12}\]", case["raw"])
    }
    assert not set(WB_EMOJI) - codes, "黄金基准必须覆盖 WB_EMOJI 的每个映射"
    assert len(WB_EMOJI) == 97  # Node 版为 93 个基础码 + 4 个同义码
    assert all(isinstance(key, str) and value for key, value in WB_EMOJI.items())
    assert set(WB_SYNONYM_OF) == {"[比耶]", "[赞]", "[偷乐]", "[鼓掌]"}
    assert all(WB_EMOJI[alias] == WB_EMOJI[target] for alias, target in WB_SYNONYM_OF.items())

    # C7：反向一对多时，同组必须正好是一组已声明同义词。
    by_value: dict[str, set[str]] = {}
    for code, value in WB_EMOJI.items():
        by_value.setdefault(value, set()).add(code)
    declared: dict[str, set[str]] = {}
    for alias, target in WB_SYNONYM_OF.items():
        declared.setdefault(target, set()).add(alias)
    for codes_for_value in by_value.values():
        if len(codes_for_value) == 1:
            continue
        assert any(codes_for_value == targets | {target} for target, targets in declared.items()), (
            f"C7 未声明的同义映射组: {sorted(codes_for_value)}"
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("#话题#", ""),
        ("#话题# #另一个#   ", "    "),
        ("a#X#b", "ab"),
        ("正文 #文末省略", "正文 "),
        ("保留\u200d连字\u200b\ufeff", "保留\u200d连字"),
        ("正文#未闭合\n下一行", "正文#未闭合\n下一行"),
    ],
)
def test_strip_topics(text: str, expected: str) -> None:
    assert strip_topics(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://t.cn/abc", ""),
        ("正文https://t.cn/abc继续", "正文继续"),
        ("（分享自 @某博主）正文", "正文"),
        ("期待\n快戳\nhttps://t.cn/abc", "期待"),
        ("正文 👉 https://t.cn/abc", "正文"),
        ("不错吧！", "不错吧！"),
        ("第一行\nhttps://t.cn/a\n第二行", "第一行\n第二行"),
    ],
)
def test_strip_links(text: str, expected: str) -> None:
    assert strip_links(text) == expected


def test_clean_pipeline_and_long_text_override() -> None:
    assert clean_text("#早安# [心] https://t.cn/x") == "❤️"
    assert clean_mblog_text("列表截断 #话题#", "补回全文 #话题#") == "补回全文"


def test_markdown_line_starts_are_escaped() -> None:
    text = "1. 第一项\n- 第二项\n# 标题\n普通\n  2) 嵌套"
    assert escape_md_line_starts(text) == ("\\1. 第一项\n\\- 第二项\n\\# 标题\n普通\n  \\2) 嵌套")


def test_emotion_codes_keep_unknown_stickers() -> None:
    assert replace_emotion_codes("[心][小V开心][赞]") == "❤️[小V开心]👍"
    assert replace_emotion_codes("[心]", {"[心]": "X"}) == "X"


def test_emoji_props_source_and_counts() -> None:
    props = load_emoji_props()
    assert EMOJI_PROPS_PATH.is_file()
    assert props["source"].endswith("emoji-data.txt")
    assert props["version"].startswith("Date: ")
    assert props["counts"] == {
        "emoji": 1447,
        "emojiPresentation": 1228,
        "emojiModifier": 5,
        "needsVS16": 219,
    }
    assert len(props["modifiers"]) == 5
    assert props["modifierBase"]


def test_force_presentation_handles_zwj_skin_tone_keycap_and_symbols() -> None:
    assert force_emoji_presentation("©®™") == "©®™"
    assert force_emoji_presentation("1\ufe0f\u20e3") == "1\ufe0f\u20e3"
    assert force_emoji_presentation("\u270c\U0001f3fb") == "\u270c\U0001f3fb"
    assert force_emoji_presentation("\u270c") == "\u270c\ufe0f"
    assert force_emoji_presentation("\U0001f468\u200d\U0001f469\u200d\U0001f467") == (
        "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    )


def test_cleaner_performance_guard() -> None:
    text = ("#话题# 正文 [心] https://t.cn/abc\n" * 500)[:10_000]
    started = time.perf_counter()
    clean_text(text)
    assert time.perf_counter() - started < 0.05
