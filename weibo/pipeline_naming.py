"""图床/缩略图/向量管线的目录与文件名模板渲染（纯函数模块）。

本模块只做字符串运算：不 import astrbot、不 import trace、不碰数据库与文件系统，
因此可以在 worker 线程里安全调用，也便于完全离线的单测。

命名规则来自现网 img-indexer 的既有产物（2.4 万个向量点）：
目录 `{category}/【微博图集@{account}】/`、
文件名 `【微博@{account}】{date}-{index}：{title}.jpg`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

DEFAULT_DIR_TEMPLATE = "{category}/【微博图集@{account}】/"
DEFAULT_FILE_TEMPLATE = "【微博@{account}】{date}-{index}：{title}.jpg"
DEFAULT_CATEGORY = "未分类"
THUMBNAIL_FOLDER = "thumbnails/"
MAX_SEGMENT_LEN = 80

# Windows/POSIX 双平台都不允许出现在文件名里的字符 + 控制字符（含 NUL）。
# 全角字符（如 "："）不在集合内，必须原样保留：现网文件名就用全角冒号。
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_WHITESPACE_RE = re.compile(r"\s+")
# 只替换已知占位符，未知占位符原样保留（配置里写错名字不能被静默吃掉）
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_LEADING_HASHTAG_RE = re.compile(r"^#[^#\n]{1,50}#\s*")
_LEADING_MENTION_RE = re.compile(r"^@[\w·-]{1,30}\s*")
_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff"})


@dataclass(frozen=True)
class NamingContext:
    """渲染一份目录/文件名所需的全部字段。

    frozen：同一个上下文会被同帖多张图复用，渲染过程绝不允许改它。
    """

    account: str  # 博主昵称，如 "示例博主"
    category: str  # 分类目录名，如 "12、日常分享"
    date: str  # "YYYY-MM-DD"
    index: int  # 该帖内第几张，1 起
    title: str  # 取自正文的短标题
    ext: str = "jpg"


def naming_templates(config: dict) -> tuple[str, str]:
    """返回（目录模板, 文件名模板），未渲染、仍带花括号。

    配置键为 `pipeline.naming_directory` / `pipeline.naming_file`；
    缺失、空白或非字符串时回落默认模板。自定义模板去掉首尾空白。
    """
    section = _pipeline_section(config)
    directory = _template_or_default(section.get("naming_directory"), DEFAULT_DIR_TEMPLATE)
    file_template = _template_or_default(section.get("naming_file"), DEFAULT_FILE_TEMPLATE)
    return directory, file_template


def category_map(config: dict) -> dict[str, str]:
    """读取博主 -> 分类目录名映射。

    `pipeline.categories` 允许是 dict，也允许是面板 textarea 存下来的 JSON
    字符串。键可以是 uid 或昵称，值必须是非空字符串；非法项跳过、绝不抛异常。
    """
    raw = _pipeline_section(config).get("categories")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        clean_key = key.strip()
        clean_value = value.strip()
        if not clean_key or not clean_value:
            continue
        mapping[clean_key] = clean_value
    return mapping


def resolve_category(mapping: dict[str, str], *, uid: str, account: str) -> str:
    """先按 uid 查分类，再按昵称查；都没有时返回 DEFAULT_CATEGORY。"""
    if not isinstance(mapping, dict):
        return DEFAULT_CATEGORY
    for key in (uid, account):
        if not isinstance(key, str):
            continue
        clean_key = key.strip()
        if not clean_key:
            continue
        value = mapping.get(clean_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_CATEGORY


def sanitize_segment(text: str, *, fallback: str = "未命名", limit: int = MAX_SEGMENT_LEN) -> str:
    """把任意文本清洗成一个可用的路径段（目录名或文件名）。

    反斜杠、正斜杠、``: * ? " < > |`` 与全部控制字符（含换行、制表符、NUL）
    替换为空格；连续空白合并为一个空格；去掉首尾空白与首尾点（避免 "." / ".." 段）；
    超长按字符截断到 `limit`，截断后再次去尾点/尾空白；
    结果为空时返回 `fallback`（fallback 本身也走同一套清洗）。
    """
    cleaned = _clean_segment(text, limit)
    if cleaned:
        return cleaned
    return _clean_segment(fallback, limit)


def title_from_text(text: str, *, limit: int = 24, fallback: str = "") -> str:
    """从微博正文里取一个短标题：第一段非空行，去掉行首话题与 @ 提及。"""
    if not isinstance(text, str):
        return fallback
    for raw_line in text.splitlines():
        line = _strip_leading_noise(raw_line.strip())
        line = _WHITESPACE_RE.sub(" ", line).strip()
        if not line:
            continue
        if isinstance(limit, int) and limit > 0 and len(line) > limit:
            line = line[:limit].strip()
        return line or fallback
    return fallback


def render_directory(template: str, ctx: NamingContext) -> str:
    """渲染目录模板，返回不带首尾 "/" 的相对目录；空结果回落到未分类。"""
    normalized = normalize_relative_dir(_render(template, ctx))
    if not normalized:
        return DEFAULT_CATEGORY
    parts = [sanitize_segment(part, fallback="") for part in normalized.split("/")]
    result = normalize_relative_dir("/".join(part for part in parts if part))
    return result or DEFAULT_CATEGORY


def render_filename(template: str, ctx: NamingContext) -> str:
    """渲染文件名并清洗；文件名里同样不允许出现路径分隔符。"""
    name = sanitize_segment(_render(template, ctx), fallback="")
    ext = _normalize_ext(ctx.ext)
    if not name:
        name = "image.jpg"
    return _ensure_ext(name, ext)


def normalize_relative_dir(path: str) -> str:
    """规整相对目录：统一 "/"、去首尾 "/"、折叠重复 "/"、丢弃 ".." 段。"""
    if not isinstance(path, str):
        return ""
    parts = []
    for segment in path.replace("\\", "/").split("/"):
        segment = segment.strip()
        if not segment or segment == "..":
            continue
        parts.append(segment)
    return "/".join(parts)


def thumbnail_name(filename: str) -> str:
    """缩略图名 = 原文件名里的 "@" 换成 "_"（图床缩略图流程的就地替换约定）。"""
    if not isinstance(filename, str):
        return ""
    return filename.replace("@", "_")


def thumbnail_folder() -> str:
    """缩略图固定传到图床的 thumbnails/ 目录。"""
    return THUMBNAIL_FOLDER


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------


def _pipeline_section(config: dict) -> dict:
    """取 config["pipeline"]；缺失或类型不对时返回空 dict。"""
    if not isinstance(config, dict):
        return {}
    section = config.get("pipeline")
    return section if isinstance(section, dict) else {}


def _template_or_default(value, default: str) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return default


def _clean_segment(text, limit) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = _ILLEGAL_RE.sub(" ", text)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip(" .")
    if isinstance(limit, int) and limit >= 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit].strip(" .")
    return cleaned


def _strip_leading_noise(line: str) -> str:
    """反复剥掉行首的 #话题# 与 @提及；只处理行首，正文里的原样保留。"""
    while True:
        stripped = _LEADING_HASHTAG_RE.sub("", line, count=1)
        stripped = _LEADING_MENTION_RE.sub("", stripped, count=1)
        stripped = stripped.strip()
        if stripped == line:
            return stripped
        line = stripped


