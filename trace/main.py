"""AstrBot 图片溯源插件（门面模块）。

业务实现按职责拆在 state/resolve/delivery/trace/library/original/checkin/qq_upload
八个 mixin 中；本模块只保留常量再导出、插件类组装、初始化与生命周期，
对外符号与拆分前完全一致。

群内发送或引用图片 + 触发指令 -> 提取图片 -> 按当前引擎检索原图并回传：

- 哈希引擎：本地计算感知哈希特征（pHash/dHash/aHash），与本地图库比对
  汉明距离（检索仅用 pHash，dHash/aHash 预留作二级确认字段）；
- 向量引擎：调用多模态向量 AI 生成向量，到 Qdrant 向量库检索图床图片；
- auto 引擎：向量优先，未命中或不可用时回退哈希。

- QQ 消息的接收与发送完全通过 AstrBot 事件与消息组件 API 完成；
- 所有出网请求（下载兜底/图床/向量服务）统一经 url_guard 做 SSRF 校验；
- 可选使用当前会话的视觉大模型对哈希命中结果做二次复核（ai_verify 开关）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import os
import random
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse, urlsplit

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools

from ..shared.errorlog import install_error_log, remove_error_log

try:
    from ..shared.common import (
        AI_VERIFY_MAX_CANDIDATES,
        COMPRESS_CONCURRENCY,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        SCAN_CONCURRENCY,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        bool_or,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        sanitize_error_text,
        truthy,
    )
    from ..shared.http import GuardedHttpClient, read_limited_bytes
    from .checkin_card import DEFAULT_QUOTES, QuoteLibrary, compose_poster
    from .checkin_store import CheckinStore
    from .features import (
        ImageFeatures,
        avif_available,
        compute_features,
        compute_features_with_mime,
        heif_available,
        image_bytes_ok,
        image_file_ok,
        image_mime,
        is_degenerate_phash,
        phash_hex_len,
    )
    from .hash_index import REMOTE_HASH_PAYLOAD_KEYS, HashIndex
    from .image_bed import MODE_CFB, ImageBedClient, redact_url
    from .image_delivery import (
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_imgbed_file_url,
        build_original_url,
        build_scaled_url,
        choose_qq_upload_channel,
        classify_probe_status,
        classify_send_error,
        compression_evidence,
        content_length_from_headers,
        deduplicate_vector_hits,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_public_http_url,
        is_scaling_capability_rejection,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        qq_upload_safe_image,
        qq_upload_settings,
        send_diagnosis,
        sniff_image_format,
        upgrade_to_https,
    )
    from .library import DEGENERATE_SKIP, ImageLibrary
    from .media_history import MediaHistory
    from .random_media import (
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        RandomMediaError,
        clean_display_name,
        extract_directory,
        is_thumbnail_url,
        media_filename,
        media_kind,
        pick_default_directory,
        strip_leading_decoration,
        swap_url_origin,
    )
    from .vector_search import VectorEngine, VectorEngineError
    from .weibo_posts import (
        WeiboPost,
        load_post_map,
        load_posts_from_dir,
        map_is_stale,
        parse_weibo_text,
        posts_from_archive_rows,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from checkin_card import DEFAULT_QUOTES, QuoteLibrary, compose_poster  # type: ignore[no-redef]
    from checkin_store import CheckinStore  # type: ignore[no-redef]
    from features import (  # type: ignore[no-redef]
        ImageFeatures,
        avif_available,
        compute_features,
        compute_features_with_mime,
        heif_available,
        image_bytes_ok,
        image_file_ok,
        image_mime,
        is_degenerate_phash,
        phash_hex_len,
    )
    from hash_index import REMOTE_HASH_PAYLOAD_KEYS, HashIndex  # type: ignore[no-redef]
    from image_bed import MODE_CFB, ImageBedClient, redact_url  # type: ignore[no-redef]
    from image_delivery import (  # type: ignore[no-redef]
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_imgbed_file_url,
        build_original_url,
        build_scaled_url,
        choose_qq_upload_channel,
        classify_probe_status,
        classify_send_error,
        compression_evidence,
        content_length_from_headers,
        deduplicate_vector_hits,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_public_http_url,
        is_scaling_capability_rejection,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        qq_upload_safe_image,
        qq_upload_settings,
        send_diagnosis,
        sniff_image_format,
        upgrade_to_https,
    )
    from library import DEGENERATE_SKIP, ImageLibrary  # type: ignore[no-redef]
    from media_history import MediaHistory  # type: ignore[no-redef]
    from random_media import (  # type: ignore[no-redef]
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        RandomMediaError,
        clean_display_name,
        extract_directory,
        is_thumbnail_url,
        media_filename,
        media_kind,
        pick_default_directory,
        strip_leading_decoration,
        swap_url_origin,
    )
    from vector_search import VectorEngine, VectorEngineError  # type: ignore[no-redef]
    from weibo_posts import (  # type: ignore[no-redef]
        WeiboPost,
        load_post_map,
        load_posts_from_dir,
        map_is_stale,
        parse_weibo_text,
        posts_from_archive_rows,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        AI_VERIFY_MAX_CANDIDATES,
        COMPRESS_CONCURRENCY,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        SCAN_CONCURRENCY,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        bool_or,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        sanitize_error_text,
        truthy,
    )
    from astrbot_plugin_weibo_suite.shared.http import (
        GuardedHttpClient,  # type: ignore[no-redef]
        read_limited_bytes,  # type: ignore[no-redef]
    )

try:
    from .checkin_mixin import CheckinMixin
    from .constants import (
        _FORCE_TOKENS,
        _FORCE_TRIM_CHARS,
        _LAYOUT_VERSION,
        _NAPCAT_KEY_MAP,
        _QQ_KEY_MAP,
        _SCALAR_PATHS,
        _STREAM_WRITE_BATCH_BYTES,
        BUCKET_LEGACY_FIELDS,
        BUCKET_MODES,
        BUCKET_REQUIRED,
        DEGENERATE_MESSAGE,
        IMAGE_EXTS,
        ONEBOT_SEND_TIMEOUT,
        PLUGIN_NAME,
        QQ_IMAGE_FILE_TYPE,
        QQ_TRANSCODE_MAX_PIXELS,
        ResolvedImage,
        _describe_bad_image,
    )
    from .delivery_mixin import DeliveryMixin
    from .library_mixin import LibraryMixin
    from .original_mixin import OriginalMixin
    from .qq_upload_mixin import QQUploadMixin
    from .resolve_mixin import ResolveMixin
    from .state_mixin import StateMixin
    from .trace_mixin import TraceMixin
except ImportError:  # 兼容插件以独立模块方式加载
    from checkin_mixin import CheckinMixin  # type: ignore[no-redef]
    from delivery_mixin import DeliveryMixin  # type: ignore[no-redef]
    from library_mixin import LibraryMixin  # type: ignore[no-redef]
    from original_mixin import OriginalMixin  # type: ignore[no-redef]
    from qq_upload_mixin import QQUploadMixin  # type: ignore[no-redef]
    from resolve_mixin import ResolveMixin  # type: ignore[no-redef]
    from state_mixin import StateMixin  # type: ignore[no-redef]
    from trace_mixin import TraceMixin  # type: ignore[no-redef]

    from constants import (  # type: ignore[no-redef]
        _FORCE_TOKENS,
        _FORCE_TRIM_CHARS,
        _LAYOUT_VERSION,
        _NAPCAT_KEY_MAP,
        _QQ_KEY_MAP,
        _SCALAR_PATHS,
        _STREAM_WRITE_BATCH_BYTES,
        BUCKET_LEGACY_FIELDS,
        BUCKET_MODES,
        BUCKET_REQUIRED,
        DEGENERATE_MESSAGE,
        IMAGE_EXTS,
        ONEBOT_SEND_TIMEOUT,
        PLUGIN_NAME,
        QQ_IMAGE_FILE_TYPE,
        QQ_TRANSCODE_MAX_PIXELS,
        ResolvedImage,
        _describe_bad_image,
    )


class ImageTracePlugin(
    StateMixin,
    ResolveMixin,
    DeliveryMixin,
    TraceMixin,
    LibraryMixin,
    OriginalMixin,
    CheckinMixin,
    QQUploadMixin,
    Star,
):
    """图片溯源插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        self._errlog = None
        self._errlog_target = None
        os.makedirs(self.tmp_dir, exist_ok=True)

        # 旧布局先搬进 basic / delivery / limits，后续读取一律走新路径
        self._migrate_config_layout()
        # pHash 十六进制长度 = phash_hex_len(hash_size)（packbits 补齐到字节
        # 边界）；hash_size 必须为 4 的倍数，否则长度公式与旧数据对不上，
        # 全库记录都会被判为不匹配、索引静默清空
        settings = self._parse_runtime_settings()
        self.hash_size = int(settings["hash_size"])
        self.library = ImageLibrary(
            os.path.join(self.data_dir, "library.db"),
            expected_phash_hex_len=phash_hex_len(self.hash_size),
        )
        self.hash_threshold = float(settings["hash_threshold"])
        # 配置上限：这两个值直接放大网络请求量与消息长度，手改离谱值时必须收敛
        self.top_n = int(settings["top_n"])
        self.max_images_per_query = int(settings["max_images_per_query"])
        # v1.2.x 的储存桶配置先迁移，再合并（储存桶完整时优先于图床）
        self._migrate_bucket_config()
        self._bed_cfg = self._effective_bed_cfg()
        self._http: GuardedHttpClient | None = None
        # 内网直连白名单按实例持有：交给本实例的 GuardedHttpClient，避免多个
        # 插件实例通过模块级全局集合互相覆盖（旧实现 terminate 会清掉别人的白名单）
        self._allowed_private_hosts: set[str] = set()
        self.bed = ImageBedClient(self.data_dir, self._bed_cfg, self._get_http)
        self.vector = VectorEngine(config, self._get_http)
        # 图床感知哈希索引：把 img-indexer 入库时预置的 pHash 拉到本地，
        # 让哈希引擎也能反查图床里的图（scan_dirs 只能扫本地目录，覆盖不到）
        self.remote_hash = HashIndex(
            os.path.join(self.data_dir, "remote_phash.json"),
            expected_phash_hex_len=phash_hex_len(self.hash_size),
        )
        # 设置页保存后重建上述快照时串行化，避免两次保存并发替换到一半
        self._reinit_lock = asyncio.Lock()
        self._remote_hash_task: asyncio.Task | None = None
        # 刷新失败/空结果的退避截止时间：否则每次 /溯源 都会重翻全量 Qdrant
        self._remote_hash_backoff_until = 0.0
        # 回传过的原图直链的历史（供 /原图 找回）；随机图客户端惰性构造
        self.history = MediaHistory()
        # QQ 官方被动回复的 msg_seq：同一 msg_id 内必须唯一，用自增而非随机
        self._qq_msg_seq = 0
        self._random_media_client: RandomMediaClient | None = None
        # 每日签到：独立 SQLite 记录（按用户全局一天一次）与语句库（首次取句时构造）。
        # 签到是附属功能：DB 打不开时降级为「签到不可用」，不能拖垮整个插件加载。
        self.checkin_store: CheckinStore | None = None
        try:
            self.checkin_store = CheckinStore(os.path.join(self.data_dir, "checkin.db"))
        except Exception as exc:
            logger.error(f"签到数据库初始化失败，/签到 将不可用: {exc}")
        self._quote_library_cache: QuoteLibrary | None = None
        # 微博签到：文案源目录 + 预生成映射表。映射表由 scripts/build_weibo_map.py
        # 离线生成，签到只读本地 JSON，不请求微博、不算 pHash。
        self.weibo_dir = os.path.join(self.data_dir, "weibo")
        self.weibo_thumb_dir = os.path.join(self.weibo_dir, "thumbs")
        try:
            os.makedirs(self.weibo_thumb_dir, exist_ok=True)
        except OSError as exc:
            logger.warning(f"微博文案目录创建失败，微博签到将回退随机语句: {exc}")
        # 外部文案源（P5）：入口 main.py 注入 (指纹, 行列表) 供应器，来源是 archive.db。
        # 溯源域不 import 文案域，只认这个窄端口；未注入时回退 weibo/ 下的 CSV/MD。
        self._weibo_external_source = None
        self._weibo_external_cache: tuple[str, list[dict]] = ("", [])
        self._weibo_posts_cache: list[WeiboPost] | None = None
        self._weibo_meta_cache: dict = {}
        self._weibo_signature: tuple | None = None
        self._weibo_empty_warned = False
        # 图床缩放能力冷却截止时刻（time.monotonic 口径，0 表示未观测到拒绝）。
        # 探测到 API 级拒绝（405/501 等）后的一段时间内不再探测缩放版。
        self._scaled_verify_unsupported_until = 0.0
        # QQ URL 上传通道冷却截止时刻：图床公网域名存在但腾讯侧拉不到时，auto
        # 模式在一段时间内直接分片，避免每次都先白撞一次必败的 URL 上传
        self._qq_url_unsupported_until = 0.0
        self._cleanup_tmp()
        # 先吃磁盘缓存（同步、瞬时），网络刷新交给生命周期钩子里的后台任务——
        # __init__ 里不能建 Task（此时还没有运行中的事件循环）
        cache_note = ""
        if self.remote_hash_on() and self.remote_hash.load_cache():
            cache_note = f"（图床哈希缓存 {self.remote_hash.count} 条）"
        engine = self._engine_choice()
        vector_note = "（向量引擎已启用）" if self.vector.enabled and engine != "hash" else ""
        bucket_note = (
            f"（储存桶 {self._bed_mode()} 优先）" if self._bed_mode() in BUCKET_MODES else ""
        )
        logger.info(
            f"图片溯源插件已加载，当前图库共 {self.library.count()} 条，"
            f"引擎={engine} {vector_note}{bucket_note}{cache_note}"
        )

    # ------------------------------------------------------------------
    # 运行期配置重建
    # ------------------------------------------------------------------

    def _parse_runtime_settings(self) -> dict[str, object]:
        """解析可热更新的溯源域配置；__init__ 与设置页重建共用这一份规则。"""
        hash_size = as_int(self._cfg("hash_size"), 16)
        if not 4 <= hash_size <= 32 or hash_size % 4 != 0:
            logger.warning(
                f"hash_size 配置值 {hash_size} 无效（须为 4~32 之间 4 的倍数，"
                "如 8/16/24/32），已回退为默认 16。"
            )
            hash_size = 16
        return {
            "hash_size": hash_size,
            "hash_threshold": max(0.0, min(1.0, self._float_cfg("similarity_threshold", 0.85))),
            "top_n": max(0, min(50, self._int_cfg("top_n", 3))),
            "max_images_per_query": max(1, min(10, self._int_cfg("max_images_per_query", 3))),
        }

    async def reinit_runtime_settings(self) -> dict[str, object]:
        """设置页保存后重建溯源域运行期快照。

        只替换引用、不关闭旧对象：正在执行的命令与管线仍持有旧 client，
        让它们自然跑完再被 GC 回收，避免重建打断在跑任务。新请求立即使用
        新配置，因此不需要依赖宿主 save_config 是否触发热重载。
        """
        async with self._reinit_lock:
            settings = self._parse_runtime_settings()
            old_hash = self.hash_size
            self.hash_size = int(settings["hash_size"])
            self.hash_threshold = float(settings["hash_threshold"])
            self.top_n = int(settings["top_n"])
            self.max_images_per_query = int(settings["max_images_per_query"])
            self._bed_cfg = self._effective_bed_cfg()
            self.bed = ImageBedClient(self.data_dir, self._bed_cfg, self._get_http)
            self.vector = VectorEngine(self.config, self._get_http)
            if self.hash_size != old_hash:
                # hash_size 变了，pHash 十六进制长度口径随之变化；不重建索引
                # 会让新旧两种长度的记录互相比对失败。旧 library 不显式 close，
                # 交给仍在使用它的 in-flight 任务结束后回收。
                self.library = ImageLibrary(
                    os.path.join(self.data_dir, "library.db"),
                    expected_phash_hex_len=phash_hex_len(self.hash_size),
                )
                self.remote_hash = HashIndex(
                    os.path.join(self.data_dir, "remote_phash.json"),
                    expected_phash_hex_len=phash_hex_len(self.hash_size),
                )
                self.remote_hash.load_cache()
                self._remote_hash_backoff_until = 0.0
            engine = self._engine_choice()
            logger.info(
                f"设置页已重建溯源域运行期配置：hash_size={self.hash_size} "
                f"引擎={engine} 图床={self._bed_mode()}"
            )
            return {
                "hash_size": self.hash_size,
                "hash_size_changed": self.hash_size != old_hash,
                "engine": engine,
                "bed_mode": self._bed_mode(),
            }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _stop_remote_hash_refresh(self, errors: list) -> None:
        """停掉后台刷新任务，避免卸载后任务还在写缓存文件。

        取消防抖：后台任务可能正卡在 47 页翻页中间，直接 await 会拖慢卸载；
        给 5 秒宽限，超时就放弃等待（任务自身会在下一轮 await 点收到取消）。
        """
        task = self._remote_hash_task
        self._remote_hash_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception as e:
            errors.append(f"图床哈希刷新任务: {e}")

    async def initialize(self) -> None:
        """AstrBot 在事件循环起来后调用；此时才可以安全创建后台任务。

        图床哈希索引的刷新必须在这里启动，不能在 __init__：那会儿还没有运行中
        的事件循环，asyncio.create_task 会直接抛错。缓存缺失或超过
        remote_hash_ttl 时才刷新；缓存新鲜则跳过，避免每次重启都翻一遍全量
        Qdrant。
        """
        # 统一错误留存：从溯源域开始接入，后续监控/归档/管线共用同一份日志。
        install_error_log(self, Path(self.data_dir) / "errors.log")
        # 提前注册内网直连主机白名单：/原图 可能先于任何随机图命令运行，
        # 而它要取的直链可能是历史里记下的内网地址
        self._random_settings()
        self._maybe_start_remote_hash_refresh()

    async def terminate(self):
        errors: list[str] = []
        # 本实例的内网直连白名单随插件卸载清空（模块级全局已不再被本实例使用）
        self._allowed_private_hosts.clear()
        remove_error_log(self)
        await self._stop_remote_hash_refresh(errors)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception as e:
                errors.append(f"HTTP 会话: {e}")
        try:
            await self.bed.close()
        except Exception as e:
            errors.append(f"图床客户端: {e}")
        try:
            self._cleanup_tmp()
        except Exception as e:
            errors.append(f"临时文件: {e}")
        try:
            self.library.close()
        except Exception as e:
            errors.append(f"图库数据库: {e}")
        if self.checkin_store is not None:
            try:
                self.checkin_store.close()
            except Exception as e:
                errors.append(f"签到数据库: {e}")
        if errors:
            logger.error("图片溯源插件卸载时部分资源清理失败: " + "；".join(errors))
        logger.info("图片溯源插件已卸载。")


