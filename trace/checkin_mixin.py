"""每日签到卡片与微博签到池。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import random
import secrets
from urllib.parse import (
    unquote,
    urlparse,
)

from astrbot.api import (
    logger,
)
from astrbot.api.event import (
    AstrMessageEvent,
)

try:
    from ..shared.common import (
        read_bytes,
        sanitize_error_text,
    )
    from ..shared.http import (
        read_limited_bytes,
    )
    from .checkin_card import (
        DEFAULT_QUOTES,
        compose_poster,
    )
    from .image_delivery import (
        build_original_url,
        qq_upload_safe_image,
        upgrade_to_https,
    )
    from .random_media import (
        clean_display_name,
        is_thumbnail_url,
        swap_url_origin,
    )
    from .weibo_posts import (
        WeiboPost,
        load_post_map,
        load_posts_from_dir,
        map_is_stale,
        posts_from_archive_rows,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from checkin_card import (  # type: ignore[no-redef]
        DEFAULT_QUOTES,
        compose_poster,
    )
    from image_delivery import (  # type: ignore[no-redef]
        build_original_url,
        qq_upload_safe_image,
        upgrade_to_https,
    )
    from random_media import (  # type: ignore[no-redef]
        clean_display_name,
        is_thumbnail_url,
        swap_url_origin,
    )
    from weibo_posts import (  # type: ignore[no-redef]
        WeiboPost,
        load_post_map,
        load_posts_from_dir,
        map_is_stale,
        posts_from_archive_rows,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        read_bytes,
        sanitize_error_text,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        read_limited_bytes,
    )

try:
    from .constants import (
        QQ_TRANSCODE_MAX_PIXELS,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        QQ_TRANSCODE_MAX_PIXELS,
    )


class CheckinMixin:
    """每日签到卡片与微博签到池。"""

    # ------------------------------------------------------------------
    # 指令：每日签到（随机缩略图 + 边框语句卡片）
    # ------------------------------------------------------------------

    @staticmethod
    def _write_bytes(path: str, data: bytes) -> None:
        """写临时文件；同步 IO，调用方走 asyncio.to_thread。"""
        with open(path, "wb") as handle:
            handle.write(data)

    async def _checkin_card_path(
        self,
        image_url: str,
        quote: str,
        footer: str,
        settings: dict,
        *,
        quote_max_lines: int = 3,
    ) -> str | None:
        """下载缩略图 → 合成卡片 → 落临时文件，返回路径；任一步失败返回 None。

        合成放进 to_thread：Pillow 解码/绘制/编码是 CPU 活，放事件循环里会
        阻塞其它消息处理；实测 720px 卡片整段约 10ms、1440px 约 25ms。
        """
        try:
            if os.path.isfile(image_url):
                data = await asyncio.to_thread(read_bytes, image_url)
            else:
                data = await self._download_bytes(image_url)
        except Exception as exc:
            logger.warning(f"签到卡片下载图片失败，改用轻量模式: {exc}")
            return None
        image_bytes = await asyncio.to_thread(
            compose_poster,
            data,
            quote=quote,
            footer=footer,
            font_path=settings["font_path"],
            border_px=settings["border_px"],
            quote_max_lines=quote_max_lines,
        )
        if not image_bytes:
            return None
        path = os.path.join(self.tmp_dir, f"c_{secrets.token_hex(8)}.jpg")
        try:
            await asyncio.to_thread(self._write_bytes, path, image_bytes)
        except OSError as exc:
            logger.warning(f"签到卡片写入临时文件失败: {exc}")
            return None
        return path

    # ------------------------------------------------------------------
    # 微博签到：离线映射表（签到只读本地 JSON，不请求微博、不算 pHash）
    # ------------------------------------------------------------------

    def _weibo_map_path(self) -> str:
        return os.path.join(self.weibo_dir, "weibo_post_map.json")

    def _weibo_external_snapshot(self) -> tuple[str, list[dict]]:
        """取外部文案源快照（archive.db → (指纹, 行列表)）。

        端口由入口 main.py 注入；未注入或读失败时返回空，调用方回退 weibo/ 目录。
        结果缓存在实例上：同一轮 _weibo_posts 的签名与加载共用一次读库。
        """
        source = getattr(self, "_weibo_external_source", None)
        if not callable(source):
            return "", []
        try:
            signature, rows = source()
        except Exception as exc:
            logger.warning(f"外部文案源读取失败，回退 weibo/ 目录: {exc}")
            self._weibo_external_cache = ("", [])
            return "", []
        signature = str(signature or "")
        rows = [row for row in (rows or []) if isinstance(row, dict)]
        if not signature or not rows:
            self._weibo_external_cache = ("", [])
            return "", []
        self._weibo_external_cache = (signature, rows)
        return signature, rows

    def _weibo_signature_sync(self) -> tuple:
        """外部文案源优先；没有时用映射表 + 源文件 mtime（任一方变化即重载）。"""
        signature, _rows = self._weibo_external_snapshot()
        if signature:
            return ("archive", signature)
        try:
            map_mtime = os.path.getmtime(self._weibo_map_path())
        except OSError:
            map_mtime = 0.0
        source_mtime = 0.0
        try:
            for name in os.listdir(self.weibo_dir):
                if not name.lower().endswith((".csv", ".md", ".markdown")):
                    continue
                try:
                    source_mtime = max(
                        source_mtime,
                        os.path.getmtime(os.path.join(self.weibo_dir, name)),
                    )
                except OSError:
                    continue
        except OSError:
            pass
        return (round(map_mtime, 3), round(source_mtime, 3))

    def _load_weibo_pool_sync(self) -> tuple[list[WeiboPost], dict]:
        """读映射表；映射缺失/过期时才解析 CSV/MD，避免签到热路径重复解析。

        映射表优先（带图），源文件只用来补新帖子：映射过期时把源文件里有、
        映射里没有的帖子追加进来，这些帖子的 image 为 None，签到走回退。
        """
        signature, rows = self._weibo_external_cache
        if not signature:
            signature, rows = self._weibo_external_snapshot()
        if signature and rows:
            posts = posts_from_archive_rows(rows)
            meta = {
                "count": len(posts),
                "matched_count": sum(1 for post in posts if post.image is not None),
                "map_count": len(posts),
                "map_generated_at": "",
                "map_stale": False,
                "source_files": [],
                "errors": [],
                "map_meta": {"source": "archive", "signature": signature},
            }
            return posts, meta
        map_path = self._weibo_map_path()
        posts, generated_at, map_meta = load_post_map(map_path)
        stale = map_is_stale(map_path, self.weibo_dir)
        source_posts: list[WeiboPost] = []
        source_files: list[str] = []
        errors: list[str] = []
        if not posts or stale:
            source_posts, source_files, errors = load_posts_from_dir(self.weibo_dir)
        merged = posts
        if not posts:
            merged = source_posts
        elif stale and source_posts:
            by_key = {(post.account.casefold(), post.published_at): post for post in posts}
            merged = [
                by_key.get((post.account.casefold(), post.published_at), post)
                for post in source_posts
            ]
            # 源文件被删掉时仍保留映射里的旧条目，避免已有配图丢失
            source_keys = {(post.account.casefold(), post.published_at) for post in source_posts}
            merged.extend(
                post
                for post in posts
                if (post.account.casefold(), post.published_at) not in source_keys
            )
        meta = {
            "count": len(merged),
            "matched_count": sum(1 for post in merged if post.image is not None),
            "map_count": len(posts),
            "map_generated_at": generated_at,
            "map_stale": stale,
            "source_files": [os.path.basename(path) for path in source_files],
            "errors": errors,
            "map_meta": map_meta,
        }
        return merged, meta

    async def _weibo_posts(self) -> tuple[list[WeiboPost], dict]:
        """签到热路径：mtime 未变时直接返回内存池，不解析任何文件。"""
        signature = await asyncio.to_thread(self._weibo_signature_sync)
        if self._weibo_posts_cache is not None and signature == self._weibo_signature:
            return self._weibo_posts_cache, self._weibo_meta_cache
        posts, meta = await asyncio.to_thread(self._load_weibo_pool_sync)
        self._weibo_posts_cache = posts
        self._weibo_meta_cache = meta
        self._weibo_signature = signature
        if posts:
            self._weibo_empty_warned = False
        elif not self._weibo_empty_warned:
            logger.warning(
                "微博签到模式已开启，但 %s 下没有文案文件或映射表；/签到 将回退随机语句。",
                self.weibo_dir,
            )
            self._weibo_empty_warned = True
        if meta.get("errors"):
            logger.warning("微博文案解析失败: %s", "；".join(meta["errors"]))
        return posts, meta

    async def _weibo_status_line(self) -> str:
        settings = self._checkin_settings()
        if settings["mode"] != "weibo":
            return "· 签到模式：quote（随机图 + 随机语句）"
        posts, meta = await self._weibo_posts()
        if not posts:
            return "· 签到模式：weibo（未找到文案/映射，/签到 将回退随机语句）"
        matched = int(meta.get("matched_count") or 0)
        from_archive = (meta.get("map_meta") or {}).get("source") == "archive"
        suffix = "，来源 archive.db" if from_archive else ""
        if meta.get("map_stale"):
            suffix += "，源文件比映射新"
        if not from_archive and not meta.get("map_count"):
            suffix += "，映射表未生成"
        return f"· 签到模式：weibo（文案 {len(posts)} 条，可配对 {matched} 条{suffix}）"

    def _weibo_thumb_path(self, file_name: str) -> str:
        """本地预缓存缩略图路径；文件名用 sha1，避免中文/特殊字符进路径。"""
        key = hashlib.sha1(str(file_name or "").encode("utf-8")).hexdigest()
        path = os.path.join(self.weibo_thumb_dir, f"{key}.jpg")
        return path if os.path.isfile(path) else ""

    async def _weibo_candidate(self, random_settings: dict):
        """取一条微博及其图库图片；返回 (post, 发送直链, 原图直链, 文件名, 本地缩略图)。

        映射表里有图就直接用；没有图时退一步取同账号同日的任意已匹配图片，
        仍没有则返回 None，由调用方回退现有「随机图 + 随机语句」。

        ``本地缩略图`` 由映射脚本的 ``--prefetch-thumbs`` 预生成；card 模式优先
        读本地文件（零网络），simple 模式仍发远程 thumb URL。
        """
        posts, _meta = await self._weibo_posts()
        if not posts:
            return None
        post = random.choice(posts)
        image = post.image
        if image is None:
            pool = [
                item.image
                for item in posts
                if item.image is not None
                and item.account.casefold() == post.account.casefold()
                and item.date == post.date
            ]
            if pool:
                image = random.choice(pool)
        if image is None:
            return None
        local_thumb = self._weibo_thumb_path(image.file_name)
        original_url = image.image_url or image.thumb_url
        if original_url:
            original_url = build_original_url(unquote(original_url))
        send_url = image.thumb_url or original_url
        lan_hosts = self._lan_url_hosts(random_settings)
        lan_base = str((random_settings or {}).get("lan_base_url") or "")
        if lan_hosts:
            send_url = swap_url_origin(send_url, lan_base, lan_hosts)
            original_url = swap_url_origin(original_url, lan_base, lan_hosts)
        base_url = str((random_settings or {}).get("base_url") or "")
        send_url = upgrade_to_https(send_url, base_url)
        original_url = upgrade_to_https(original_url, base_url)
        if not send_url and not local_thumb:
            return None
        return post, send_url, original_url, image.file_name, local_thumb

    async def _deliver_checkin(
        self,
        event: AstrMessageEvent,
        header: str,
        quote: str,
        footer: str,
        send_url: str,
        original_url: str,
        block_name: str,
        settings: dict,
        *,
        quote_max_lines: int = 3,
        simple_text: str = "",
        card_source: str = "",
    ):
        """签到统一交付：card 合成失败自动退回 simple；两类签到共用。

        card_source 供微博模式传本地预缓存缩略图路径；为空时走
        ``_checkin_card_source`` 反查缩略图替身（查不到再退回发送图）。
        """
        remember = [{"url": original_url, "file_name": block_name}] if original_url else []
        if settings["render_mode"] == "card":
            source = card_source or await self._checkin_card_source(send_url, original_url)
            path = await self._checkin_card_path(
                source,
                quote,
                footer,
                settings,
                quote_max_lines=quote_max_lines,
            )
            if path:
                try:
                    async for result in self._yield_delivery(
                        event,
                        header,
                        [{"path": path, "file_name": block_name}],
                        # 卡片已是成品 JPEG：交给适配器原样发送，避免二次压缩
                        mode_override="native",
                        allow_local_fallback=False,
                        remember_blocks=remember,
                    ):
                        yield result
                finally:
                    self._remove_quiet(path)
                return
            logger.warning("签到卡片合成失败，改用轻量模式")
        text = simple_text or f"{header}\n💬 {quote}"
        async for result in self._yield_delivery(
            event,
            text,
            [{"url": send_url, "file_name": block_name}],
            remember_blocks=remember,
        ):
            yield result

    async def _checkin_card_source(self, send_url: str, original_url: str) -> str:
        """卡片合成专用图源：发送态是原图时先反查缩略图替身。

        卡片成品最长边只有 POSTER_MAX_SIDE，用原图合成没有收益——2026-11-02
        线上实录：一张 8736×11648 的手机长图被拿来合成，既白花几十 MB 流量与
        上亿像素解码，又被 compose_poster 的防炸上限挡回去，整张卡片降级成
        没有边框的轻量模式。

        这里只保证「合成用图」与「发送态」解耦：不论 ``random_media.send_thumbnail``
        开关，合成都优先用缩略图替身，查不到再退回原图。反查复用
        ``_random_send_url`` 的 thumbnail=True 口径，但**不改**该开关的默认语义，
        /随机图 的发图选择不受影响。
        """
        source = original_url or send_url
        if is_thumbnail_url(send_url):
            # 上游已经给的就是缩略图直链（图床随机目录指到了 thumbnails/）
            return send_url
        thumb = await self._random_send_url(source, self._random_settings(), thumbnail=True)
        if thumb and thumb != source:
            logger.debug(f"签到卡片改用缩略图合成: {source} -> {thumb}")
        return thumb or source

    async def _checkin_flow(self, event: AstrMessageEvent):
        """签到主流程：判定 → 取图 → 取句 → 卡片/轻量交付。

        签到记录先落库、再取图：取图失败不回收当天资格，避免接口抖动时用户
        反复重试刷图；提示文案会明确说明「已签到但取图失败」。同一天重复签到
        不再累加天数，但仍重新取图取句，让每次都有新内容。
        """
        settings = self._checkin_settings()
        if not settings["enabled"]:
            yield event.plain_result("每日签到功能已在插件配置中关闭。")
            return
        if self.checkin_store is None:
            yield event.plain_result("签到存储不可用（数据库初始化失败），详情请查看机器人日志。")
            return
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("无法识别发送者，签到失败。")
            return

        today = self._checkin_today(settings["utc_offset_hours"])
        try:
            outcome = await asyncio.to_thread(self.checkin_store.check_in, user_id, today)
        except Exception as exc:
            logger.error(f"签到记录写入失败: {exc}")
            yield event.plain_result("签到记录写入失败，请稍后再试。")
            return
        if outcome.already:
            header = f"📅 今日已签到 · 连续 {outcome.streak} 天（累计 {outcome.total} 天）"
        else:
            header = f"✅ 签到成功 · 连续 {outcome.streak} 天（累计 {outcome.total} 天）"

        random_settings = self._random_settings()

        # 微博模式：映射表里已有「文案 → 图库图片」，签到只读本地 JSON + 内网缩略图。
        # 任何一步失败都不把异常抛回事件管线，直接继续走下面的随机语句回退。
        if settings["mode"] == "weibo":
            try:
                candidate = await self._weibo_candidate(random_settings)
            except Exception as exc:
                logger.warning(f"微博签到取图失败，回退随机语句: {exc}")
                candidate = None
            if candidate is not None:
                post, send_url, original_url, image_name, local_thumb = candidate
                quote = post.text
                footer = (
                    f"{post.account_display} · {post.date_display}\n"
                    f"连续签到 {outcome.streak} 天 · 累计 {outcome.total} 天"
                )
                block_name = (
                    clean_display_name(image_name)
                    or image_name
                    or clean_display_name(original_url)
                    or "今日签到"
                )
                simple_text = (
                    f"{header}\n📝 {post.account_display} · {post.date_display}\n💬 {quote}"
                )
                async for result in self._deliver_checkin(
                    event,
                    header,
                    quote,
                    footer,
                    send_url,
                    original_url,
                    block_name,
                    settings,
                    quote_max_lines=5,
                    simple_text=simple_text,
                    card_source=local_thumb,
                ):
                    yield result
                return
            logger.debug("微博文案暂无可配图片，回退随机图 + 随机语句")

        try:
            send_url, original_url, original_name = await self._random_image_candidate(
                "", random_settings
            )
        except Exception as exc:
            # RandomMediaError 之外还可能有网络/解析等未预期异常：签到不能因此
            # 把异常抛回 AstrBot 事件管线，统一降级为可读提示（URL/内网地址脱敏）
            logger.warning(f"签到取图失败: {exc}")
            safe_exc = sanitize_error_text(exc)
            if outcome.already:
                yield event.plain_result(
                    f"📅 今天已经签到过（连续 {outcome.streak} 天），但取图失败：{safe_exc}"
                )
            else:
                yield event.plain_result(
                    f"✅ 签到成功（连续 {outcome.streak} 天），但取图失败：{safe_exc}"
                )
            return

        try:
            quote = await self._quote_library().pick()
        except Exception as exc:
            # 语句文件损坏/非 UTF-8/接口构造失败都不该让签到整体失败
            logger.warning(f"签到语句获取失败，使用内置语句: {exc}")
            quote = random.choice(DEFAULT_QUOTES)
        footer = f"连续签到 {outcome.streak} 天 · 累计 {outcome.total} 天"
        block_name = original_name or clean_display_name(original_url) or "今日签到"
        async for result in self._deliver_checkin(
            event,
            header,
            quote,
            footer,
            send_url,
            original_url,
            block_name,
            settings,
            quote_max_lines=3,
        ):
            yield result

    @staticmethod
    def _bed_file_id(url: str) -> str:
        """从图床直链里取出文件 id（即 S3 对象键）。

        图床直链形如 `{base}/file/{id}`，id 可能含中文与 `@`/`#`（故 URL 里是
        百分号编码的），必须 unquote 还原成原始键才能算出同一个点 id。取不到
        返回空串，由调用方按「无替身」处理。
        """
        path = urlparse(str(url or "")).path
        marker = "/file/"
        index = path.find(marker)
        if index < 0:
            return ""
        return unquote(path[index + len(marker) :]).strip()

    async def _offbox_pipeline(
        self,
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
        blocks = await self._offbox_rewrite_blocks(blocks)
        handled = await self._qq_upload_takeover(
            event, header, blocks, mark_compressed=mark_compressed
        )
        return blocks, handled

    async def _offbox_rewrite_blocks(self, blocks: list[dict]) -> list[dict]:
        """对 blocks 里每个待发 URL 做官方富媒体格式体检，按需改写发送形态。

        体检点放在 _yield_delivery（三条回传路径的汇合处）：标准格式原样
        保留（只花一次 16 字节嗅探）；假格式可解码 → 换成本地 JPEG path；
        不可解码 → 换成缩略图替身 URL。全部体检失败按原样保留——体检是
        增强，不能反过来挡住发送。
        """
        settings = self._random_settings()
        rewritten: list[dict] = []
        for block in blocks:
            url = str(block.get("url") or "")
            if not url or block.get("path"):
                rewritten.append(block)
                continue
            try:
                outcome = await self._offbox_image_delivery(url, settings)
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

    async def _offbox_image_delivery(self, url: str, settings: dict) -> tuple[str, str] | None:
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
            client = await self._get_http()
            limit = self._download_limit_bytes()
            async with await client.request("GET", url, timeout=self._download_timeout()) as resp:
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
        path = await asyncio.to_thread(self._write_transcoded_jpeg, data)
        if path:
            return ("path", path)
        file_id = self._bed_file_id(url)
        if file_id and self.vector.qdrant_ready:
            try:
                thumb = await self.vector.thumb_url_for(file_id)
            except Exception:
                thumb = None
            if thumb:
                lan_hosts = self._lan_url_hosts(settings)
                if lan_hosts:
                    # 图片由适配器本机下载，替身直链同样收进内网源
                    thumb = swap_url_origin(
                        thumb, str(settings.get("lan_base_url") or ""), lan_hosts
                    )
                logger.info(f"qq_official 假格式且不可转码，改发缩略图替身: {file_id}")
                return ("url", thumb)
        return None

    def _write_transcoded_jpeg(self, data: bytes) -> str | None:
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
                path = os.path.join(self.tmp_dir, f"qqsafe_{secrets.token_hex(8)}.jpg")
                fixed.save(path, format="JPEG", quality=92)
            logger.info(f"qq_official 图片格式体检：已转码为 JPEG 交付 {path}")
            return path
        except Exception as exc:
            if path:
                self._remove_quiet(path)  # 半截临时文件不能留给 24h TTL 兜底
            logger.warning(f"图片转码失败，按原样交付: {exc}")
            return None
