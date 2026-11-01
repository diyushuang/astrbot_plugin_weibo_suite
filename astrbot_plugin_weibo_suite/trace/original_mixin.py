"""随机图 / 随机视频 / 原图找回（CloudFlare-ImgBed）。"""

from __future__ import annotations

import asyncio
import os
import secrets
from urllib.parse import (
    unquote,
    urlsplit,
)

import astrbot.api.message_components as Comp
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
    from .image_bed import (
        MODE_CFB,
        redact_url,
    )
    from .image_delivery import (
        build_imgbed_file_url,
        build_original_url,
        upgrade_to_https,
    )
    from .random_media import (
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
    from .weibo_posts import (
        load_post_map,
        map_is_stale,
        parse_weibo_text,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from image_bed import (  # type: ignore[no-redef]
        MODE_CFB,
        redact_url,
    )
    from image_delivery import (  # type: ignore[no-redef]
        build_imgbed_file_url,
        build_original_url,
        upgrade_to_https,
    )
    from random_media import (  # type: ignore[no-redef]
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
    from weibo_posts import (  # type: ignore[no-redef]
        load_post_map,
        map_is_stale,
        parse_weibo_text,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        read_bytes,
        sanitize_error_text,
    )


class OriginalMixin:
    """随机图 / 随机视频 / 原图找回（CloudFlare-ImgBed）。"""

    # ------------------------------------------------------------------
    # 指令：随机图 / 随机视频 / 原图（CloudFlare-ImgBed 随机图接口）
    # ------------------------------------------------------------------

    async def random_image(self, event: AstrMessageEvent):
        """从图床随机取一张图片并回传（可带目录，如 /随机图 风景）"""
        settings = self._random_settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self._random_media_delivery(
            event, directory, parsed_type or "image", settings
        ):
            yield result

    async def random_video(self, event: AstrMessageEvent):
        """从图床随机取一个视频并回传（可带目录，如 /随机视频 风景）"""
        settings = self._random_settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self._random_media_delivery(
            event, directory, parsed_type or "video", settings
        ):
            yield result

    async def checkin(self, event: AstrMessageEvent):
        """每日签到，领取一张带边框与随机语句的随机缩略图"""
        async for result in self._checkin_flow(event):
            yield result

    @staticmethod
    def _first_file_segment(event: AstrMessageEvent):
        """取本条消息或引用消息里的第一个文件段；旧版本没有 File 时返回 None。"""
        file_type = getattr(Comp, "File", None)
        if not isinstance(file_type, type):
            return None
        reply_type = getattr(Comp, "Reply", None)
        for seg in getattr(event.message_obj, "message", None) or []:
            if isinstance(seg, file_type):
                return seg
            if reply_type is not None and isinstance(seg, reply_type):
                for sub in getattr(seg, "chain", None) or []:
                    if isinstance(sub, file_type):
                        return sub
        return None

    async def checkin_import(self, event: AstrMessageEvent):
        """管理员：导入微博文案 CSV/MD 到插件数据目录。"""
        if not event.is_admin():
            yield event.plain_result("仅管理员可导入签到文案。")
            return
        segment = self._first_file_segment(event)
        if segment is None:
            yield event.plain_result(
                "请把 CSV 或 MD 文件作为附件发送，并在同一条消息中发送 /签到文案导入。"
            )
            return
        name = os.path.basename(str(getattr(segment, "name", "") or "")).strip()
        try:
            getter = getattr(segment, "get_file", None)
            if callable(getter):
                try:
                    target = await getter(allow_return_url=True)
                except TypeError:  # 旧版本 get_file 可能没有该关键字
                    target = await getter()
            else:
                target = str(getattr(segment, "file", "") or "")
            if not target:
                yield event.plain_result("无法读取文件内容，请重新发送附件。")
                return
            if str(target).lower().startswith(("http://", "https://")):
                data = await self._download_bytes(str(target))
            else:
                data = await asyncio.to_thread(read_bytes, str(target))
        except Exception as exc:
            logger.warning(f"签到文案导入读取失败: {exc}")
            yield event.plain_result(f"读取文件失败：{sanitize_error_text(exc)}")
            return
        if len(data) > 4 * 1024 * 1024:
            yield event.plain_result("文件超过 4MB，请拆分后再导入。")
            return
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                yield event.plain_result("文件不是 UTF-8 编码，无法解析。")
                return
        extension = os.path.splitext(name)[1].lower()
        if extension not in {".csv", ".md", ".markdown"}:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if "序号" in first_line and "发布时间" in first_line:
                extension = ".csv"
            elif first_line.startswith("# 微博原创文案"):
                extension = ".md"
            else:
                yield event.plain_result(
                    "无法识别文件格式：只支持 CSV（序号,发布时间,来源,文案,微博链接）"
                    "或微博原创文案 MD 导出。"
                )
                return
        base = os.path.basename(name) or f"weibo_import_{secrets.token_hex(4)}{extension}"
        base = base.replace("/", "_").replace("\\", "_").strip()
        if (
            not base
            or base in {".", ".."}
            or not base.lower().endswith((".csv", ".md", ".markdown"))
        ):
            base = f"weibo_import_{secrets.token_hex(4)}{extension}"
        parsed = parse_weibo_text(text, base)
        if not parsed:
            yield event.plain_result("未解析到任何微博文案，请检查文件内容。")
            return
        try:
            await asyncio.to_thread(os.makedirs, self.weibo_dir, exist_ok=True)
            final_path = os.path.join(self.weibo_dir, base)
            temp_path = f"{final_path}.tmp"

            def write_text() -> None:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(temp_path, final_path)

            await asyncio.to_thread(write_text)
        except OSError as exc:
            logger.warning(f"签到文案导入写入失败: {exc}")
            yield event.plain_result(f"写入失败：{sanitize_error_text(exc)}")
            return
        self._weibo_posts_cache = None
        self._weibo_meta_cache = {}
        self._weibo_signature = None
        accounts = sorted({post.account for post in parsed})
        dates = sorted({post.date for post in parsed})
        account_text = "、".join(accounts[:5]) + ("…" if len(accounts) > 5 else "")
        date_text = f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:]}" if dates else "-"
        if dates and dates[-1] != dates[0]:
            date_text += f" ~ {dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}"
        map_path = self._weibo_map_path()
        map_note = (
            "映射表未生成：签到时会按同账号同日图片回退，建议运行 scripts/build_weibo_map.py。"
        )
        if os.path.isfile(map_path):
            map_posts, _generated, map_meta = await asyncio.to_thread(load_post_map, map_path)
            if map_is_stale(map_path, self.weibo_dir):
                map_note = "源文件已更新，映射表比源文件旧；请重新运行 scripts/build_weibo_map.py。"
            else:
                map_note = (
                    f"映射表：{len(map_posts)} 条，可配对 "
                    f"{int(map_meta.get('matched_count') or 0)} 条。"
                )
        yield event.plain_result(
            f"✅ 已导入 {base}：{len(parsed)} 条文案，账号 {account_text}，"
            f"日期 {date_text}。\n{map_note}"
        )

    def _imgbed_base(self) -> str:
        """/原图 直查用的图床站点地址（取第一个配置完整的）。

        CloudFlare-ImgBed 图床配置与本插件登记原图同站点，最权威；其次是
        随机图接口用的站点地址（同一台图床的另一处配置）。都没有时返回空串，
        由调用方给出「未配置图床站点」的提示，而不是拼一条必然 404 的链接。
        """
        bed = self._dict_cfg("image_bed")
        if str(bed.get("mode") or "").strip() == MODE_CFB:
            base = str(bed.get("cfi_base_url") or "").strip().rstrip("/")
            if base.lower().startswith(("http://", "https://")):
                return base
        base = str(self._dict_cfg("random_media").get("base_url") or "").strip().rstrip("/")
        return base if base.lower().startswith(("http://", "https://")) else ""

    @classmethod
    def _first_image_candidate(cls, event: AstrMessageEvent) -> str:
        """从本条消息/被引用消息的图片段里取一个**原始**文件名或直链。

        这里**不做任何清洗**，原样返回。这一点是必须的——调用方要先用
        is_thumbnail_url 判定它是否指向缩略图，而那个判据依赖路径里的
        `thumbnails/` 段；若先清洗成"裸文件名"，路径信息就丢了，判定会失效
        （实测：清洗后的名字 is_thumbnail_url 返回 False，于是缩略图检查形同
        虚设）。需要干净展示名时由调用方再走 clean_display_name。

        取不到返回空串。
        """
        for seg in cls._extract_images(event):
            for candidate in (getattr(seg, "url", None), getattr(seg, "file", None)):
                raw = str(candidate or "").strip()
                if raw and media_filename(raw):
                    return raw
        return ""

    @staticmethod
    def _original_usage() -> str:
        return (
            "本会话还没有回传过图片。/原图 也可直接按文件名到图床取原图：\n"
            "· /原图 文件名 —— 直接把图床里的原图取回来（无需先回传）\n"
            "· /原图 —— 取本会话最近回传过的图片的原图\n"
            "· 引用一张图发送 /原图 —— 按该图文件名到图床取原图"
        )

    async def _reverse_original(
        self, fetch, *, action: str, level: str = "warning"
    ) -> tuple[str, str]:
        """执行一次原图反查，返回 (payload 原名, 还原后的原图直链)。

        三个反查入口（缩略图直链 / 缩略图名 / 裸文件名）共用这段“不可用判定 →
        调用 → 异常只记日志 → 取 url → 按原图口径剥离处理参数”的骨架；各自的
        展示名回退顺序不同，留在调用点处理，避免把三种文案揉进一个函数。
        查不到/不可用一律返回 ("", "")，永不抛错。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        try:
            info = await fetch()
        except Exception as exc:  # 反查方法自身已兜错，这里再保一层
            getattr(logger, level)(f"{action}失败: {exc}")
            return "", ""
        if not isinstance(info, dict):
            return "", ""
        original = str(info.get("url") or "").strip()
        if not original:
            return "", ""
        return str(info.get("file_name") or "").strip(), build_original_url(original)

    async def _original_from_thumbnail(self, thumb_ref: str) -> tuple[str, str]:
        """把「指向缩略图的直链或名字」换成它对应的原图 (名称, 直链)。

        为什么必须在这里拦：`/原图` 的直查分支只会拿一个名字去拼
        `{base}/file/{name}`。当这个名字来自「用户引用了上一条 /随机图 发出的
        缩略图」时，它指向 `thumbnails/`，拼出来必然命中缩略图，于是
        `/原图` 忠实地把 720px 小图又发了一遍——线上报的故障就是这个。

        反查走 Qdrant：缩略图与原图落在**同一个点**上（payload 里
        `thumb_url` 与 `image_url` 并存），按 thumb_url 即可取回原图。
        这里特意**不按名字倒推点 id**——缩略图被图床改过名（前置毫秒时间戳、
        `@` 换 `_`），倒推做不到。

        **host 差异由反查侧吸收，这里不强行归一化**。实测该部署的 payload：
            thumb_url = http://10.0.0.5:7658/file/thumbnails/178965…_【微博_…】.jpg
            image_url = http://10.0.0.5:7658/file/12%E3%80%81…%E5%89%A7%E7%85%A7.jpg
        两者都记**内网 IP**；而插件运行期手上的直链可能来自公网反代
        `bed.example.com`（OpenResty → `10.0.0.6:8080`，同一图床）。故这里只把
        编码形态还原成原始形态（`unquote`，缩略图那侧存的是**未编码**串），
        实际匹配交给 `original_info_for_thumb`：它先试整串、再退回按 `/file/`
        之后的**对象键**匹配，从而与 host 无关。

        查不到时返回 ("", "")，调用方据此给出可读提示；宁可说「找不到」，也
        绝不把缩略图当原图发出去。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        reference = str(thumb_ref or "").strip()
        if not reference:
            return "", ""
        base = self._imgbed_base()
        # 统一成「与 payload 同形」的完整缩略图直链：
        # 1) 有站点前缀就直接用；2) 只有路径段则补上前缀；3) 两者都无则作罢
        if reference.lower().startswith(("http://", "https://")):
            full = reference
        elif base:
            full = f"{base}/file/{reference.lstrip('/')}"
        else:
            return "", ""
        # 关键：还原到**原始未编码**形态（payload.thumb_url 就是这么存的），
        # 同时去掉可能残留的处理参数（width/height/fit/fallback）
        thumb_url = unquote(build_original_url(full))
        display, url = await self._reverse_original(
            lambda: self.vector.original_info_for_thumb(thumb_url),
            action="缩略图反查原图",
        )
        if not url:
            return "", ""
        # 展示名**必须**优先取 payload 里的 file_name：缩略图名的 `@`→`_` 不可逆，
        # 光清洗缩略图名还原不出真名（实测 payload.file_name 才是原始名）
        return display or clean_display_name(url) or clean_display_name(reference) or "", url

    async def _original_from_thumb_name(self, name: str) -> tuple[str, str]:
        """按「缩略图改名后的文件名」反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的分工：那个认**带 `thumbnails/` 段**的直链，
        这个只认**文件名**，用于目录段已经在流转中丢掉的场景（QQ 把机器人发的图
        转存到自己的 CDN 后，插件拿到的只有裸文件名）。两者都是反查，返回的都
        是同一个 Qdrant 点上的原图直链。

        展示名取 payload.file_name（图床登记真名，`@` 完好）；反查失败返回空，
        由调用方继续走裸名探测与可读提示——**绝不把缩略图当原图发出去**。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        query = str(name or "").strip()
        if not query:
            return "", ""
        display, url = await self._reverse_original(
            lambda: self.vector.original_info_for_thumb_name(query),
            action="按缩略图名反查原图",
            level="debug",
        )
        if not url:
            return "", ""
        return display or clean_display_name(url) or query, url

    async def _lookup_imgbed_file(self, name: str, *, quoted: str = "") -> tuple[str, str, str]:
        """按文件名到图床直查原图，返回 (名称, 直链, 失败提示)。

        **查询顺序：先向量库按名反查，再退回裸名拼 URL + 探测。** 反查在前是
        必须的，不是优化：图床的 `/file/{path}` 只认**完整对象键**（含各级目录），
        裸文件名拼上去**必然 404**。线上报的

            图床里没有找到「【微博@示例博主】20240101-04：示例海报.jpg」

        就是这个：原图实际在 `/file/7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》
        图集/` 下面，而插件拼的是 `/file/【微博@潮流合伙人】…jpg`。实测 A/B：
        裸名 404 / 带目录 200（内网与公网域名结果一致）。Qdrant 的 payload
        `image_url` 正是入库时登记的**带完整目录**的权威直链，拿名字反查它即可。

        反查非「恰好 1 命中」时（同名文件在多目录下有副本、或该图未进索引）不硬猜，
        退回原有的裸名拼 URL + 探测逻辑：那一步虽然对多级目录无能为力，但对
        「图床根目录下的文件」以及**用户直接给完整相对路径**的情形仍然有效，
        且探测结果能给出可读提示。

        探测为「确定不存在」（4xx）时返回空直链并给出提示，同时附上尝试过的
        完整直链，便于用户核对图床里的目录层级；探测结果未知（网络异常）不拦，
        仍把直链交给回传——网络抖动不该被当成「图床没有这张图」。

        name 指向缩略图时（`thumbnails/...`，典型来自引用上一条随机图消息）先
        反查真正的原图直链再返回，绝不把缩略图当原图交付。quoted 是引用图里的
        **原始直链**（保留 `thumbnails/` 段），有它时优先用它反查——name 可能
        已被清洗掉时间戳前缀，而 strip 之后的形态与 Qdrant 里存的并不完全一致。
        """
        base = self._imgbed_base()
        if not base:
            return (
                "",
                "",
                "未配置图床站点地址：请在「图床设置」（cloudflare_imgbed）或「随机图」里"
                "填写站点地址后重试，或先 /溯源 命中再发 /原图。",
            )
        # 判定要用「原始引用直链」与「名字」两者：任一指明 thumbnails/ 都算命中
        if is_thumbnail_url(quoted) or is_thumbnail_url(name):
            original_name, original_url = await self._original_from_thumbnail(quoted or name)
            if original_url:
                logger.info(
                    f"/原图 直查命中缩略图，已反查回原图: {quoted or name} -> "
                    f"{redact_url(original_url)}"
                )
                return original_name, original_url, ""
            return (
                "",
                "",
                f"「{name}」是图床里的缩略图，且查不到它对应的原图；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。",
            )
        # ① 先按文件名反查：拿回入库时登记的、**带完整目录**的权威直链。
        #    这一步解决「裸名拼 URL 必然 404」——图床 /file/ 只认完整对象键。
        #    注意判据是 `found[1]` 而**不是** `if found:`：`_original_by_name`
        #    以 `("", "")` 表示落空，而**非空元组恒为真**——写成 `if found:`
        #    会在落空时也走进来，直接返回空直链（调用方拿到空 url 会当成
        #    「查不到」），恰好把兜底路径②整个跳过。这是个静默错误：
        #    没有异常、没有日志，只是所有直查都失效。
        found = await self._original_by_name(name)
        if found[1]:
            original_name, original_url = found
            logger.info(f"/原图 按名反查命中原图: {name} -> {redact_url(original_url)}")
            return original_name, original_url, ""
        # ①.5 名字本身是「缩略图被改名后的文件名」时，按缩略图名反查原图。
        #     这一段要放在 ② 之前：这类名字（`1789…_【微博_…】.jpg`）在图床里
        #     只存在于 `thumbnails/` 下，裸名拼 URL 必然 404，探测只会给出
        #     「图床里没有找到」这种让人白核对的提示。
        #
        #     为什么 ① 拦不住它：① 按 payload.file_name 精确匹配，而缩略图名
        #     被图床改过（前置毫秒时间戳、`@` 换成 `_`，且 `_` 不可逆）。
        #     为什么 quoted 也可能拦不住：用户引用机器人消息时，QQ 会把图转存到
        #     自己的 CDN，插件手上的直链里**没有 `thumbnails/` 段**，
        #     `is_thumbnail_url` 只能返回 False，于是走到这里。
        #
        #     只对「裸文件名」生效：入参自带目录时（`/原图 目录/海报.jpg`）说明
        #     用户给的是**原图路径**，而缩略图名反查只按 basename 比对，会把别的
        #     目录下同名缩略图的原图当成本次要取的文件——比查不到更糟。
        thumb_hit = ("", "")
        if "/" not in name:
            thumb_hit = await self._original_from_thumb_name(name)
        if thumb_hit[1]:
            original_name, original_url = thumb_hit
            logger.info(f"/原图 按缩略图名反查回原图: {name} -> {redact_url(original_url)}")
            return original_name, original_url, ""
        # ② 反查未命中（未进索引 / 同名多份 / 向量检索关闭）→ 退回裸名拼 URL + 探测，
        #    对根目录文件与「用户直接给完整相对路径」两种情形仍然管用
        candidate = build_imgbed_file_url(base, name)
        if not candidate:
            return (
                "",
                "",
                f"「{name}」不像图床里的文件名，无法直接取原图；"
                "可先用 /溯源 命中该图后再发 /原图。",
            )
        exists, _length = await self._probe_url(candidate)
        if exists is False:
            # 落空原因不同，提示必须不同：库里压根没这个名字（未进索引）时让用户
            # 去 /溯源；而同名多份且内容不同时图是**确实存在**的，报「图床里没有
            # 找到」只会让人白核对一遍。命中数只在失败路径上多查一次。
            matches = await self._file_name_matches(name)
            if matches and matches > 1:
                return (
                    "",
                    "",
                    f"「{name}」在图库里有 {matches} 份同名文件且内容不同，"
                    "无法确定你要哪一张。请带上目录再试（如："
                    f"/原图 目录名/{name}）。",
                )
            # 失败文案只带路径、不带主机：图床地址可能配置成内网 IP/内网域名，
            # 群聊里不应出现；日志里保留 scheme+host+path 便于管理员核对，但查询
            # 串（可能含签名/下载凭据）一律脱敏
            candidate_path = unquote(urlsplit(candidate).path)
            logger.info(f"/原图 图床直查未命中: {name} -> {redact_url(candidate)}")
            return "", "", f"图床里没有找到「{name}」，已尝试路径：{candidate_path}"
        return name, candidate, ""

    async def _file_name_matches(self, name: str) -> int | None:
        """问向量库「这个名字有几个点」；用于失败文案的诊断，查不到返回 None。

        对齐库内 `VectorEngine.file_name_matches`——本方法存在的意义只是把
        「引擎未启用 / 异常」收敛成 None，让调用方不必到处判 self.vector。
        """
        if not self.vector.qdrant_ready:
            return None
        try:
            return await self.vector.file_name_matches(name)
        except Exception as exc:  # 诊断失败不能影响原本的失败提示
            logger.debug(f"同名命中数诊断失败: {exc}")
            return None

    async def _original_by_name(self, name: str) -> tuple[str, str]:
        """按裸文件名到向量库反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的区别：那个按**缩略图直链**反查（用于拦
        「引用随机图发出来的图」），这个按**文件名**反查（用于 `/原图 文件名`
        这条「没有历史、只有名字」的路径）。

        名字里可能只有裸文件名，而图床里它在多级目录下——payload.image_url 存的
        是入库时的完整直链，故必须由库来告诉我们目录，不能靠猜。

        跳过 name 自带目录（含 `/`）的情形：那说明调用方已经拿到了相对路径，
        直接交给图床按路径取更准，不必多此一举反查（反查只匹配裸 file_name，
        带目录的名字在这一步本来就命中不了）。

        展示名用 payload.file_name（图床登记真名，`@` 完好），比入参更可信。
        本方法**永不抛错**：反查只是获取目录层级的捷径，失败由调用方走兜底。
        """
        query = str(name or "").strip()
        if not query or "/" in query or not self.vector.qdrant_ready:
            return "", ""
        display, url = await self._reverse_original(
            lambda: self.vector.original_by_file_name(query),
            action="按文件名反查原图",
        )
        if not url:
            return "", ""
        return display or query, url

    async def original_repair(self, event: AstrMessageEvent):
        """清理会话历史里带配文前缀/装饰的脏键（1.6.1 前的旧数据）。

        为什么需要这个命令：1.6.1 之前 `_remember_blocks` 会把配文前缀
        （`🖼️ `、`🎬 `、多图的 `1. `）连同文件名一起登记进历史。于是

            引用那条带前缀的消息发 /原图 → 历史/引用图里取到的名字自带前缀
            → 拼成 /file/🖼️%20【…】 → 必然 404
            → 提示文案里再复述一遍带前缀的名字，用户完全看不出问题在哪儿

        清洗逻辑现在已修（见 random_media.clean_display_name），**新写入的键是
        干净的**；但旧键是纯内存 LRU，不会自己消失——只在进程重启或该会话累积
        到上限被淘汰时才可能挤掉。故给一个显式入口。

        只管历史，**不动图床**：前缀从来不是图床里的真实文件，脏的只是本地键。
        """
        cleaned = self._repair_history_keys()
        if not cleaned:
            yield event.plain_result(
                "✅ 会话历史里没有带前缀的脏记录，无需修复。\n"
                "（前台提示文案本插件从不写入文件，无需清理）"
            )
            return
        logger.info(f"/原图修复 清理了 {cleaned} 条带前缀的会话历史键")
        yield event.plain_result(
            f"✅ 已清理 {cleaned} 条带前缀的会话历史记录。\n"
            "现在可以重新「引用那张图 + /原图」取原图了；"
            "若仍取不到，再引用一次 /随机图 刚发出的图重试。"
        )

    def _repair_history_keys(self) -> int:
        """把会话历史里带装饰前缀的键改名成干净形态；返回处理条数。

        做的是**改名而非删除**：URL 是好的，脏的只是键。直接删会让用户白白
        丢掉一条本来可用的历史，改名则让它立刻变成可命中的干净键。改名与
        LRU 归位由 MediaHistory.repair_keys 统一处理（不再访问其私有结构）。
        """
        return self.history.repair_keys(clean_display_name)

    async def original_image(self, event: AstrMessageEvent):
        """重发最近回传过的图片的原图；也可直接按文件名到图床取（/原图 风景.jpg）"""
        session = self._session_key(event)
        query = self._strip_command(event.message_str or "", ("原图",)).strip(" \t:：,，")
        name = ""
        url = ""
        # 引用图里的**原始**直链/名字：保留 `thumbnails/` 路径段，供缩略图判定与
        # 反查使用（反查要拼出与 Qdrant payload 同形的完整缩略图直链）
        quoted = ""
        # 取值来源，只用于日志：历史命中 / 按名直查 / 缩略图反查。
        # 线上排查时「/原图 到底走了哪条路」是最关键的一条信息，而历史命中
        # 这条最常见、却原本一行日志都不打，导致日志里完全看不到命令执行过。
        source = "history"
        if query:
            status, payload = self.history.find(session, query)
            if status == "ambiguous":
                candidates = "、".join(payload[:5])
                yield event.plain_result(f"匹配到多张图片，请写出更完整的文件名：{candidates}")
                return
            if status == "found":
                name, url = payload
            else:
                # 历史没命中时 query 会退化成**直查图床的文件名/相对路径**，此时它
                # 可能自带装饰前缀——最常见的是用户从配文里复制粘贴（配文是
                # `🖼️ 名字`，复制过来就带了 `🖼️ `）。不清洗就会拼出
                # `/file/🖼️%20名字` 而必然 404。命中历史的分支无需清洗：
                # 历史键本就是干净文件名（1.6.1 起写入时即已清洗）。
                #
                # 这里用 strip_leading_decoration 而**不是** clean_display_name：
                # 后者靠 media_filename 取末段，会把用户手写的多级相对路径
                # （`7、综艺节目/…/海报.jpg`）削成裸文件名——图床按裸名取不到，
                # 于是「用户给足了信息反而查不到」。前者原地剥前缀、保住目录。
                query = strip_leading_decoration(query) or query
        else:
            latest = self.history.latest(session)
            if latest is not None:
                name, url = latest
            else:
                # 会话里没有回传记录：退化为「按消息/引用图里的文件名直查图床」
                quoted = self._first_image_candidate(event)
                query = clean_display_name(quoted) or ""

        if not url:
            if not query:
                yield event.plain_result(self._original_usage())
                return
            source = "lookup"
            name, url, message = await self._lookup_imgbed_file(query, quoted=quoted)
            if not url:
                yield event.plain_result(message)
                return
        elif is_thumbnail_url(url):
            # 兜底：历史里若因任何原因（旧版本写入、外部注入）存进了缩略图直链，
            # 也要在发出前反查回原图。正常路径下历史记的是原图（见 _yield_delivery
            # 的 remember_blocks），这里是防御，不依赖上游一定正确。
            original_name, original_url = await self._original_from_thumbnail(
                self._bed_file_id(url) or name
            )
            if original_url:
                logger.info(
                    f"/原图 历史命中缩略图，已反查回原图: {redact_url(url)} -> "
                    f"{redact_url(original_url)}"
                )
                name, url = original_name, original_url
                source = "history-thumb-reverse"
            else:
                yield event.plain_result(
                    f"本会话记录的「{name}」是缩略图，且查不到它对应的原图；"
                    "可先用 /溯源 命中该图后再发 /原图。"
                )
                return
        # R6：/原图 只发 URL、不落地字节——剥离 ImgBed 处理参数还原未处理原文件，
        # 并强制 url 通道 + 禁用本地回退
        final_url = build_original_url(url)
        # 最后一次校验：走到这里说明上游每一条路径都「自认为」拿到的是原图，
        # 但只要最终直链仍指向缩略图目录，就绝不能发出去——静默发出一张 720px
        # 小图，用户只会觉得「/原图 又没生效」，比明确报错难排查得多。
        if is_thumbnail_url(final_url):
            logger.warning(f"/原图 最终直链仍是缩略图，已拒绝发送: {redact_url(final_url)}")
            yield event.plain_result(
                f"取到的「{name}」仍是图床里的缩略图直链，已中止发送；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。"
            )
            return
        logger.info(f"/原图 发出（来源={source}）: {name} <- {redact_url(final_url)}")
        block = {"url": final_url, "file_name": name}
        async for result in self._yield_delivery(
            event,
            f"🖼️ {name}（原图）",
            [block],
            mode_override="url",
            allow_local_fallback=False,
        ):
            yield result

    async def _random_media_delivery(
        self, event: AstrMessageEvent, directory, content_type: str, settings: dict
    ):
        """取一条随机媒体并回传。

        图片走 _yield_delivery（复用全局通道：auto / bytes / url / url-scaled /
        native），视频走标准消息链 Video.fromURL；无法识别类型时回退纯文本
        直链。同域 http 直链先升级为 https，减少协议端一次跳转。
        """
        # 未指定目录时回退到配置的 default_dir 池：图床若设置了
        # randomImageAPI.allowedDir，不带 dir 的裸 /random 会被判为「目录不允许」
        # 而 403，故由客户端补一个目录；池有多项时随机取其一，以尽量覆盖整库。
        target_dir = str(directory or "").strip() or pick_default_directory(settings)
        try:
            media_url = await self._random_client().fetch(target_dir, content_type)
        except RandomMediaError as exc:
            logger.warning(f"获取随机媒体失败: {exc}")
            yield event.plain_result(f"⚠️ 获取随机媒体失败：{sanitize_error_text(exc)}")
            return
        lan_hosts = self._lan_url_hosts(settings)
        if lan_hosts:
            # 内网直连：床内直链统一改写为内网源（/random 直链可能已是内网源，
            # 改写幂等；公网域名形式的直链也一并收进来），后续取字节不出公网
            media_url = swap_url_origin(media_url, settings["lan_base_url"], lan_hosts)
        media_url = upgrade_to_https(media_url, settings["base_url"])
        target = self._delivery_target(event)
        kind = media_kind(media_url, content_type)
        if kind == "video":
            # 官方机器人等平台把视频 URL 交给腾讯服务器拉流，内网地址它访问
            # 不到，须换回公网直链；换不出来（第三方内网地址等）就保持原样，
            # 与图片 URL 上传共用同一套换源判定（_public_media_url）
            send_url = media_url
            if target.offbox_upload:
                send_url = self._public_media_url(media_url, settings) or media_url
            caption = self._random_caption(media_url, "video", settings)
            yield event.chain_result([Comp.Plain(caption), Comp.Video.fromURL(send_url)])
            return
        if kind == "image":
            send_url, original_url, original_name = await self._random_image_from_media(
                media_url, settings
            )
            # 配文一律用**原图**名并清洗掉图床改名的前缀（时间戳 + `@` 换 `_`），
            # 显示缩略图名对用户没有意义
            caption = self._random_caption(
                original_url, "image", settings, display_name=original_name
            )
            # qq_official 等平台的假格式体检已下沉到 _yield_delivery 统一处理
            blocks = [
                {
                    "url": send_url,
                    "file_name": original_name or clean_display_name(original_url) or "",
                }
            ]
            async for result in self._yield_delivery(
                event,
                caption,
                blocks,
                remember_blocks=(blocks if send_url == original_url else [{"url": original_url}]),
            ):
                yield result
            return
        yield event.plain_result(f"随机媒体获取成功：{media_url}")

    async def _random_image_candidate(self, directory, settings: dict) -> tuple[str, str, str]:
        """取一张随机图并归一为 (发送直链, 原图直链, 原图权威名)。

        /签到 与 /随机图 共用这段「取图 → 内网改写/HTTPS 升级 → 缩略图反查 →
        选发送态」链路。抽出来是为了让「发缩略图、历史留原图」的口径只存在
        一份：任何一处改动都不会让两条命令各自漂移。

        失败抛 RandomMediaError，由调用方给出可读提示。
        """
        # 未指定目录时回退到配置的 default_dir 池：图床若设置了
        # randomImageAPI.allowedDir，不带 dir 的裸 /random 会被判为「目录不允许」
        # 而 403，故由客户端补一个目录；池有多项时随机取其一，以尽量覆盖整库。
        target_dir = str(directory or "").strip() or pick_default_directory(settings)
        media_url = await self._random_client().fetch(target_dir, "image")
        lan_hosts = self._lan_url_hosts(settings)
        if lan_hosts:
            # 内网直连：床内直链统一改写为内网源（/random 直链可能已是内网源，
            # 改写幂等；公网域名形式的直链也一并收进来），后续取字节不出公网
            media_url = swap_url_origin(media_url, settings["lan_base_url"], lan_hosts)
        media_url = upgrade_to_https(media_url, settings["base_url"])
        return await self._random_image_from_media(media_url, settings)

    async def _random_image_from_media(
        self, media_url: str, settings: dict
    ) -> tuple[str, str, str]:
        """把随机到的图片直链归一为 (发送直链, 原图直链, 原图权威名)。

        发送用缩略图替身、历史留原图：QQ 侧下载小图更快，sendMsg 回执超时
        概率显著下降；而 /原图 仍能凭历史里的原图直链取回全尺寸文件。

        边界：图床的 /random 若被指到 thumbnails/ 目录（default_dir 配错或
        用户显式传了该目录），media_url 本身就是缩略图。此时别再查替身
        （缩略图没有替身），并把「原图」口径定为反查结果，避免历史里记进
        缩略图直链、导致 /原图 之后只能拿回小图。
        """
        lan_hosts = self._lan_url_hosts(settings)
        original_url = media_url
        original_name = ""
        if is_thumbnail_url(media_url):
            info = None
            try:
                normalized = unquote(build_original_url(media_url))
                info = await self.vector.original_info_for_thumb(normalized)
            except Exception as exc:  # 反查失败不影响发送，按原样处理
                logger.debug(f"随机图反查原图失败（按原样处理）: {exc}")
            if info and info.get("url"):
                original_url = info["url"]
                if lan_hosts:
                    # Qdrant 反查回的是公网直链，内网直连模式下同样改写
                    original_url = swap_url_origin(
                        original_url, settings["lan_base_url"], lan_hosts
                    )
                # 真名取自 payload.file_name（图床没改过名的原始名）
                original_name = str(info.get("file_name") or "").strip()
            else:
                # 2026-10-20 排查实录：反查失败的原因有多种——引擎未配置、
                # Qdrant 请求失败、数据未命中——后两者各有 DEBUG 记录（线上
                # 默认 INFO 看不到），而「引擎未配置」历史上完全静默，导致
                # warning 里看不出任何线索、跨实例排查只能逐台实测。把引擎
                # 状态直接写进 warning：未配置 → 给配置指引；缺 embed（此时
                # 反查仍可用）→ 说明只影响以图搜图；配置齐全 → 指向 DEBUG。
                if not self.vector.qdrant_ready:
                    hint = "向量引擎未配置，请在插件配置 vector_search 一节补齐"
                elif not self.vector.enabled:
                    hint = "向量引擎缺 embed 配置（反查仍可用，仅以图搜图不可用）"
                else:
                    hint = "原因见 DEBUG 日志"
                logger.warning(
                    f"随机图命中的是缩略图且反查不到原图（{hint}）: {redact_url(media_url)}"
                )
        send_url = await self._random_send_url(original_url, settings)
        return send_url, original_url, original_name