__all__ = [
    "AI_VERIFY_MAX_CANDIDATES",
    "BUCKET_LEGACY_FIELDS",
    "BUCKET_MODES",
    "BUCKET_REQUIRED",
    "COMPRESS_CONCURRENCY",
    "DEFAULT_QUOTES",
    "DEGENERATE_MESSAGE",
    "DEGENERATE_SKIP",
    "DOWNLOAD_CHUNK_SIZE",
    "DOWNLOAD_TIMEOUT",
    "IMAGE_EXTS",
    "MEMORY_DOWNLOAD_MAX_BYTES",
    "MODE_CFB",
    "ONEBOT_IMAGE_TIMEOUT",
    "ONEBOT_SEND_TIMEOUT",
    "PLAN_TOTAL_TIMEOUT",
    "PLUGIN_NAME",
    "PROBE_CONCURRENCY",
    "PROBE_TIMEOUT",
    "QQ_IMAGE_FILE_TYPE",
    "QQ_TRANSCODE_MAX_PIXELS",
    "QQ_URL_COOLDOWN_SECONDS",
    "RANDOM_ENDPOINT_DEFAULT",
    "REMOTE_HASH_PAYLOAD_KEYS",
    "RESCAN_BATCH_SIZE",
    "RESCAN_PROGRESS_EVERY",
    "SCALED_VERIFY_COOLDOWN_SECONDS",
    "SCAN_CONCURRENCY",
    "TMP_MAX_AGE_SECONDS",
    "_FORCE_TOKENS",
    "_FORCE_TRIM_CHARS",
    "_LAYOUT_VERSION",
    "_NAPCAT_KEY_MAP",
    "_QQ_KEY_MAP",
    "_SCALAR_PATHS",
    "_STREAM_WRITE_BATCH_BYTES",
    "AstrBotConfig",
    "AstrMessageEvent",
    "AsyncGenerator",
    "CheckinMixin",
    "CheckinStore",
    "Comp",
    "CompressionEvidence",
    "Context",
    "DeliveryMixin",
    "DeliveryTarget",
    "GuardedHttpClient",
    "HashIndex",
    "ImageBedClient",
    "ImageFeatures",
    "ImageLibrary",
    "ImageTracePlugin",
    "LibraryMixin",
    "MediaHistory",
    "OriginalMixin",
    "Path",
    "QQUploadMixin",
    "QuoteLibrary",
    "RandomMediaClient",
    "RandomMediaError",
    "ResolveMixin",
    "ResolvedImage",
    "SendOutcome",
    "SimpleNamespace",
    "Star",
    "StarTools",
    "StateMixin",
    "TraceMixin",
    "VectorEngine",
    "VectorEngineError",
    "WeiboPost",
    "_describe_bad_image",
    "aiohttp",
    "annotations",
    "as_float",
    "as_int",
    "asyncio",
    "avif_available",
    "base64",
    "bool_or",
    "bounded_int",
    "build_imgbed_file_url",
    "build_original_url",
    "build_scaled_url",
    "choose_qq_upload_channel",
    "classify_probe_status",
    "classify_send_error",
    "clean_display_name",
    "compose_poster",
    "compression_evidence",
    "compute_features",
    "compute_features_with_mime",
    "content_length_from_headers",
    "contextlib",
    "dataclass",
    "date",
    "datetime",
    "deduplicate_vector_hits",
    "delivery_settings",
    "delivery_target",
    "extract_directory",
    "hashlib",
    "heif_available",
    "image_bytes_ok",
    "image_file_ok",
    "image_mime",
    "install_error_log",
    "io",
    "is_blank",
    "is_degenerate_phash",
    "is_napcat_parseable_url",
    "is_public_http_url",
    "is_qq_image_bed_host",
    "is_scaling_capability_rejection",
    "is_scaling_needed",
    "is_thumbnail_url",
    "load_post_map",
    "load_posts_from_dir",
    "logger",
    "map_is_stale",
    "media_filename",
    "media_kind",
    "os",
    "parse_weibo_text",
    "phash_hex_len",
    "pick_default_directory",
    "posts_from_archive_rows",
    "prefer_inline_bytes",
    "prefer_url_direct",
    "prepare_image_bytes",
    "qq_upload_safe_image",
    "qq_upload_settings",
    "random",
    "read_bytes",
    "read_limited_bytes",
    "redact_url",
    "remove_error_log",
    "sanitize_error_text",
    "secrets",
    "send_diagnosis",
    "sniff_image_format",
    "strip_leading_decoration",
    "swap_url_origin",
    "time",
    "timedelta",
    "timezone",
    "truthy",
    "unquote",
    "upgrade_to_https",
    "urlparse",
    "urlsplit",
]
