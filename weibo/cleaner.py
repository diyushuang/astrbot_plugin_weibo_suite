r"""微博文案清洗：话题、链接、微博表情码与 Emoji 变体选择符。

逐条移植自原项目的 src/build_weibo_copy.js（Node 版）清理链路::

    forceEmojiPresentation(replaceEmotionCodes(stripLinks(stripTopics(text))))

语义必须与 Node 版逐字符一致——这是 F3（文案清洗进签到文案库）的地基。
本模块只用标准库：不 import astrbot、不 import trace。

已知（清理链路上不可达）的 JS/Python 空白集差异，未做等价处理：

- JS 的 \s / trim 把 U+FEFF 当空白，Python 不当；但 strip_topics 会先删掉全部 U+FEFF。
- Python 的 \s / strip 还认 \x1c-\x1f、\x85 为空白，JS 不认；微博正文语料无这些字符。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "EMOJI_PROPS_PATH",
    "WB_EMOJI",
    "WB_SYNONYM_OF",
    "clean_mblog_text",
    "clean_text",
    "escape_md_line_starts",
    "force_emoji_presentation",
    "load_emoji_props",
    "replace_emotion_codes",
    "strip_links",
    "strip_topics",
]

# <package>/weibo/cleaner.py -> <package>/data/emoji-props.json
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EMOJI_PROPS_PATH: Path = _DATA_DIR / "emoji-props.json"

# 只清理微博正文里用作填充的零宽空格和 BOM。
# U+200C/U+200D 必须保留：ZWJ 是 👨‍👩‍👧 / 🚶‍♀️ 这类 emoji 序列的连接符，删掉就会拆成两个字形。
INVISIBLE_RE = re.compile(r"[\u200B\uFEFF]")

# 成对话题：#…# / ＃…＃，话题内不跨行（[超话] 这类码随话题一起去掉）
PAIRED_TOPIC_RE = re.compile(r"[#＃][^#＃\n]*[#＃]")
# 微博文末话题可省略闭合 #：成对话题先删掉，这里只处理残留。
# 用 \Z 而非 $：JS 的 $（无 m 标志）只匹配输入绝对结尾，Python 的 $ 还匹配"末尾换行之前"。
# node v24 实测 /a$/.test("a\n") === false，故 \Z 才是等价位点。
TRAILING_TOPIC_RE = re.compile(r"[#＃][^#＃\n]{0,30}[ \t]*\Z")

# URL 后常紧跟中文（无空格），字符类必须排除 CJK 与中文标点，否则会吞掉正文
LINK_RE = re.compile(
    r"(?:https?|ftp)://[^\s\u3000\u4e00-\u9fff\u3000-\u303f\uff00-\uffef<>\"'）)】]+",
    re.IGNORECASE,
)

# 微博转发第三方内容时自动追加的样板；链接删掉后只剩没有所指的空壳
SHARE_BOILERPLATE_RE = re.compile(r"[（(]\s*分享自\s*@[^）)]*[）)]")

# 紧邻链接、且链接位于行尾时，这些引导语同样失去所指。不吃前面的标点，避免把"吧！戳"删成"吧"。
# alternation 顺序与 JS 保持一致：一键直达（可带 0~6 个非空白字符）必须排在 戳 之前。
POINTER_AT_LINE_END_RE = re.compile(
    r"(?:戳链接解锁|一键直达\S{0,6}|点击进入|长按识别|扫码查看|快戳|👉|➡\uFE0F?|戳|前往|直达)"
    r"[：:]?\s*\Z"
)

# 正文行首若正好是 Markdown 列表/标题/编号语法，会被渲染器吃掉，转义保留原样。
# 用 [0-9] 而非 \d：JS 的 \d 只匹配 ASCII 数字，Python 的 \d 还会匹配全角等 Unicode 数字。
MD_LINE_START_RE = re.compile(r"^([ \t]*)([-*+]|#{1,6}|[0-9]{1,3}[.)])(\s)")

# 微博表情码：[xx]，方括号内 1~12 个字符且不跨行。
# JS 的 {1,12} 按 UTF-16 码元计数，Python 按码位计数；微博表情码都在 BMP 内，两者等价。
EMOTION_CODE_RE = re.compile(r"\[[^\[\]\n]{1,12}\]")

_MULTI_SPACE_RE = re.compile(r"[ \t\u00a0]{2,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\Z")

# 数字/#/* 只在 keycap 序列里需要 VS16，©®™ 常作法律符号 —— 这三类不自动补，
# 否则 "2026" 会被逐个加 VS16。
NEVER_VS16: frozenset[int] = frozenset({0x23, 0x2A, 0xA9, 0xAE, 0x2122}) | frozenset(
    ord(char) for char in "0123456789"
)

# 微博默认表情码 -> Unicode emoji。优先选 Emoji 12.1（2019）及更早收录的码位，老设备也能显示。
# 值用 \uXXXX / \UXXXXXXXX 转义书写（含 VS16 等不可见字符），行尾注释给出实际字形。
_WB_EMOJI_BASE: dict[str, str] = {
    "[心]": "\u2764\ufe0f",  # ❤️
    "[爱你]": "\U0001f970",  # 🥰
    "[给你小心心]": "\U0001f495",  # 💕
    "[可爱]": "\U0001f60a",  # 😊
    "[太开心]": "\U0001f604",  # 😄
    "[哈哈]": "\U0001f606",  # 😆
    "[偷笑]": "\U0001f92d",  # 🤭
    "[坏笑]": "\U0001f60f",  # 😏
    "[亲亲]": "\U0001f618",  # 😘
    "[抱抱]": "\U0001f917",  # 🤗
    "[抱一抱]": "\U0001fac2",  # 🫂
    "[害羞]": "\u263a\ufe0f",  # ☺️
    "[挤眼]": "\U0001f609",  # 😉
    "[憧憬]": "\U0001f60d",  # 😍
    "[馋嘴]": "\U0001f924",  # 🤤
    "[思考]": "\U0001f914",  # 🤔
    "[并不简单]": "\U0001f9d0",  # 🧐
    "[允悲]": "\U0001f62d",  # 😭
    "[泪]": "\U0001f622",  # 😢
    "[苦涩]": "\U0001f616",  # 😖
    "[哇]": "\U0001f632",  # 😲
    "[笑cry]": "\U0001f602",  # 😂
    "[晕]": "\U0001f635",  # 😵
    "[闭嘴]": "\U0001f910",  # 🤐
    "[嘘]": "\U0001f92b",  # 🤫
    "[怒骂]": "\U0001f92c",  # 🤬
    "[可怜]": "\U0001f97a",  # 🥺
    "[酷]": "\U0001f60e",  # 😎
    "[加油]": "\U0001f4aa",  # 💪
    "[拳头]": "\u270a",  # ✊
    "[举手]": "\U0001f64b",  # 🙋
    "[揣手]": "\U0001f932",  # 🤲
    "[耶]": "\u270c\ufe0f",  # ✌️
    "[yeah]": "\U0001f91f",  # 🤟
    "[ok]": "\U0001f44c",  # 👌
    "[给力]": "\U0001f44d",  # 👍
    "[顶]": "\U0001f51d",  # 🔝
    "[赞啊]": "\U0001f44f",  # 👏
    "[吃瓜]": "\U0001f349",  # 🍉
    "[蜡烛]": "\U0001f56f\ufe0f",  # 🕯️
    "[太阳]": "\U0001f31e",  # 🌞
    "[月亮]": "\U0001f319",  # 🌙
    "[星星]": "\u2b50",  # ⭐
    "[鲜花]": "\U0001f490",  # 💐
    "[微风]": "\U0001f343",  # 🍃
    "[音乐]": "\U0001f3b5",  # 🎵
    "[浮云]": "\u2601\ufe0f",  # ☁️
    "[咖啡]": "\u2615",  # ☕
    "[蛋糕]": "\U0001f382",  # 🎂
    "[飞机]": "\u2708\ufe0f",  # ✈️
    "[雪花]": "\u2744\ufe0f",  # ❄️
    "[粽子]": "\U0001f359",  # 🍙
    "[喵喵]": "\U0001f431",  # 🐱
    "[兔子]": "\U0001f430",  # 🐰
    "[熊猫]": "\U0001f43c",  # 🐼
    "[doge]": "\U0001f415",  # 🐕
    "[二哈]": "\U0001f436",  # 🐶
    "[单身狗]": "\U0001f43e",  # 🐾
    "[猪头]": "\U0001f437",  # 🐷
    "[羞嗒嗒]": "\U0001f633",  # 😳
    "[打call]": "\U0001f4e3",  # 📣
    "[送花花]": "\U0001f339",  # 🌹
    "[努力]": "\U0001f624",  # 😤
    "[比心心]": "\U0001f497",  # 💗
    "[送福]": "\U0001f381",  # 🎁
    "[嘻嘻]": "\U0001f601",  # 😁
    "[困]": "\U0001f62a",  # 😪
    "[彩虹屁]": "\U0001f308",  # 🌈
    "[笑哈哈]": "\U0001f923",  # 🤣
    "[照相机]": "\U0001f4f7",  # 📷
    "[666]": "\U0001f919",  # 🤙
    "[好喜欢]": "\U0001f496",  # 💖
    "[玉兔]": "\U0001f407",  # 🐇
    "[Wow]": "\U0001f62e",  # 😮
    "[么么哒]": "\U0001f48b",  # 💋
    "[话筒]": "\U0001f3a4",  # 🎤
    "[我来了]": "\U0001f64c",  # 🙌
    "[相爱]": "\U0001f491",  # 💑
    "[你好]": "\U0001f44b",  # 👋
    "[委屈]": "\U0001f614",  # 😔
    "[抓狂]": "\U0001f92f",  # 🤯
    "[睡]": "\U0001f634",  # 😴
    "[爱慕]": "\U0001f63b",  # 😻
    "[桃花]": "\U0001f338",  # 🌸
    "[冲刺]": "\U0001f3c3",  # 🏃
    "[握手]": "\U0001f91d",  # 🤝
    "[跪了]": "\U0001f647",  # 🙇
    "[舔屏]": "\U0001f975",  # 🥵
    "[求关注]": "\U0001f440",  # 👀
    "[大红灯笼]": "\U0001f3ee",  # 🏮
    "[爱红包]": "\U0001f9e7",  # 🧧
    "[马上有福]": "\U0001f40e",  # 🐎
    "[马到成功]": "\U0001f3c7",  # 🏇
}

# 与上面某个码语义等价、刻意共用同一 emoji（不是映射缺陷，审计 C7 会放行这组）
WB_SYNONYM_OF: dict[str, str] = {
    "[比耶]": "[耶]",
    "[赞]": "[给力]",
    "[偷乐]": "[偷笑]",
    "[鼓掌]": "[赞啊]",
}


def _expand_synonyms(base: dict[str, str], synonyms: dict[str, str]) -> dict[str, str]:
    expanded = dict(base)
    for alias, target in synonyms.items():
        expanded[alias] = expanded[target]
    return expanded


WB_EMOJI: dict[str, str] = _expand_synonyms(_WB_EMOJI_BASE, WB_SYNONYM_OF)


@lru_cache(maxsize=8)
def _load_emoji_props_json(path: str) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"emoji-props 数据文件缺失：{file}")
    with file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_emoji_props(path: str | Path | None = None) -> dict[str, Any]:
    """读取由 emoji-data.txt 生成的 emoji 属性表；按路径缓存，缺文件直接报错。"""
    return _load_emoji_props_json(str(EMOJI_PROPS_PATH if path is None else Path(path)))


def _emoji_sets(props: dict[str, Any]) -> tuple[frozenset[int], frozenset[int]]:
    needs_vs16 = frozenset(cp for cp in props["needsVS16"] if cp not in NEVER_VS16)
    return needs_vs16, frozenset(props["modifiers"])


@lru_cache(maxsize=8)
def _emoji_sets_for_path(path: str) -> tuple[frozenset[int], frozenset[int]]:
    return _emoji_sets(_load_emoji_props_json(path))


def force_emoji_presentation(text: str, props: dict[str, Any] | None = None) -> str:
    """给 Emoji_Presentation=No 的码位补 U+FE0F；除插入的 VS16 外逐字符保持原样。"""
    needs_vs16, modifiers = (
        _emoji_sets_for_path(str(EMOJI_PROPS_PATH)) if props is None else _emoji_sets(props)
    )
    out: list[str] = []
    last = len(text) - 1
    for index, char in enumerate(text):
        out.append(char)
        if ord(char) not in needs_vs16:
            continue
        following = text[index + 1] if index < last else ""
        # 已经有 VS16 或 keycap 组合符时不再叠加
        if following in ("\ufe0f", "\u20e3"):
            continue
        # 肤色修饰符必须紧跟基字符：中间插 VS16 会让 ✌🏻 变成非 RGI 的 ✌️🏻，被拆成两个字形
        if following and ord(following) in modifiers:
            continue
        out.append("\ufe0f")
    return "".join(out)


def strip_topics(text: str) -> str:
    """删成对话题、文末省略闭合的 # 话题，并清掉 U+200B/U+FEFF（保留 ZWJ）。"""
    text = PAIRED_TOPIC_RE.sub("", text)
    text = INVISIBLE_RE.sub("", text)
    return TRAILING_TOPIC_RE.sub("", text)


