"""/溯源 主流程：哈希与向量检索、候选合并、AI 复核。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import (
    AsyncGenerator,
)

from astrbot.api import (
    logger,
)
from astrbot.api.event import (
    AstrMessageEvent,
)

try:
    from ..shared.common import (
        AI_VERIFY_MAX_CANDIDATES,
    )
    from .features import (
        compute_features,
        compute_features_with_mime,
    )
    from .image_delivery import (
        deduplicate_vector_hits,
    )
    from .vector_search import (
        VectorEngineError,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from features import (  # type: ignore[no-redef]
        compute_features,
        compute_features_with_mime,
    )
    from image_delivery import (  # type: ignore[no-redef]
        deduplicate_vector_hits,
    )
    from vector_search import (  # type: ignore[no-redef]
        VectorEngineError,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        AI_VERIFY_MAX_CANDIDATES,
    )

try:
    from .constants import (
        ResolvedImage,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        ResolvedImage,
    )


class TraceMixin:
    """/溯源 主流程：哈希与向量检索、候选合并、AI 复核。"""

    # ------------------------------------------------------------------
    # 指令：溯源
    # ------------------------------------------------------------------

    async def trace(self, event: AstrMessageEvent):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
        self._maybe_start_remote_hash_refresh()  # 缓存过期才兜底刷新（幂等）
        images = self._extract_images(event)
        if not images:
            yield event.plain_result(self._usage())
            return
        # 多图查询期间不能逐图 stop_event：AstrBot 调度器一旦看到 stopped 就会
        # 中断当前生成器，后面的图片不会被处理。整轮结束后再统一 stop。
        self._defer_event_stop(event, True)
        try:
            engine = self._engine_choice()
            budget = self.max_images_per_query
            if engine == "vector":
                if not self.vector.enabled:
                    yield event.plain_result(self.vector.config_hint())
                    return
                for seg in images[:budget]:
                    self._reset_event_sent(event)
                    async for item in self._trace_vector(event, seg):
                        yield item
                return
            if engine == "auto" and self.vector.enabled:
                for seg in images[:budget]:
                    self._reset_event_sent(event)
                    async for item in self._trace_auto(event, seg):
                        yield item
                return
            # hash（或 auto 但向量引擎未配置）
            if self._hash_pool_size() == 0:
                empty_note = (
                    "（向量引擎未启用：若图床侧已部署向量库，请检查 vector_search 配置）"
                    if not self.vector.enabled
                    else ""
                )
                yield event.plain_result(
                    "图库还是空的：可发送 /登记原图 登记原图，"
                    "或在插件配置 scan_dirs 填写图床目录后执行 /溯源重扫。" + empty_note
                )
                return
            for seg in images[:budget]:
                self._reset_event_sent(event)
                async for item in self._trace_hash(event, seg):
                    yield item
        finally:
            self._defer_event_stop(event, False)
            event.stop_event()

    # ------------------------------------------------------------------
    # 哈希引擎（原有 pHash 比对逻辑，行为保持不变）
    # ------------------------------------------------------------------

    def _hash_pool_size(self) -> int:
        """两条哈希腿的可用条数之和：本地图库 + 图床镜像索引。

        空库提示必须按这个和判定。只看本地图库会在「本地空、图床有」时误报
        「图库还是空的」，而实际上哈希腿完全可用——用户会被误导去配 scan_dirs。
        """
        return self.library.count() + self.remote_hash.count

    @staticmethod
    def _miss_message(fail_line: str, candidates: list, empty_note: str = "") -> str:
        """未命中回复的统一拼装：阈值提示 + 可选的“最接近候选”或空库说明。"""
        if candidates:
            return "\n".join([fail_line, "最接近的候选：", *candidates])
        if empty_note:
            return f"{fail_line}\n{empty_note}"
        return fail_line

    def _merged_hash_candidates(self, phash: str, want: int) -> tuple:
        """把本地图库与图床镜像两条腿的候选**按相似度合并**后返回。

        为什么要合并而不是「本地先查、没中再查图床」：两条腿各有偏科。本地图库
        是扫描来的、可能只有几百张，但能拿到本地路径参与后续压缩/回传；图床镜像
        有两万多张、覆盖面大，却只有直链。若按顺序短路，会出现「本地有一条 0.86
        的低质命中、图床有一条 0.99 的完美命中，却只报前者」——用户看到的是
        「溯源命中了」但拿到的不是最像的那张。

        返回 (hits, cand_lines)：hits 是达阈值且已按相似度降序的候选（本地优先
        同分），cand_lines 是未命中时展示的「最接近候选」文本行。

        两条腿各自独立 try：图床那条是纯本地矩阵运算、本地那条要碰 SQLite，
        任一腿出错都不该让整条检索失败。
        """
        merged: list = []
        try:
            for m in self.library.search(phash, want):
                merged.append((float(m.similarity), "local", m))
        except Exception as exc:
            logger.warning(f"本地图库哈希检索失败（继续用图床索引）: {exc}")
        if self.remote_hash_on() and not self.remote_hash.is_empty:
            try:
                for m in self.remote_hash.search(phash, want):
                    merged.append((float(m.similarity), "remote", m))
            except Exception as exc:
                logger.warning(f"图床哈希索引检索失败（继续用本地图库）: {exc}")
        # 相似度降序；同分时本地靠前（本地能给出路径，回传链路更短）
        merged.sort(key=lambda item: (-item[0], 0 if item[1] == "local" else 1))
        hits = [m for sim, _src, m in merged if sim >= self.hash_threshold]
        cand_lines = []
        if self.top_n > 0:
            for sim, _src, m in merged[: self.top_n]:
                note = self._candidate_note(m)
                cand_lines.append(
                    f"· #{m.id} 相似度 {sim * 100:.1f}%{f'（{note}）' if note else ''}"
                )
        return hits, cand_lines

    def _candidate_note(self, match) -> str:
        """候选行的括注：本地腿用 note，图床腿用文件名（note 恒为空）。"""
        note = (getattr(match, "note", "") or "").strip()
        if note:
            return note
        return (getattr(match, "file_name", "") or "").strip()

    async def _trace_hash(
        self, event: AstrMessageEvent, seg, resolved: ResolvedImage | None = None
    ) -> AsyncGenerator:
        """哈希检索；resolved 供 auto 引擎复用已解析的本地文件（含清理责任）。

        resolved 为 ResolvedImage（可能已带 features/MIME）；传了它表示调用方
        已解析过这张图，本方法不负责删除临时文件（由调用方统一清理）。
        """
        threshold = self.hash_threshold
        top_n = self.top_n
        owns_temp = resolved is None
        if resolved is None:
            resolved = await self._resolve_local(
                seg,
                probe=lambda path: compute_features_with_mime(path, self.hash_size),
            )
        local_path = resolved.path
        try:
            if not local_path:
                yield event.plain_result(resolved.message)
                return

            feats = resolved.features
            if feats is None:
                feats = await asyncio.to_thread(compute_features, local_path, self.hash_size)
            # 至少取回 5 条候选：即便 top_n 配置为 0/较小值，AI 复核也需要
            # 足够的候选池才能剔除误报
            hits, candidate_lines = await asyncio.to_thread(
                self._merged_hash_candidates, feats.phash, max(5, top_n)
            )
            if hits and self._bool_cfg("ai_verify", False):
                hits = await self._ai_verify(local_path, hits)

            if hits:
                async for result in self._hash_hit_result(event, hits[0]):
                    yield result
                return

            yield event.plain_result(
                self._miss_message(
                    f"❌ 图库中未找到相似度达标的原图（阈值 {threshold * 100:.0f}%）。",
                    candidate_lines,
                )
            )
        except Exception as e:
            logger.error(f"图片溯源处理失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 处理图片时出错，详情请查看机器人日志。")
        finally:
            if owns_temp and resolved is not None and resolved.is_tmp:
                self._remove_quiet(resolved.path)

    async def _hash_hit_result(self, event: AstrMessageEvent, best) -> AsyncGenerator:
        """把一条哈希命中渲染成回复（本地腿与图床腿共用）。

        两腿的差异只有两处：展示名（本地用备注、图床用文件名）与路径来源
        （本地文件 vs 远端直链）。其余展示与回传逻辑完全一致，故合并到一处，
        免得日后改一处漏一处。
        """
        title = f"✅ 溯源命中（相似度 {best.similarity * 100:.1f}%）#{best.id}"
        label = self._candidate_note(best)
        if label:
            title += f" · {label}"
        lines = []
        if best.width and best.height:
            lines.append(f"尺寸：{best.width}x{best.height}")
        if best.created_at:
            lines.append(f"入库时间：{best.created_at}")
        block = {"text": " · ".join(lines)}
        if best.width and best.height:
            # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
            block["width"] = best.width
            block["height"] = best.height
        if best.file_size and best.file_size > 0:
            # 入库时记录的原图体积：回传计划据此免探测原图体积
            block["size_bytes"] = int(best.file_size)
        if best.image_url:
            block["url"] = best.image_url
        elif best.file_path and os.path.isfile(best.file_path):
            block["path"] = best.file_path
        if "url" in block or "path" in block:
            async for result in self._yield_delivery(event, title, [block]):
                yield result
        else:
            # 本地路径/图床直链都可能暴露服务器目录或内网地址，只进日志
            logger.warning(
                f"哈希命中 #{best.id} 的原图不可用: "
                f"{best.file_path or best.image_url or '未知路径'}"
            )
            yield event.plain_result(f"{title}，但原图文件已不存在或无法访问（详情见机器人日志）。")

    # ------------------------------------------------------------------
    # 向量引擎
    # ------------------------------------------------------------------

    async def _vector_search_one(self, event, seg, resolved: ResolvedImage | None = None):
        """单图向量检索。返回 (kind, item)：hit / miss / nofile / error。

        hit/miss/nofile 时 item 为可直接 yield 的结果对象；error 时 item 为错误说明字符串。

        resolved 供 auto 引擎复用已解析的本地文件：传了它（ResolvedImage）就不再
        重复下载/解码，且临时文件由调用方统一清理；其中的 MIME 会直接交给
        embed_file，省掉为同一张图再取一次格式的解码。
        """
        top_n = self.top_n
        owns_temp = resolved is None
        if resolved is None:
            resolved = await self._resolve_local(seg, probe=self._probe_mime)
        try:
            if not resolved.path:
                return "nofile", event.plain_result(resolved.message)
            local_path = resolved.path
            vector = await self.vector.embed_file(local_path, mime=resolved.mime or None)
            hits = await self.vector.search(vector, limit=self.vector.top_k, with_vector=True)
            # 取回的全部达标命中（不止最高分那张），按相似度降序输出；
            # 数量上限即 top_k，需要更多就调大该配置
            good = sorted(
                (h for h in hits if h["score"] >= self.vector.threshold),
                key=lambda h: h["score"],
                reverse=True,
            )
            if good:
                good = deduplicate_vector_hits(good, self.vector.duplicate_threshold)
                return "hit", self._vector_hits_delivery(event, good)
            candidate_lines = [
                f"· {h['payload'].get('file_name') or str(h['id']).rsplit('/', 1)[-1]}"
                f" 相似度 {h['score'] * 100:.1f}%"
                for h in hits[:top_n]
            ]
            return "miss", event.plain_result(
                self._miss_message(
                    f"❌ 向量库中未找到相似度达标的原图"
                    f"（阈值 {self.vector.threshold * 100:.0f}%）。",
                    candidate_lines,
                    empty_note="（向量库中暂无图床图片，等待图床上传侧钩子入库）",
                )
            )
        except VectorEngineError as e:
            logger.warning(f"向量检索出错: {getattr(e, 'detail', e)}")
            return "error", str(e)
        except Exception:
            logger.error("向量检索异常", exc_info=True)
            return "error", "内部错误（详见机器人日志）"
        finally:
            if owns_temp and resolved.is_tmp:
                self._remove_quiet(resolved.path)

    async def _trace_vector(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """严格向量引擎：只用向量库，出错直接提示。"""
        kind, item = await self._vector_search_one(event, seg)
        if kind == "error":
            yield event.plain_result(f"⚠️ 向量检索引擎错误：{item}")
        elif kind == "hit":
            async for result in item:
                yield result
        else:
            yield item

    async def _trace_auto(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """auto 引擎：向量优先，未命中或引擎不可用时回退哈希。

        图片只解析一次：解析时用 compute_features_with_mime 同时完成格式校验与
        感知特征，向量腿拿 MIME、哈希腿拿 features，两边都不再为同一张图重复
        解码。临时文件由这里的 finally 统一清理（下游收到 resolved 时不再删除）。
        """
        resolved = await self._resolve_local(
            seg,
            probe=lambda path: compute_features_with_mime(path, self.hash_size),
        )
        if not resolved.path:
            yield event.plain_result(resolved.message)
            return
        try:
            kind, item = await self._vector_search_one(event, seg, resolved=resolved)
            if kind == "hit":
                async for result in item:
                    yield result
                return
            if kind == "nofile":
                yield item
                return
            hash_empty = self._hash_pool_size() == 0
            if kind == "miss":
                yield item
                if hash_empty:
                    yield event.plain_result("（哈希图库为空，无兜底可执行）")
                    return
                yield event.plain_result("尝试哈希比对：")
            else:  # error
                logger.info(f"auto 引擎回退哈希：{item}")
                if hash_empty:
                    yield event.plain_result(
                        "⚠️ 向量引擎不可用，且哈希图库为空，无兜底可执行。"
                        "可发送 /登记原图 或执行 /溯源重扫 建库。"
                    )
                    return
                yield event.plain_result("⚠️ 向量引擎不可用，改用哈希比对：")
            async for it in self._trace_hash(event, seg, resolved=resolved):
                yield it
        finally:
            if resolved.is_tmp:
                self._remove_quiet(resolved.path)

    async def _vector_hits_delivery(self, event: AstrMessageEvent, hits: list) -> AsyncGenerator:
        """构造向量命中的去重回传请求。

        先发一条纯文本命中提示，再把全部相似图合并进单独一条消息：第二条走
        _yield_delivery 的统一入口（一条消息含多图），逐图配文由 mark_compressed
        按各回传路径的实际压缩情形写入，因此不再重复命中提示标题。
        """
        total_hits = sum(int(hit.get("duplicate_count") or 1) for hit in hits)
        duplicate_count = total_hits - len(hits)
        multi = len(hits) > 1
        if duplicate_count:
            logger.info(
                "向量命中去重：原始 %d 张，合并重复 %d 张，回传 %d 张",
                total_hits,
                duplicate_count,
                len(hits),
            )
        for hit in hits:
            duplicates = hit.get("duplicates") or []
            if not duplicates:
                continue
            payload = hit.get("payload") or {}
            display_name = payload.get("file_name") or str(hit.get("id"))
            duplicate_ids = "、".join(str(item.get("id")) for item in duplicates)
            logger.info(
                "向量命中去重：保留 %s（%s），合并重复 %d 张（%s）",
                display_name,
                hit.get("id"),
                len(duplicates),
                duplicate_ids,
            )

        if multi:
            header = f"✅ 向量检索命中 {len(hits)} 张（按相似度降序），图片发送可能有延迟"
        else:
            header = f"✅ 向量检索命中（相似度 {hits[0]['score'] * 100:.1f}%），图片发送可能有延迟"

        blocks = []
        for idx, hit in enumerate(hits, 1):
            payload = hit.get("payload") or {}
            file_name = payload.get("file_name") or str(hit.get("id")).rsplit("/", 1)[-1]
            fields = []
            if multi:
                fields.append(f"相似度 {hit['score'] * 100:.1f}%")
            width, height = payload.get("width"), payload.get("height")
            if width and height:
                fields.append(f"{width}x{height}")
            if payload.get("created_at"):
                fields.append(str(payload["created_at"]))
            prefix = f"{idx}." if multi else ""
            block = {
                "prefix": prefix,
                "file_name": str(file_name or ""),
                "fields": fields,
            }
            if width and height:
                # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
                block["width"] = width
                block["height"] = height
            size_bytes = self._known_bytes(payload)
            if size_bytes:
                # 图床侧钩子（img-indexer）入库时已记录原图体积，直接用：
                # 回传计划因此不必再探测原图尺寸，省一次往返与一次图床处理
                block["size_bytes"] = size_bytes
            image_url = str(payload.get("image_url") or "")
            if image_url:
                block["url"] = image_url
            else:
                block["fields"].append("该条目没有可用直链，可能已被删除")
            blocks.append(block)
        prompt_via_onebot = False
        prompt_task = None
        if self._can_send_via_onebot(event):
            # 提示与图片并行发出：提示只走一次 call_action，串行 await 会让图片
            # 晚一个完整往返才起步。两条消息都在同一条 OneBot 连接上按顺序下发，
            # 提示先入队，正常情况下仍先到达。
            prompt_task = asyncio.create_task(self._send_prompt(event, header))
        else:
            yield event.plain_result(header)
        try:
            async for result in self._yield_delivery(event, "", blocks, mark_compressed=True):
                yield result
            if prompt_task is not None:
                # 只为 stop_event 决策等待提示结果；图片可能已经交付，不再回滚。
                prompt_via_onebot = await prompt_task
                prompt_task = None
                if not prompt_via_onebot:
                    # 协议端明确拒绝提示（FAILED）：图片可能已发，但标题不能丢，
                    # 改走标准消息链补发一次纯文本
                    yield event.plain_result(header)
        finally:
            # 投递层异常或消费者提前退出时回收提示任务，避免留下悬挂任务。
            if prompt_task is not None:
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
        # 提示用 OneBot 原生发出（图片那条未必）时，仍按旧语义停事件；若图片
        # 那条也走了 OneBot，_yield_delivery 已停过，这里重复调用无副作用。
        # 多图溯源期间由 trace 统一延迟 stop，否则管道会在首图后中断生成器。
        # 放在 yield 之后：第二条消息已经交付，停事件不会把它吞掉。
        if prompt_via_onebot and not self._defer_event_stop(event):
            event.stop_event()

    @staticmethod
    def _is_negative_answer(text: str) -> bool:
        """判断视觉模型的回答是否定（仅"否"开头不足以覆盖常见措辞）。"""
        answer = (text or "").strip().lower()
        if not answer:
            return False
        for prefix in ("否", "不是", "不 是", "不同", "不一致", "no", "not", "nope", "different"):
            if answer.startswith(prefix):
                return True
        return False

    async def _ai_verify(self, query_path: str, hits: list) -> list:
        """用当前会话的视觉大模型复核候选图是否为同一张图。

        任一环节失败都静默降级为纯哈希结果，不影响正常使用。候选之间彼此独立，
        因此并发送审——此前串行等待，多候选时复核耗时是单次推理的 N 倍。
        """
        provider = None
        try:
            # get_using_provider 已被官方弃用，优先使用异步版；旧版本回退同步 API。
            get_provider = getattr(self.context, "get_using_provider_async", None)
            if get_provider is not None:
                provider = await get_provider()
            else:
                provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None or not hasattr(provider, "text_chat"):
            logger.info("未找到可用的 LLM 提供商，跳过 AI 复核。")
            return hits

        async def verify(cand):
            """返回保留的候选；被否决时返回 None。"""
            cand_path = ""
            if cand.file_path and os.path.isfile(cand.file_path):
                cand_path = cand.file_path
            if not cand_path:
                return cand  # 候选无本地文件，无法复核，保留哈希结论
            try:
                resp = await provider.text_chat(
                    prompt=(
                        "请判断这两张图片是否为同一张图片（允许压缩、缩放、裁剪、"
                        "加水印等差异）。只回答：是、否、不确定。"
                    ),
                    image_urls=[cand_path, query_path],
                )
                answer = (getattr(resp, "completion_text", "") or "").strip()
                logger.debug(f"AI 复核 #{cand.id} -> {answer}")
                return None if self._is_negative_answer(answer) else cand
            except Exception as e:
                logger.warning(f"AI 复核失败，保留候选 #{cand.id}: {e}")
                return cand

        results = await asyncio.gather(*(verify(cand) for cand in hits[:AI_VERIFY_MAX_CANDIDATES]))
        # 不能回退为未复核的 hits：候选被全部否决时应当报告未命中
        return [cand for cand in results if cand is not None]