def _normalize_ext(ext) -> str:
    """扩展名归一：去空白与首部点，非法字符清空；空扩展名表示不写扩展名。"""
    if not isinstance(ext, str):
        return ""
    return sanitize_segment(ext.strip().strip("."), fallback="")


def _render(template, ctx: NamingContext) -> str:
    """替换已知占位符；每个值先单独清洗，防止值里的 "/" 造出额外目录。"""
    if not isinstance(template, str):
        template = ""
    values = {
        "category": sanitize_segment(ctx.category),
        "account": sanitize_segment(ctx.account),
        "date": sanitize_segment(ctx.date),
        "index": sanitize_segment(str(ctx.index)),
        "title": sanitize_segment(ctx.title),
        "ext": _normalize_ext(ctx.ext),
    }
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def _ensure_ext(name: str, ext: str) -> str:
    """确保文件名以 "." + ext 结尾；模板自带同名扩展名时不重复追加。"""
    if not ext:
        return name
    suffix = f".{ext}"
    if name.lower().endswith(suffix.lower()):
        return name
    head, dot, tail = name.rpartition(".")
    if dot and tail.lower() in _IMAGE_EXTS:
        name = head
    return f"{name}{suffix}"


__all__ = [
    "DEFAULT_CATEGORY",
    "DEFAULT_DIR_TEMPLATE",
    "DEFAULT_FILE_TEMPLATE",
    "MAX_SEGMENT_LEN",
    "THUMBNAIL_FOLDER",
    "NamingContext",
    "category_map",
    "naming_templates",
    "normalize_relative_dir",
    "render_directory",
    "render_filename",
    "resolve_category",
    "sanitize_segment",
    "thumbnail_folder",
    "thumbnail_name",
    "title_from_text",
]