def strip_links(text: str) -> str:
    """删链接与微博分享样板；整行只剩链接时顺带删掉上一行末尾失去所指的引导语。"""
    kept: list[str] = []
    for line in SHARE_BOILERPLATE_RE.sub("", text).split("\n"):
        matches = list(LINK_RE.finditer(line))
        if not matches:
            kept.append(line)
            continue
        last = matches[-1]
        # JS 用「最后一个」链接判断链接是否位于行尾
        link_ends_line = line[last.end() :].strip() == ""
        stripped = LINK_RE.sub("", line)
        if stripped.strip() == "":
            # 整行只有链接：失去所指的引导语可能留在上一行末尾
            if link_ends_line and kept:
                kept[-1] = POINTER_AT_LINE_END_RE.sub("", kept[-1])
            continue
        kept.append(POINTER_AT_LINE_END_RE.sub("", stripped) if link_ends_line else stripped)
    lines = [_TRAILING_SPACE_RE.sub("", _MULTI_SPACE_RE.sub(" ", line)) for line in kept]
    return "\n".join(line for line in lines if line.strip()).strip()


def escape_md_line_starts(text: str) -> str:
    """给行首的 Markdown 列表/标题/编号语法加反斜杠，避免被渲染器吃掉。"""
    return "\n".join(MD_LINE_START_RE.sub(r"\1\\\2\3", line) for line in text.split("\n"))


def replace_emotion_codes(text: str, mapping: dict[str, str] | None = None) -> str:
    """把 [xx] 微博表情码换成 Unicode emoji；未收录的品牌/活动贴纸保留原码。"""
    table = WB_EMOJI if mapping is None else mapping
    return EMOTION_CODE_RE.sub(lambda match: table.get(match.group(0)) or match.group(0), text)


def clean_text(text: str) -> str:
    """clean 链路：话题 -> 链接 -> 表情码 -> Emoji 呈现。"""
    return force_emoji_presentation(replace_emotion_codes(strip_links(strip_topics(text))))


def clean_mblog_text(text: str, long_text: str = "") -> str:
    """清洗单条微博正文；long_text 非空时以它为准（longtext 接口补回的全文）。"""
    return clean_text(long_text or text)
