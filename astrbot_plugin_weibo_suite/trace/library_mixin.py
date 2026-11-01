"""登记、状态、重扫、删除与帮助指令的实现。"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import (
    logger,
)
from astrbot.api.event import (
    AstrMessageEvent,
)

try:
    from ..shared.common import (
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCAN_CONCURRENCY,
    )
    from .features import (
        compute_features,
        compute_features_with_mime,
        is_degenerate_phash,
    )
    from .image_bed import (
        MODE_CFB,
    )
    from .library import (
        DEGENERATE_SKIP,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from features import (  # type: ignore[no-redef]
        compute_features,
        compute_features_with_mime,
        is_degenerate_phash,
    )
    from image_bed import (  # type: ignore[no-redef]
        MODE_CFB,
    )
    from library import (  # type: ignore[no-redef]
        DEGENERATE_SKIP,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCAN_CONCURRENCY,
    )

try:
    from .constants import (
        _FORCE_TOKENS,
        _FORCE_TRIM_CHARS,
        DEGENERATE_MESSAGE,
        IMAGE_EXTS,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        _FORCE_TOKENS,
        _FORCE_TRIM_CHARS,
        DEGENERATE_MESSAGE,
        IMAGE_EXTS,
    )


class LibraryMixin:
    """登记、状态、重扫、删除与帮助指令的实现。"""

    # ------------------------------------------------------------------
    # 指令：登记原图
    # ------------------------------------------------------------------

    async def register_image(self, event: AstrMessageEvent):
        """将消息或引用中的图片作为原图登记入库（默认仅管理员）"""
        if self._bool_cfg("register_admin_only", True) and not event.is_admin():
            yield event.plain_result("⛔ 仅管理员可登记原图（可在插件配置中修改）。")
            return

        images = self._extract_images(event)
        if not images:
            yield event.plain_result(
                "请与图片同条消息发送 /登记原图 [备注]，或引用一张图片发送 /登记原图。"
            )
            return

        note = self._strip_command(event.message_str, ("登记原图", "原图登记"))
        seg = images[0]
        local_path = ""
        is_tmp = False
        stored = None
        db_saved = False
        try:
            resolved = await self._resolve_local(
                seg,
                probe=lambda path: compute_features_with_mime(path, self.hash_size),
            )
            local_path, is_tmp, fail_message = (
                resolved.path,
                resolved.is_tmp,
                resolved.message,
            )
            if not local_path:
                yield event.plain_result(fail_message)
                return
            size_bytes = os.path.getsize(local_path)
            if size_bytes > self._download_limit_bytes():
                yield event.plain_result("⚠️ 图片超过大小上限，已取消登记。")
                return

            feats = resolved.features
            if feats is None:
                feats = await asyncio.to_thread(compute_features, local_path, self.hash_size)
            if is_degenerate_phash(feats.phash):
                # 纯色/大面积平坦图的 pHash 退化成全零码：登记入库后任何一张
                # 同类图都会以相似度 1.0 命中这一行，图库等于被永久污染。
                # 这里直接拒收，比入库后再靠查询侧过滤要诚实。
                yield event.plain_result(DEGENERATE_MESSAGE)
                return
            dup = self.library.find_by_phash(feats.phash)
            if dup is not None:
                yield event.plain_result(self._dup_message(dup))
                return

            stored = await self.bed.store(local_path)
            row = {
                "phash": feats.phash,
                "dhash": feats.dhash,
                "ahash": feats.ahash,
                "width": feats.width,
                "height": feats.height,
                "file_size": size_bytes,
                "image_url": stored.url or "",
                "file_path": stored.file_path or "",
                "source": "register",
                "note": note,
                "group_id": event.get_group_id() or "",
                "sender_id": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
            }
            # 查重与插入在库方法的同一个锁区间内完成：上传期间的并发同图
            # 登记不会产生重复行（不再依赖"检查与插入之间无 await"的约定）
            entry_id, dup = await asyncio.to_thread(self.library.add_if_absent, row)
            if entry_id is DEGENERATE_SKIP:
                # 兜底：上面的预检与这里之间没有 await，正常不会走到，但库层
                # 才是退化判定的唯一权威，两个入口的口径必须一致
                await self._cleanup_stored(stored)
                yield event.plain_result(DEGENERATE_MESSAGE)
                return
            if dup is not None:
                await self._cleanup_stored(stored)
                yield event.plain_result(self._dup_message(dup))
                return
            db_saved = True
            lines = [
                f"✅ 已登记原图 #{entry_id}（{stored.message}）",
                f"尺寸：{feats.width}x{feats.height}",
            ]
            if note:
                lines.append(f"备注：{note}")
            if stored.url:
                lines.append(f"直链：{stored.url}")
            if (
                self.vector.enabled
                and self._vector_index_on_register()
                and not is_degenerate_phash(feats.phash)
            ):
                if self._bed_mode() == MODE_CFB:
                    # 图床侧钩子会把上传自动同步进向量库，无需插件再写一次
                    logger.debug(
                        "cloudflare_imgbed 模式：上传由图床侧钩子自动入库，跳过插件侧向量同步"
                    )
                elif not stored.url:
                    # 本地副本模式没有可供回图的公开直链，向量命中后也无法回传
                    logger.debug("当前图床模式没有公开直链，跳过向量同步")
                else:
                    try:
                        await self.vector.upsert_file(
                            local_path,
                            # 内容指纹作为 point id：同图同 ID，与图床 URL 规则无关
                            f"phash:{feats.phash}",
                            stored.url,
                            size_bytes,
                            feats.width,
                            feats.height,
                            src=self.bed.src_path_for(stored.url),
                            mime=resolved.mime,
                        )
                        lines.append("已同步至 Qdrant 向量库")
                    except Exception as e:
                        # 图片已上传、记录已入库，向量同步失败只是增量失败，
                        # 不能让用户误以为整次登记失败
                        logger.warning(f"登记原图向量同步失败: {e}")
                        lines.append("（向量同步失败，可在图床侧触发钩子或对账补齐）")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            if stored is not None and not db_saved:
                await self._cleanup_stored(stored)
            logger.error(f"登记原图失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 登记失败，详情请查看机器人日志。")
        finally:
            if is_tmp:
                self._remove_quiet(local_path)

    @staticmethod
    def _dup_message(dup) -> str:
        note = f"，备注：{dup['note']}" if dup["note"] else ""
        return f"该图片已登记过（#{dup['id']}{note}）。"

    @staticmethod
    def _strip_command(message_str: str, keywords: tuple[str, ...]) -> str:
        """取指令关键词之后的内容作为备注。"""
        text = (message_str or "").strip()
        for kw in keywords:
            idx = text.find(kw)
            if idx != -1:
                return text[idx + len(kw) :].strip()
        return text

    # ------------------------------------------------------------------
    # 指令：状态 / 重扫 / 删除 / 帮助
    # ------------------------------------------------------------------

    async def status(self, event: AstrMessageEvent):
        """查看图库统计与插件配置摘要"""
        stats = self.library.stats()
        scan_dirs = self._scan_dirs()
        lines = [f"📚 图片溯源图库状态：共 {stats['total']} 条（索引 {stats['indexed']}）"]
        if stats["skipped"]:
            lines.append(
                f"⚠️ 有 {stats['skipped']} 条因哈希精度不匹配未参与检索，"
                "建议执行 /溯源重扫 force 重建。"
            )
        if stats["by_source"]:
            lines.append(
                "· 来源：" + "，".join(f"{k or '未知'} {v}" for k, v in stats["by_source"].items())
            )
        lines.append(
            f"· pHash 精度：{self.hash_size * self.hash_size}bit（hash_size={self.hash_size}）"
        )
        lines.append(f"· 相似度阈值：{self.hash_threshold * 100:.0f}%")
        lines.append(self._storage_line())
        engine = self._engine_choice()
        lines.append(f"· 检索引擎：{engine}（向量{'已启用' if self.vector.enabled else '未启用'}）")
        if self.vector.enabled:
            try:
                vec_count = await self.vector.count()
                count_text = f"{vec_count} 条"
            except Exception as e:
                # 原始错误可能包含内网地址/Key，只进日志，不进群聊
                logger.warning(f"查询向量库点数失败: {e}")
                count_text = "不可用（连接或鉴权异常，详情见机器人日志）"
            lines.append(f"· 向量库：{self.vector.collection}，{count_text}")
            lines.append(f"· Embed：{self.vector.embed_model} / {self.vector.image_input}")
        lines.append(f"· 扫描目录：{len(scan_dirs)} 个")
        lines.append(self._remote_hash_line())
        lines.append(self._bed_status_line())
        lines.append(await self._weibo_status_line())
        lines.append(f"· AI 复核：{'开' if self._bool_cfg('ai_verify', False) else '关'}")
        yield event.plain_result("\n".join(lines))

    def _remote_hash_line(self) -> str:
        """/溯源状态 的图床哈希索引一行。

        排查「为什么图床的图哈希查不到」时，这一行就能区分三种成因：
        开关没开 / 拉取失败（条数 0）/ 拉到了但被大量跳过（skipped 偏高）。
        """
        if not self.remote_hash_on():
            reason = (
                "未配置 Qdrant（vector_search.qdrant_url）"
                if not getattr(self.vector, "qdrant_ready", False)
                else "已关闭"
            )
            return f"· 图床哈希索引：{reason}"
        info = self.remote_hash.stats()
        if not info["count"]:
            return "· 图床哈希索引：空（拉取中或拉取失败，详情见机器人日志）"
        text = f"· 图床哈希索引：{info['count']} 条可用"
        if info["skipped"]:
            # 只报总数时，「跳过 2000 条」既可能是线上脏数据，也可能是这边没带
            # image_url 字段，用户/排障者完全无从分辨；有分类就带上最高那一类
            reasons = info.get("skipped_reasons") or {}
            top = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else ""
            suffix = f"，主要是{top}" if top else ""
            text += f"，跳过 {info['skipped']} 条（格式错误/退化图/无直链{suffix}）"
        if info["built_at"]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(info["built_at"]))
            text += f"（建于 {stamp}）"
        return text

    def _bed_status_line(self) -> str:
        """/溯源状态 的图床可用性一行。

        上传持续失败时登记会静默降级为本地副本（只在第一次打一条 warning），
        用户几天后只会疑惑"直链怎么都是本地路径"，所以把降级状态摆到状态里。
        """
        try:
            info = self.bed.status()
        except Exception:  # pragma: no cover - 老版本客户端无 status()
            return ""
        if not info.get("degraded"):
            return ""
        since = info.get("degraded_since") or 0
        stamp = time.strftime("%m-%d %H:%M", time.localtime(since)) if since else "未知"
        return (
            f"⚠️ 图床不可用（{info.get('mode') or '未知'}），自 {stamp} 起登记已降级为"
            "本地副本；上传恢复后自动解除（详情见机器人日志）"
        )

    @staticmethod
    def _parse_force_flag(message_str: str) -> bool:
        """解析 /溯源重扫 是否带强制重建开关。

        旧实现是 `"force" in message_str.lower()`：指令后面随手跟一个含
        force 的路径（D:\\force\\pics）或 "forceful" 都会触发整库重建。

        现在只认「指令之后剩余的词全部是开关词」这种形态：多写任何别的内容
        都按没带开关处理。这个命令不接受参数，因此这样做没有功能损失；反过来
        也顺带挡住「引用了一条含 force 的长消息」被误判成重建请求的情形。
        """
        text = (message_str or "").strip().lower()
        if not text:
            return False
        for sep in (",", "，", ";", "；", "/", "\\", "|"):
            text = text.replace(sep, " ")
        words = [word.strip(_FORCE_TRIM_CHARS) for word in text.split()]
        words = [word for word in words if word]
        if not words:
            return False
        # 第一个词是指令本身（/溯源重扫），开关只看它后面
        rest = words[1:] if len(words) > 1 else words
        return all(word in _FORCE_TOKENS for word in rest)

    @staticmethod
    def _collect_image_files(dirs: list) -> tuple:
        """递归收集目录下的图片文件（同步、耗时，调用方需放到线程里执行）。

        返回 (文件列表, 成功遍历的目录列表)：遍历报错的目录（如网络盘
        掉线）会被跳过并记日志，不进入成功集合，从而不影响 prune 的
        清理范围判定。
        """
        files: list = []
        ok_dirs: list = []
        for d in dirs:
            errors: list = []

            def _onerror(err, errors=errors):
                errors.append(err)

            for root, _sub, names in os.walk(d, onerror=_onerror):
                for name in names:
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        files.append(os.path.abspath(os.path.join(root, name)))
            if errors:
                logger.warning(f"扫描目录 {d} 时出错（已跳过该目录的失效清理）: {errors[0]}")
            else:
                ok_dirs.append(d)
        return files, ok_dirs

    async def rescan(self, event: AstrMessageEvent):
        """扫描配置的本地图库目录，建立/更新索引（管理员）"""
        try:
            force = self._parse_force_flag(event.message_str)
            valid_dirs = [os.path.abspath(d) for d in self._scan_dirs() if os.path.isdir(d)]
            invalid = len(self._scan_dirs()) - len(valid_dirs)
            if not valid_dirs:
                yield event.plain_result(
                    "未配置有效的扫描目录：请在插件配置 scan_dirs 中填写"
                    "图片库/图床存储目录的绝对路径。"
                )
                return

            # 目录遍历可能耗时很久，放到线程里避免阻塞事件循环
            files, scanned_dirs = await asyncio.to_thread(self._collect_image_files, valid_dirs)
            # force 不再先删后建：删除与插入放进 replace_source 的同一个事务，
            # 中途异常/进程被杀时旧索引原样保留，最坏只是这次重建没生效。
            # 非 force 仍走增量 add_many，旧行为不变。
            if force:
                # 重建语义：库里的旧 scan 行既不进 known_paths 也不进
                # known_phashes，否则所有文件都会被判成"已存在"而整批跳过，
                # 所谓重建等于什么都没做。
                known_paths: set = set()
                known_phashes: set = set()
                scan_paths: set = set()
            else:
                # 这两次都是全表扫描，同步跑会卡住事件循环（大库上是秒级）
                scan_paths = await asyncio.to_thread(self.library.get_paths_for_source, "scan")
                known_paths = {os.path.normcase(os.path.abspath(p)) for p in scan_paths}
                # 一次性取回全部 pHash，在内存里去重，避免逐张查库；
                # 即便跨任务并发重扫漏判，phash 唯一索引 + INSERT OR IGNORE 也能兜底
                known_phashes = await asyncio.to_thread(self.library.get_all_phash_hashes)
            pending: list = []
            added = skipped = failed = 0
            total = len(files)
            yield event.plain_result(f"开始扫描 {len(valid_dirs)} 个目录，共 {total} 张图片…")
            concurrency = max(1, SCAN_CONCURRENCY)
            processed = 0
            next_report = RESCAN_PROGRESS_EVERY
            for start in range(0, total, concurrency):
                batch_paths = files[start : start + concurrency]
                compute_paths = []
                for path in batch_paths:
                    if os.path.normcase(path) in known_paths:
                        skipped += 1
                    else:
                        compute_paths.append(path)
                if compute_paths:
                    results = await asyncio.gather(
                        *(
                            asyncio.to_thread(compute_features, path, self.hash_size)
                            for path in compute_paths
                        ),
                        return_exceptions=True,
                    )
                else:
                    results = []
                for path, result in zip(compute_paths, results, strict=True):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, BaseException):
                        failed += 1
                        logger.warning(f"扫描图片失败 {path}: {result}")
                        continue
                    feats = result
                    if is_degenerate_phash(feats.phash):
                        # 纯色/平坦图不入库（库层也会过滤），这里先数出来，
                        # 否则"新增+跳过+失败 != 总数"，用户会以为扫描漏了文件
                        skipped += 1
                        continue
                    if feats.phash in known_phashes:
                        skipped += 1
                        continue
                    try:
                        size_bytes = os.path.getsize(path)
                    except OSError as e:
                        failed += 1
                        logger.warning(f"扫描图片失败 {path}: {e}")
                        continue
                    known_phashes.add(feats.phash)
                    pending.append(
                        {
                            "phash": feats.phash,
                            "dhash": feats.dhash,
                            "ahash": feats.ahash,
                            "width": feats.width,
                            "height": feats.height,
                            "file_size": size_bytes,
                            "file_path": path,
                            "source": "scan",
                            "note": os.path.basename(path),
                        }
                    )
                    # 批量写入（单事务），避免逐条提交拖慢扫描。force 时
                    # 不能中途落库：整体替换只在最后一次性提交，中途写进去
                    # 的行会与"替换"语义冲突（半套新数据 + 半套旧数据）。
                    if not force and len(pending) >= RESCAN_BATCH_SIZE:
                        batch = list(pending)
                        pending.clear()
                        added += await asyncio.to_thread(
                            self.library.add_many, batch, reload_cache=False
                        )
                processed += len(batch_paths)
                if processed >= next_report:
                    yield event.plain_result(
                        f"进度：{processed}/{total}，新增 {added + len(pending)}，"
                        f"跳过 {skipped}，失败 {failed}"
                    )
                    next_report = processed + RESCAN_PROGRESS_EVERY
            if force:
                # 一次性原子替换：DELETE + INSERT 同一事务，失败整体回滚
                rows = list(pending)
                pending.clear()
                added = await asyncio.to_thread(self.library.replace_source, "scan", rows)
            elif pending:
                batch = list(pending)
                pending.clear()
                added += await asyncio.to_thread(self.library.add_many, batch, reload_cache=False)
            if added and not force:
                await asyncio.to_thread(self.library.reload_cache)
            # 只清理"本次成功遍历的目录"下的失效条目，目录临时掉线不清索引
            removed = await asyncio.to_thread(
                self.library.prune_scan_missing,
                set(files),
                set(scanned_dirs),
            )
            result = f"✅ 重扫完成：共 {total} 张，新增 {added}，跳过 {skipped}，失败 {failed}"
            if removed:
                result += f"，清理失效条目 {removed}"
            failed_dirs = len(valid_dirs) - len(scanned_dirs)
            if failed_dirs:
                result += f"（注意：{failed_dirs} 个目录扫描失败已跳过）"
            elif invalid > 0:
                result += f"（注意：{invalid} 个配置目录无效已忽略）"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"重扫失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 重扫失败，详情请查看机器人日志。")

    async def delete_entry(self, event: AstrMessageEvent, entry_id: int):
        """按编号删除图库条目（管理员）"""
        try:
            row = self.library.get(entry_id)
            if row is None:
                yield event.plain_result(f"未找到编号 #{entry_id} 的条目。")
                return
            await asyncio.to_thread(self.library.delete, entry_id)
        except Exception as e:
            logger.error(f"删除条目 #{entry_id} 失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 删除失败，详情请查看机器人日志。")
            return
        notes: list = []
        # best-effort 清理插件侧写入的向量点（同图同 ID，按内容指纹派生）；
        # 图床侧钩子写入的点使用图床自身的 key 约定，其清理需在图床侧完成
        if self.vector.enabled and row["phash"]:
            try:
                await self.vector.delete_point(f"phash:{row['phash']}")
                notes.append("向量点已清除")
            except Exception as e:
                logger.warning(f"删除向量点失败 #{entry_id}: {e}")
                notes.append("向量点清除失败（详情见日志）")
        if row["image_url"]:
            try:
                if await self.bed.delete_remote(row["image_url"]):
                    notes.append("远端对象已删除")
            except Exception as e:
                logger.warning(f"删除远端对象失败 #{entry_id}: {e}")
        self._remove_local_copy(row)
        # 展示只用备注：file_path / image_url 可能含服务器目录或内网地址，
        # 管理员命令的回执同样会进群聊，细节只写日志
        desc = row["note"] or "无备注"
        logger.info(
            f"/溯源删除 #{entry_id}: note={row['note'] or ''} "
            f"path={row['file_path'] or ''} url={row['image_url'] or ''}"
        )
        suffix = ("，" + "、".join(notes)) if notes else ""
        yield event.plain_result(f"✅ 已删除 #{entry_id}（{desc}）{suffix}")

    def _remove_local_copy(self, row) -> None:
        """删除登记流程产生的本地副本（仅限插件 images/ 目录内，绝不碰扫描目录的原图）。"""
        if row["source"] != "register":
            return
        self._remove_local_path_if_owned(row["file_path"] or "")

    def _remove_local_path_if_owned(self, path: str) -> None:
        """仅删除插件 images 目录内的本地副本。"""
        if not path:
            return
        real = os.path.abspath(path)
        images_root = os.path.abspath(self.bed.local_dir)
        try:
            inside = os.path.commonpath([real, images_root]) == images_root
        except ValueError:
            inside = False
        if inside and os.path.isfile(real):
            self._remove_quiet(real)

    async def _cleanup_stored(self, stored) -> None:
        """补偿清理本次上传但未落入图库的远端对象/本地副本。"""
        if stored is None:
            return
        if stored.url:
            try:
                await self.bed.delete_remote(stored.url)
            except Exception as e:
                logger.warning(f"补偿删除远端对象失败: {e}")
        if stored.file_path:
            try:
                self._remove_local_path_if_owned(stored.file_path)
            except Exception as e:
                logger.warning(f"补偿删除本地副本失败: {e}")

    async def trace_help(self, event: AstrMessageEvent):
        """图片溯源插件使用帮助"""
        yield event.plain_result(self._usage())

    def _usage(self) -> str:
        return (
            "🔍 图片溯源使用说明\n"
            "· /溯源 —— 与图片同条消息发送，或引用一张图片发送，自动在图库中寻找原图\n"
            "· /登记原图 [备注] —— 将图片作为原图登记入库（默认仅管理员）\n"
            "· /溯源状态 —— 查看图库统计\n"
            "· /溯源重扫 [force] —— 扫描本地图库目录建立索引（管理员）\n"
            "· /溯源删除 <编号> —— 删除图库条目（管理员）\n"
            "· /随机图 [目录] —— 从图床随机取一张图片并回传\n"
            "· /随机视频 [目录] —— 从图床随机取一个视频并回传\n"
            "· /签到 —— 每日签到领取一张带边框的卡片（每天首次计入天数，同日重复只换图"
            "不涨天数，可 /打卡）；checkin.mode=weibo 时改为“微博文案 + 对应图库图片”，"
            "卡片显示博主与发博日期\n"
            "· /签到文案导入 —— 管理员：把微博文案 CSV/MD 作为附件发送，导入插件数据目录\n"
            "· /原图 [文件名] —— 取原图：带文件名时直接到图床按名取（无需先回传），"
            "不带文件名取本会话最近回传的原图\n"
            "· /原图修复 —— 清理会话历史里带配文前缀（🖼️ / 🎬 / 序号）的旧记录\n"
            "· /溯源帮助 —— 显示本说明\n"
            "检索引擎：auto=向量优先（未命中/不可用自动回退哈希）；"
            "hash/vector 可在插件配置 search_engine 切换。\n"
            "提示：哈希阈值、向量阈值与模型、图床接口、随机图接口等均可在 WebUI 插件配置中调整。"
        )
