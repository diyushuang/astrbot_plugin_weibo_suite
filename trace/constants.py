"""插件级常量、配置布局映射与 ResolvedImage 等共享数据结构。"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from .features import ImageFeatures, avif_available, heif_available
    from .image_delivery import sniff_image_format
except ImportError:  # 兼容插件以独立模块方式加载
    from features import (  # type: ignore[no-redef]
        ImageFeatures,
        avif_available,
        heif_available,
    )
    from image_delivery import sniff_image_format  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_weibo_suite"
# 扫描目录认可的图片扩展名。TIFF 由 Pillow 原生支持；AVIF 只有 Pillow 11.3+
# 能解码，旧环境收进来只会让每张 AVIF 都记一条「扫描失败」，故按能力开关。
# HEIC/HEIF 同理：没有 pillow-heif 时每张都会解码失败，不应纳入扫描。
IMAGE_EXTS = (
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".jfif",
        ".tif",
        ".tiff",
    }
    | ({".heic", ".heif"} if heif_available() else set())
    | ({".avif"} if avif_available() else set())
)
# QQ 开放平台富媒体 file_type：1=图片、2=视频、3=语音、4=文件
QQ_IMAGE_FILE_TYPE = 1
# 假格式转码前的像素上限：与 features / 签到卡片同一量级，防解压炸弹
QQ_TRANSCODE_MAX_PIXELS = 50_000_000
# OneBot 直发的回执超时：协议端卡住时不能让事件协程永久挂起
ONEBOT_SEND_TIMEOUT = 30.0

# ----------------------------------------------------------------------
# 配置布局（1.7.14）：同类项合并进 basic / delivery / limits 三个组。
# 旧布局（顶层散项 + image_delivery / qq_upload 两组）继续可读——新键优先、
# 旧键回落，启动时由 _migrate_config_layout 落盘迁移，用户无需手动改配置。
# ----------------------------------------------------------------------

# 旧顶层键 → (新组, 新字段)
_SCALAR_PATHS: dict[str, tuple[str, str]] = {
    "search_engine": ("basic", "search_engine"),
    "scan_dirs": ("basic", "scan_dirs"),
    "similarity_threshold": ("basic", "hash_similarity_threshold"),
    "hash_size": ("basic", "hash_size"),
    "top_n": ("basic", "top_n"),
    "max_images_per_query": ("basic", "max_images_per_query"),
    "ai_verify": ("basic", "ai_verify"),
    "register_admin_only": ("basic", "register_admin_only"),
    "max_download_mb": ("limits", "max_download_mb"),
    "remote_hash_enabled": ("limits", "remote_hash_enabled"),
    "remote_hash_ttl": ("limits", "remote_hash_ttl"),
    "remote_hash_max_points": ("limits", "remote_hash_max_points"),
}

# 当前配置布局版本（写在隐藏项 config_layout 里，避免重复迁移覆盖用户新值）
_LAYOUT_VERSION = "3"

# 旧 image_delivery 字段 → delivery 组里的 NapCat 字段
_NAPCAT_KEY_MAP: dict[str, str] = {
    "mode": "napcat_mode",
    "max_side": "napcat_max_side",
    "quality": "napcat_quality",
    "verify_scaled": "napcat_verify_scaled",
    "scaled_url_style": "napcat_scaled_url_style",
    "target_kb": "napcat_target_kb",
    "webp": "napcat_webp",
    "onebot_image_timeout": "napcat_timeout",
}

# 旧 qq_upload 字段 → delivery 组里的 QQ 官方字段
_QQ_KEY_MAP: dict[str, str] = {
    "mode": "qq_mode",
    "direct_max_mb": "qq_native_max_mb",
    "auto_prefer": "qq_auto_prefer",
    "url_max_mb": "qq_url_max_mb",
    "max_upload_mb": "qq_max_upload_mb",
    "on_failure": "qq_on_failure",
}


def _describe_bad_image(path: str) -> tuple[str, str | None]:
    """坏产物摘要 + 按文件头识别的容器格式，供日志与失败分诊使用。

    第二项为 None 表示「连图片容器都认不出」（错误体、HTML、空文件），非
    None 表示「容器认得出但解不开」（缺解码器、截断）——两者的处置完全不同：
    前者才值得重下，后者重下也是同一份内容。函数为同步 IO，调用方应走
    asyncio.to_thread。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        size = os.path.getsize(path)
    except OSError:
        return "无法读取", None
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    return f"{size} 字节, 头部 {text!r}", sniff_image_format(head)


@dataclass
class ResolvedImage:
    """一次解析的产物：本地路径、临时文件标记、失败提示，以及可选的 MIME/特征。

    MIME 与 features 只在调用方要求的探测模式下填充。带 features 的解析表示
    “这一次解码已经完成格式校验 + 感知特征”，下游不得再为同一张图重复解码。
    """

    path: str = ""
    is_tmp: bool = False
    message: str = ""
    mime: str = ""
    features: ImageFeatures | None = None


# 退化 pHash（纯色/大面积平坦图）的对外提示语。登记、检索、重扫三处都用这一句，
# 避免同一个现象在不同入口得到互相矛盾的说法（"已登记" / "查不到" / "没命中"）。
DEGENERATE_MESSAGE = (
    "⚠️ 这张图是纯色或大面积平坦图，感知哈希退化为全零码，"
    "无法与其它同类图区分，已跳过（不入库、不参与检索）。"
)

# /溯源重扫 的「强制重建」开关：指令之后剩余的词必须全部是开关词。
# 子串匹配会让「扫描 C:\\force\\img」这类参数意外触发强制重建，
# 而 force 的代价是整库重算。
_FORCE_TOKENS = frozenset({"force", "强制", "重建"})
# 开关词两侧常见的标点（/溯源重扫 force。 / 重建！）要先剥掉再比对
_FORCE_TRIM_CHARS = "。.!！?？~～：:、\"'“”‘’()（）[]【】"

# 下载流落盘批大小：每积累这么多字节在线程池里写一次盘。太小会放大线程切换
# 开销，太大则单次写盘变长（并多占内存）；1 MiB 约等于 16 个 64 KiB 分片。
_STREAM_WRITE_BATCH_BYTES = 1 << 20

# 储存桶（对象存储）独立配置组；v1.2.x 曾作为 image_bed 的模式，迁移见
# _migrate_bucket_config。完整判定与 image_bed.py 的 _require / 公开直链
# 校验口径一致：缺任何一项上传都必然回退本地副本，不满足就不优先于图床
BUCKET_MODES = ("cloudflare_r2", "oracle_oci")
BUCKET_REQUIRED = {
    "cloudflare_r2": (
        "r2_account_id",
        "r2_access_key_id",
        "r2_secret_access_key",
        "r2_bucket",
        "r2_public_base_url",
    ),
    "oracle_oci": (
        "oci_namespace",
        "oci_region",
        "oci_access_key_id",
        "oci_secret_access_key",
        "oci_bucket",
    ),
}
# v1.2.x image_bed 组里可能遗留的储存桶字段（迁移时搬走并清理）
BUCKET_LEGACY_FIELDS = (
    "r2_account_id",
    "r2_access_key_id",
    "r2_secret_access_key",
    "r2_bucket",
    "r2_endpoint",
    "r2_public_base_url",
    "oci_namespace",
    "oci_region",
    "oci_access_key_id",
    "oci_secret_access_key",
    "oci_bucket",
    "oci_endpoint",
    "oci_public_bucket",
    "oci_public_base_url",
)
