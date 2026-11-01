"""QQ 官方机器人富媒体上传与随机媒体发送通道。"""

from __future__ import annotations

import os
from pathlib import (
    Path,
)
from types import (
    SimpleNamespace,
)
from urllib.parse import (
    urlsplit,
)

from astrbot.api import (
    logger,
)
from astrbot.api.event import (
    AstrMessageEvent,
)

try:
    from ..shared.common import (
        sanitize_error_text,
    )
    from .image_delivery import (
        CompressionEvidence,
        build_scaled_url,
        choose_qq_upload_channel,
        is_public_http_url,
        qq_upload_settings,
    )
    from .random_media import (
        clean_display_name,
        extract_directory,
        swap_url_origin,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from image_delivery import (  # type: ignore[no-redef]
        CompressionEvidence,
        build_scaled_url,
        choose_qq_upload_channel,
        is_public_http_url,
        qq_upload_settings,
    )
    from random_media import (  # type: ignore[no-redef]
        clean_display_name,
        extract_directory,
        swap_url_origin,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        sanitize_error_text,
    )

try:
    from .constants import (
        QQ_IMAGE_FILE_TYPE,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        QQ_IMAGE_FILE_TYPE,
    )


class QQUploadMixin:
    """QQ 官方机器人富媒体上传与随机媒体发送通道。"""

    # ------------------------------------------------------------------
    # QQ 官方机器人上传通道（url / chunked，与缩略图发送无关）
    # ------------------------------------------------------------------

    def _qq_upload_config(self) -> tuple[str, int, int, str, int, str]:
        """推送通道（QQ 官方）配置归一化，语义见 image_delivery.qq_upload_settings。"""
        return qq_upload_settings(self._qq_delivery_cfg())

    @staticmethod
    def _qq_send_target(event: AstrMessageEvent) -> tuple[str, str] | None:
        """取 (会话类型, 目标 openid)：group→group_openid、c2c→user_openid。

        botpy 的群消息自带 group_openid、C2C 消息在 author.user_openid 上。
        都拿不到时退化为「session_id + 消息类型」；连类型都判不出就返回 None，
        让调用方交回原回传逻辑——猜错会话类型会把图发到错误的聊天里，比不发
        严重得多，所以这里宁可放弃接管。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        group_openid = str(getattr(raw, "group_openid", "") or "").strip()
        if group_openid:
            return ("group", group_openid)
        author = getattr(raw, "author", None)
        user_openid = str(getattr(author, "user_openid", "") or "").strip()
        if not user_openid:
            user_openid = str(getattr(raw, "user_openid", "") or "").strip()
        if user_openid:
            return ("c2c", user_openid)
        try:
            session_id = str(event.get_session_id() or "").strip()
        except Exception:
            session_id = ""
        if not session_id:
            return None
        try:
            message_type = str(event.get_message_type() or "").upper()
        except Exception:
            message_type = ""
        if "GROUP" in message_type:
            return ("group", session_id)
        if any(hint in message_type for hint in ("FRIEND", "C2C", "PRIVATE")):
            return ("c2c", session_id)
        return None

    @staticmethod
    def _qq_target_kwargs(target: tuple[str, str]) -> dict:
        """把 (类型, openid) 转成适配器上传方法的收件人关键字参数。"""
        kind, session_id = target
        return {"group_openid": session_id} if kind == "group" else {"openid": session_id}

    def _public_media_url(self, url: str, settings: dict) -> str | None:
        """把图床直链换成「外部服务器下载得到」的公网地址；换不出来返回 None。

        两处共用：qq_official 的视频要交给腾讯拉流、图片要交给腾讯按 URL 转存。
        内网直连（lan_base_url）只是图床与 AstrBot 之间的便利通道，对外正源是
        base_url，故命中内网主机时按 base_url 换源（只换协议+主机端口，路径与
        查询串原样保留）。

        判定不能只看 IP 段：曾遇到把云厂商分配的公网段 IP 当作内网直连地址的
        部署，句法检查会把它当公网地址放行。因此以配置为准：

        1. 命中内网直连主机（lan_base_url）→ 按 base_url 换回公网源；
        2. 换完仍落在内网主机上（说明 base_url 本身就配成了内网）→ 不可用；
        3. 与 base_url 同主机 → 视为公网直链（配置已声明它是公网站点）；
        4. 其余第三方主机 → 退化为句法判断（公网域名/公网 IP 放行）。
        """
        if not url:
            return None
        base = str(settings.get("base_url") or "").strip()
        lan_hosts = self._lan_url_hosts(settings)
        candidate = swap_url_origin(url, base, lan_hosts) if lan_hosts else url
        try:
            host = (urlsplit(candidate).hostname or "").lower()
        except ValueError:
            return None
        # 只与「内网地址的主机」比，不能与整个 _lan_url_hosts 比——后者里头还
        # 包含公网 base 的主机（换源后正是要落到它上面）
        lan_host = ""
        lan_base = str(settings.get("lan_base_url") or "").strip()
        if lan_base:
            try:
                lan_host = (urlsplit(lan_base).hostname or "").lower()
            except ValueError:
                lan_host = ""
        if lan_host and host == lan_host:
            # 换源后仍是内网主机：腾讯服务器访问不到，别发出一次必败的上传
            return None
        base_host = ""
        if base:
            try:
                base_host = (urlsplit(base).hostname or "").lower()
            except ValueError:
                base_host = ""
        if base_host and host == base_host:
            return candidate
        return candidate if is_public_http_url(candidate) else None

    async def _qq_block_size(self, block: dict) -> int | None:
        """该图体积：优先用命中条目自带的大小，未知时 HEAD 探一次。

        探不出来返回 None（=未知），调用方据此避开「未知却当成小图直传」这个
        5MB 陷阱；探测只读响应头，不产生下载流量。
        """
        known = self._known_bytes(block)
        if known:
            return known
        url = str(block.get("url") or "")
        if not url:
            return None
        exists, length = await self._probe_url(url)
        return length if exists is not False else None

    async def _qq_upload_takeover(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mark_compressed: bool = False,
    ) -> bool:
        """qqofficial 上传通道接管：返回 True 表示本批已由本方法处理完毕。

        只在通道不是 direct 时才真正接管；一旦接管，图片由插件自己上传并发送
        （post_group_message / post_c2c_message + msg_type=7），所以成功返回后
        调用方必须标记已发送并 stop_event，否则 respond stage 会照原路（base64
        直传）再发一次，那正是「上传文件大小超过限制」的来源。

        固定 url / url-scaled 通道是例外：某张图没有可交给腾讯的公网直链
        （只剩本地转码 path，或图床只有内网源）时，该图降级为分片上传——
        URL 上传没有入参，照原通道发出去必然失败，白记一轮 WARN。

        失败语义两档（delivery.qq_on_failure）：
        - fallback（默认）：一张都还没发出去时返回 False，交回原有的本地压缩 /
          缩略图降级链；只要已经发过就绝不再交回，避免同一张图发两遍。
        - notify：不交回，改回一条文字说明失败原因。
        """
        mode, direct_max, url_max, auto_prefer, max_upload, on_failure = self._qq_upload_config()
        if mode == "native":
            # 通道关闭：交给 image_delivery 的原生回传逻辑（含压缩）
            return False
        target = self._qq_send_target(event)
        bot = getattr(event, "bot", None)
        if target is None or getattr(bot, "api", None) is None:
            logger.debug("qq_official 上传通道：会话标识或 bot 未就绪，交回原回传逻辑")
            return False
        images = [block for block in blocks if block.get("url") or block.get("path")]
        if not images:
            return False

        settings = self._random_settings()
        plan: list[tuple[dict, str, str | None]] = []
        degraded = 0
        for block in images:
            # 固定通道（native/bytes/url/url-scaled）不依赖体积，省掉一次 HEAD 探测
            size = None if mode != "auto" else await self._qq_block_size(block)
            url = str(block.get("url") or "")
            public_url = self._public_media_url(url, settings) if url else None
            url_usable = public_url is not None
            if mode == "auto" and url_usable and self._qq_url_cooling():
                # 刚撞过「腾讯拉不到公网直链」：冷却期内 auto 直接分片，别再白等
                url_usable = False
            channel = choose_qq_upload_channel(
                mode=mode,
                size_bytes=size,
                direct_max_bytes=direct_max,
                url_max_bytes=url_max,
                url_usable=url_usable,
                auto_prefer=auto_prefer,
            )
            if channel == "native":
                # 混批里只要有一张判定该直传，就整批交回原逻辑：拆成两次投递会
                # 绕开「一次事件只投递一批图」的去重盾，用户可能收到重复图片
                logger.debug("qq_official 上传通道：该批含直传图，交回原回传逻辑")
                return False
            if channel in {"url", "url-scaled"} and public_url is None:
                # 钉死直链通道，但这张图没有可交给腾讯的公网直链：只剩本地
                # 转码 path（格式体检把假格式换成 JPEG 时会丢掉 url），或图床
                # 只有内网源（base_url 未配/本身是内网）。URL 上传没有入参，
                # 发出去必然失败——降级到分片，图源本机就有，不依赖图床公网可达
                degraded += 1
                channel = "bytes"
            plan.append((block, channel, public_url))

        if degraded:
            logger.info(
                f"qq_official 直链通道没有可用的公网直链（本地转码图或内网图床），"
                f"本批 {degraded} 张改走分片上传"
            )

        if mark_compressed:
            # 本通道发的是原图（URL 原样 / 分片原始字节），配文按「原图」口径写，
            # 不能沿用压缩证据——文字说已压缩而用户收到原图是这个仓库的老毛病
            self._mark_compressed_captions(blocks, [CompressionEvidence.ORIGINAL] * len(images))

        prefix = [header] if header else []
        sent_any = False
        for index, (block, channel, public_url) in enumerate(plan):
            own_text = str(block.get("text") or "").strip()
            parts = (*prefix, own_text) if index == 0 else (own_text,)
            content = "\n".join(part for part in parts if part)
            try:
                media = await self._qq_upload_block(
                    event, block, channel, public_url, target, max_upload
                )
                await self._qq_post_media(event, target, content, media)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"qq_official {channel} 通道上传/发送失败"
                    f"（第 {index + 1}/{len(plan)} 张）: {detail}"
                )
                if channel in {"url", "url-scaled"}:
                    # 公网直链拿得到、腾讯却拉不到：记冷却，auto 后续直接走 bytes
                    self._note_qq_url_unsupported(exc)
                if not sent_any and on_failure == "fallback":
                    logger.warning("qq_official 上传通道不可用，交回原回传逻辑（本地压缩/缩略图）")
                    return False
                await self._qq_notify(
                    event, target, f"⚠️ 图片发送失败：{sanitize_error_text(detail)}"
                )
                return True
            sent_any = True
        return True

    async def _qq_upload_block(
        self,
        event: AstrMessageEvent,
        block: dict,
        channel: str,
        public_url: str | None,
        target: tuple[str, str],
        max_upload: int,
    ):
        """按通道上传一张图，返回 QQ 侧 Media。

        url / url-scaled 共用同一条 URL 上传实现，区别只在交给腾讯的是原图
        直链还是图床缩放直链；bytes 即分片上传（官方术语 chunked upload），
        需要先把图落到本地。
        """
        if channel in {"url", "url-scaled"}:
            if not public_url:
                raise RuntimeError("没有公网直链，QQ 无法按 URL 拉取该图")
            upload_url = public_url
            if channel == "url-scaled":
                _mode, max_side, quality, style = self._delivery_config()
                upload_url = (
                    build_scaled_url(public_url, max_side, quality=quality, style=style)
                    or public_url
                )
            return await self._qq_upload_via_url(event, upload_url, target)
        path = str(block.get("path") or "")
        temp = ""
        try:
            if not path:
                temp = await self._download(str(block.get("url") or ""), size_limit=max_upload)
                path = temp
            name = (
                str(block.get("file_name") or "").strip() or os.path.basename(path) or "image.jpg"
            )
            return await self._qq_upload_via_chunked(event, path, name, target)
        finally:
            # 分片通道只为这一次上传落盘，发完即删；临时目录另有 TTL 清理兜底
            self._remove_quiet(temp)

    async def _qq_upload_via_url(self, event: AstrMessageEvent, url: str, target: tuple[str, str]):
        """URL 上传：只传 `url` 字段，由腾讯侧下载转存（图片软限 20MB）。

        借适配器自身的通用媒体上传方法（它对「非本地路径」正是走 url 分支），
        避免在插件里复刻 QQ 接口字段与错误码；AstrBot 发主动消息时也是用同样的
        伪事件（只带 bot 属性）调用它。
        """
        try:
            from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
                QQOfficialMessageEvent,
            )
        except ImportError as exc:  # 版本差异：模块或方法被改名
            raise RuntimeError(f"当前 AstrBot 版本缺少 qqofficial 媒体上传方法: {exc}") from exc
        helper = SimpleNamespace(bot=getattr(event, "bot", None))
        return await QQOfficialMessageEvent.upload_group_and_c2c_media(
            helper,
            url,
            QQ_IMAGE_FILE_TYPE,
            srv_send_msg=False,
            **self._qq_target_kwargs(target),
        )

    async def _qq_upload_via_chunked(
        self, event: AstrMessageEvent, path: str, name: str, target: tuple[str, str]
    ):
        """分片上传：prepare → 并发 PUT 分片 → 逐片确认 → 合并（硬限 200MB）。

        直接实例化适配器的分片器，而不是走 upload_group_and_c2c_media：后者只在
        「本地文件 > 10MB」时才分片，5~10MB 仍会落回 base64 分支（照样撞 5MB）。
        """
        try:
            from astrbot.core.platform.sources.qqofficial.qqofficial_chunked_upload import (
                QQOfficialChunkedUploader,
            )
        except ImportError as exc:
            raise RuntimeError(f"当前 AstrBot 版本缺少 qqofficial 分片上传器: {exc}") from exc
        uploader = QQOfficialChunkedUploader(event.bot.api._http)
        kind, session_id = target
        if kind == "group":
            return await uploader.upload_group(
                file_path=Path(path),
                file_type=QQ_IMAGE_FILE_TYPE,
                file_name=name,
                group_openid=session_id,
                srv_send_msg=False,
            )
        return await uploader.upload_c2c(
            file_path=Path(path),
            file_type=QQ_IMAGE_FILE_TYPE,
            file_name=name,
            user_openid=session_id,
            srv_send_msg=False,
        )

    def _next_qq_seq(self) -> int:
        """QQ 被动回复的 msg_seq：同一 msg_id 内自增，避免随机撞号被判重复。"""
        self._qq_msg_seq = self._qq_msg_seq % 60000 + 1
        return self._qq_msg_seq

    async def _qq_post_media(
        self, event: AstrMessageEvent, target: tuple[str, str], content: str, media
    ) -> None:
        """自己发一条 media 消息（msg_type=7）。

        不走 event.send：那条路会把图片重新解析成 base64 交给 file_data 直传，
        正是本次要绕开的 5MB 限制。msg_seq 必须自增——被动回复中 (msg_id,
        msg_seq) 组合重复会被平台判失败。
        """
        api = event.bot.api
        message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        payload = {
            "msg_type": 7,
            "media": media,
            "content": content or None,
            "msg_id": message_id or None,
            "msg_seq": self._next_qq_seq(),
        }
        kind, session_id = target
        if kind == "group":
            await api.post_group_message(group_openid=session_id, **payload)
        else:
            await api.post_c2c_message(openid=session_id, **payload)

    async def _qq_notify(self, event: AstrMessageEvent, target: tuple[str, str], text: str) -> None:
        """失败说明用纯文本回一条；这条发不出去也不影响主流程。"""
        try:
            api = event.bot.api
            message_id = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            ).strip()
            payload = {
                "msg_type": 0,
                "content": text,
                "msg_id": message_id or None,
                "msg_seq": self._next_qq_seq(),
            }
            kind, session_id = target
            if kind == "group":
                await api.post_group_message(group_openid=session_id, **payload)
            else:
                await api.post_c2c_message(openid=session_id, **payload)
        except Exception as exc:
            logger.debug(f"qq_official 失败说明发送失败（忽略）: {exc}")

    async def _random_send_url(self, media_url: str, settings: dict) -> str:
        """把随机到的原图换成它的缩略图直链；取不到替身就原样返回原图。

        缩略图与原图落在同一个 Qdrant 点上（点 id = UUID5(原图 id)），所以只要
        随机到的是内容目录里的原图，就能按 id 查出它的 thumb_url——不需要映射表，
        也不能靠文件名（图床给缩略图改过名）。整条链路都失败时静默退回原图：
        替身只是提速手段，绝不能因为它缺席而让 /随机图 发不出图。
        """
        if not settings.get("send_thumbnail", True):
            return media_url
        file_id = self._bed_file_id(media_url)
        if not file_id:
            return media_url
        if not self.vector.qdrant_ready:
            # 向量库没配好就无从查替身；这不是错误，直接发原图
            return media_url
        try:
            thumb = await self.vector.thumb_url_for(file_id)
        except Exception as exc:  # thumb_url_for 已兜错，这里再保一层
            logger.warning(f"查询缩略图失败，改发原图: {exc}")
            return media_url
        if not thumb:
            logger.debug(f"该图无缩略图替身，按原图发送: {file_id}")
            return media_url
        logger.debug(f"随机图改发缩略图替身: {file_id} -> {thumb}")
        lan_hosts = self._lan_url_hosts(settings)
        if lan_hosts:
            # Qdrant 登记的 thumb_url 是公网直链，内网直连模式下改写为内网源
            thumb = swap_url_origin(thumb, str(settings.get("lan_base_url") or ""), lan_hosts)
        return thumb

    @staticmethod
    def _random_caption(
        media_url: str, kind: str, settings: dict, *, display_name: str = ""
    ) -> str:
        """随机媒体回传文案；show_file_info 关闭或取不到文件名时用固定文案。

        文件名一律经 clean_display_name 清洗：图床给缩略图改名时会前置毫秒
        时间戳（`1789658616018_【微博_…】.jpg`），这个前缀对用户毫无意义，
        纯属噪声，绝不能出现在配文里。清洗只做「去时间戳前缀」这一步确定的
        处理，不猜测、不改写其余内容。

        display_name 供调用方传入**更权威**的名字（如反查回原图时取自 Qdrant
        payload.file_name 的原始名）。它的价值在于缩略图名的 `@`→`_` 是**不可逆**
        的，光靠清洗缩略图名永远还原不出真名；有 payload 真名时优先用它。
        """
        label = "图片" if kind == "image" else "视频"
        fallback = f"随机{label}发送成功"
        if not settings.get("show_file_info", True):
            return fallback
        filename = str(display_name or "").strip() or clean_display_name(media_url)
        if not filename:
            return fallback
        icon = "🖼️" if kind == "image" else "🎬"
        return f"{icon} {filename}"

    async def send_random_media(
        self,
        event: AstrMessageEvent,
        directory: str | None = None,
        content_type: str | None = None,
    ):
        """发送随机图片或视频。

        当用户请求随机图片或视频时使用此工具。

        Args:
            directory(string): 目录路径，指定从哪个目录获取随机媒体，可选。
            content_type(string): 内容类型，可选值为 image 或 video，可选。
        """
        settings = self._random_settings()
        if not settings["enable_llm"]:
            yield event.plain_result("LLM 调用随机媒体已被禁用，请使用 /随机图 或 /随机视频 命令")
            return
        parsed_directory, parsed_type = extract_directory(event.message_str or "")
        if directory is None and parsed_directory:
            directory = parsed_directory
        content_type = (parsed_type or content_type or "image").strip().lower()
        async for result in self._random_media_delivery(event, directory, content_type, settings):
            yield result
