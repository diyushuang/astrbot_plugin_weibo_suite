"""qq_official 图片格式体检：假格式改写为本地 JPEG 或缩略图替身。

第三方转存平台（QQ 官方机器人）只收标准 JPEG/PNG/GIF/WEBP/BMP；图床里存在
扩展名 .jpg、实为 HEIF/AVIF/WEBP 的文件，原样交付必被打回「富媒体文件格式
不支持」。本模块只负责「检查并改写 blocks」，不做通道选择，也通过 host 上的
同名薄委托调用插件能力，避免反向 import main。
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from ..shared.http import read_limited_bytes
    from .constants import QQ_TRANSCODE_MAX_PIXELS
    from .image_delivery import qq_upload_safe_image
    from .random_media import swap_url_origin
except ImportError:  # 兼容插件以独立模块方式加载
    from image_delivery import qq_upload_safe_image  # type: ignore[no-redef]
    from random_media import swap_url_origin  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        read_limited_bytes,
    )
    from constants import QQ_TRANSCODE_MAX_PIXELS  # type: ignore[no-redef]


async def offbox_pipeline(
    host,
    event: AstrMessageEvent,
    header: str,
    blocks: list[dict],
    *,
    mark_compressed: bool = False,
) -> tuple[list[dict], bool]:
    """第三方转存平台的两个前置动作合一：格式体检 → 上传通道接管。

    顺序不可交换：体检可能把「假 jpg」换成本地转码 JPEG（path）或缩略图
    替身 URL，上传通道必须拿着**改写后**的 block 才能决定发 URL 还是发
    分片（path 只能分片）。合并前这是 _yield_delivery 里两段独立 hook，
    很容易出现「改写了 blocks 却忘了把它交给下一步」这类顺序错误。

    返回 (最终 blocks, 是否已投递完毕)：handled 为真时调用方只需标记已
    发送并 stop_event，不要再走任何原生回传分支（否则会重复投递）。
    """
    blocks = await host._offbox_rewrite_blocks(blocks)
    handled = await host._qq_upload_takeover(event, header, blocks, mark_compressed=mark_compressed)
    return blocks, handled


async def rewrite_blocks(host, blocks: list[dict]) -> list[dict]:
    """对 blocks 里每个待发 URL 做官方富媒体格式体检，按需改写发送形态。

    体检点放在 _yield_delivery（三条回传路径的汇合处）：标准格式原样
    保留（只花一次 16 字节嗅探）；假格式可解码 → 换成本地 JPEG path；
    不可解码 → 换成缩略图替身 URL。全部体检失败按原样保留——体检是
    增强，不能反过来挡住发送。
    """
    settings = host._random_settings()
    rewritten: list[dict] = []
    for block in blocks:
        url = str(block.get("url") or "")
        if not url or block.get("path"):
            rewritten.append(block)
            continue
        try:
            outcome = await host._offbox_image_delivery(url, settings)
        except Exception as exc:
            logger.warning(f"qq_official 图片格式体检异常，按原样交付: {exc}")
            outcome = None
        if outcome is None:
            rewritten.append(block)
            continue
        kind, value = outcome
        new_block = dict(block)
        if kind == "path":
            new_block.pop("url", None)
            new_block["path"] = value
            # 转码产物属于本次投递：由 _yield_delivery 在发送完成后清理
            new_block["_tmp_owned"] = True
        else:
            new_block["url"] = value
        rewritten.append(new_block)
    return rewritten


async def inspect_image_delivery(host, url: str, settings: dict) -> tuple[str, str] | None:
    """qq_official 图片交付体检：返回 (方式, 值)；None 表示按原 URL 交付。

    官方富媒体上传只收标准 JPEG/PNG/GIF；图床里存在扩展名 .jpg、实为
    HEIF/AVIF/WEBP（微博 live 图等）的文件，原样交付必被打回「富媒体文件
    格式不支持」。处置分两级：
    - "path"：PIL 可解码的假格式 → 转码为 JPEG 落盘，本地字节交付；
    - "url"：PIL 解不了的（如未装 pillow-heif 的 HEIC）→ 换发同一点的
      缩略图替身直链（img-indexer 预生成的真 JPEG）。
    文件头能放行、或两级兜底都失败时返回 None（按原样交付——体检是增强，
    不能反过来挡住发送）。
    """
    try:
        client = await host._get_http()
        limit = host._download_limit_bytes()
        async with await client.request("GET", url, timeout=host._download_timeout()) as resp:
            if not 200 <= resp.status < 300:
                return None
            head = await resp.content.read(16)
            if qq_upload_safe_image(head):
                return None
            # 嗅探已消费前 16 字节：转码必须用完整字节，把 head 拼回去，
            # 否则 PIL 拿到的是缺了文件头的残缺流，解码必败。
            # 上限按完整响应体算：read_limited_bytes 会拿 Content-Length 与
            # limit 比较，若传 limit-len(head) 会把恰好等于上限的文件误拒。
            rest = await read_limited_bytes(resp, limit)
            data = head + rest
            if len(data) > limit:
                logger.warning(f"qq_official 图片超过体检上限，按原样交付: {len(data)} 字节")
                return None
    except Exception as exc:
        logger.warning(f"qq_official 图片格式体检失败，按原样交付: {exc}")
        return None
    path = await asyncio.to_thread(host._write_transcoded_jpeg, data)
    if path:
        return ("path", path)
    file_id = host._bed_file_id(url)
    if file_id and host.vector.qdrant_ready:
        try:
            thumb = await host.vector.thumb_url_for(file_id)
        except Exception:
            thumb = None
        if thumb:
            lan_hosts = host._lan_url_hosts(settings)
            if lan_hosts:
                # 图片由适配器本机下载，替身直链同样收进内网源
                thumb = swap_url_origin(thumb, str(settings.get("lan_base_url") or ""), lan_hosts)
            logger.info(f"qq_official 假格式且不可转码，改发缩略图替身: {file_id}")
            return ("url", thumb)
    return None


def write_transcoded_jpeg(host, data: bytes) -> str | None:
    """把任意可解码图片字节转存为 JPEG 临时文件，返回路径；失败返回 None。"""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()  # 幂等：让 Image.open 直解 HEIC
    except ImportError:
        pass
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("PIL 不可用，无法转码非标准格式图片，按原样交付")
        return None
    path = ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > QQ_TRANSCODE_MAX_PIXELS:
                logger.warning(f"qq_official 待转码图片像素过大，跳过转码: {image.size}")
                return None
            fixed = ImageOps.exif_transpose(image)
            if fixed.mode not in ("RGB", "L"):
                fixed = fixed.convert("RGB")
            path = os.path.join(host.tmp_dir, f"qqsafe_{secrets.token_hex(8)}.jpg")
            fixed.save(path, format="JPEG", quality=92)
        logger.info(f"qq_official 图片格式体检：已转码为 JPEG 交付 {path}")
        return path
    except Exception as exc:
        if path:
            host._remove_quiet(path)  # 半截临时文件不能留给 24h TTL 兜底
        logger.warning(f"图片转码失败，按原样交付: {exc}")
        return None
