"""缩略图压缩、发送目标选择与 OneBot / NapCat 投递。"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)
from urllib.parse import (
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
        COMPRESS_CONCURRENCY,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        as_float,
        as_int,
        bool_or,
        read_bytes,
        truthy,
    )
    from .checkin_card import (
        QuoteLibrary,
    )
    from .features import (
        image_bytes_ok,
        image_file_ok,
    )
    from .image_delivery import (
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_original_url,
        build_scaled_url,
        classify_send_error,
        compression_evidence,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        send_diagnosis,
        sniff_image_format,
    )
    from .random_media import (
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        clean_display_name,
        is_thumbnail_url,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from checkin_card import (  # type: ignore[no-redef]
        QuoteLibrary,
    )
    from features import (  # type: ignore[no-redef]
        image_bytes_ok,
        image_file_ok,
    )
    from image_delivery import (  # type: ignore[no-redef]
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_original_url,
        build_scaled_url,
        classify_send_error,
        compression_evidence,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        send_diagnosis,
        sniff_image_format,
    )
    from random_media import (  # type: ignore[no-redef]
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        clean_display_name,
        is_thumbnail_url,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        COMPRESS_CONCURRENCY,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        as_float,
        as_int,
        bool_or,
        read_bytes,
        truthy,
    )

try:
    from .constants import (
        ONEBOT_SEND_TIMEOUT,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        ONEBOT_SEND_TIMEOUT,
    )


class DeliveryMixin:
    """缩略图压缩、发送目标选择与 OneBot / NapCat 投递。"""

    def _delivery_config(self) -> tuple[str, int, int, str]:
        return delivery_settings(self._napcat_delivery_cfg())

    def _random_settings(self) -> dict:
        """归一化随机图配置组（random_media）。

        与其它配置组同源：数值用 common 的容错解析、布尔用真值表、空串按未
        配置处理。缺 base_url 时不在这里拦截，交由 RandomMediaClient 抛
        RandomMediaError，使“未配置”提示集中在一处。

        default_dir 支持单个目录或用 `,` 分隔的目录池：图床的 /random 只收单一
        dir，池用于「不指定目录时尽量覆盖整库」——见 pick_default_directory。

        lan_base_url（内网直连）非空时：随机接口改走内网源，其主机进入本实例
        的私网白名单（仅豁免该主机的私网地址拒绝，其它 SSRF 校验照旧）；
        每次归一化都重写本实例白名单，配置改动后热重载即可生效。
        """
        raw = self._dict_cfg("random_media")
        lan_base = str(raw.get("lan_base_url") or "").strip()
        lan_host = ""
        if lan_base:
            parsed_lan = urlsplit(lan_base)
            if parsed_lan.scheme in ("http", "https") and parsed_lan.netloc:
                lan_host = (parsed_lan.hostname or "").lower()
            else:
                logger.warning(
                    f"random_media.lan_base_url 不是有效的 http(s) 地址，已忽略: {lan_base}"
                )
                lan_base = ""
        # 不能 clear() 后再 add()：中间存在"白名单为空"的窗口，并发在途请求
        # 正好在窗口里做私网校验时，合法的内网源会被误判成 SSRF 而拒绝。
        # 改成先并集再剔除陈旧项——任一时刻集合都是「旧 ∪ 新」，只会短暂
        # 多放行一个即将被移除的主机，不会误拒新主机。
        desired = {lan_host} if lan_host else set()
        hosts = self._allowed_private_hosts
        hosts.update(desired)
        for stale in tuple(hosts - desired):
            hosts.discard(stale)
        timeout = as_float(raw.get("timeout"), 10.0)
        if timeout <= 0:
            # 0/负数会让请求立即超时，回退默认值
            timeout = 10.0
        return {
            "base_url": str(raw.get("base_url") or "").strip(),
            "lan_base_url": lan_base,
            "api_endpoint": str(raw.get("api_endpoint") or RANDOM_ENDPOINT_DEFAULT).strip()
            or RANDOM_ENDPOINT_DEFAULT,
            "api_token": str(raw.get("api_token") or "").strip(),
            "default_dir": str(raw.get("default_dir") or "").strip(),
            "timeout": timeout,
            "retry_count": bounded_int(raw.get("retry_count"), 3, 0, 10),
            "show_file_info": bool_or(raw.get("show_file_info"), True),
            "enable_llm": bool_or(raw.get("enable_llm"), True),
            "send_thumbnail": bool_or(raw.get("send_thumbnail"), True),
        }

    @staticmethod
    def _lan_url_hosts(settings: dict) -> tuple[str, ...]:
        """内网直连模式下允许改写源的主机集合：内网 base 与公网 base 的主机名。

        图床自己产出的直链只可能落在这两个主机上（/random 按请求 Host 生成、
        Qdrant 登记的是公网域名），命中才改写成内网源，第三方链接不受影响。
        """
        lan = str((settings or {}).get("lan_base_url") or "").strip()
        if not lan:
            return ()
        hosts = [urlsplit(lan).hostname or ""]
        base_host = urlsplit(str((settings or {}).get("base_url") or "")).hostname
        if base_host:
            hosts.append(base_host)
        return tuple(host for host in hosts if host)

    @staticmethod
    def _delivery_target(event: AstrMessageEvent) -> DeliveryTarget:
        """当前事件的推送目标画像——平台判定的唯一入口。

        此前「能不能 OneBot 直发」「媒体是否经第三方转存」「能不能渲染原生 At」
        是三个各自去问 get_platform_name() 的静态方法，同一次推送里被反复问、
        也没有任何地方能一眼看出这些能力之间的约束关系。现在统一由这里构造，
        各分支只读字段（见 image_delivery.DeliveryTarget）。
        """
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        return delivery_target(platform, has_bot=getattr(event, "bot", None) is not None)

    def _random_client(self) -> RandomMediaClient:
        """惰性构造随机图客户端；配置在插件加载时读入，改动后重载生效。"""
        if self._random_media_client is None:
            self._random_media_client = RandomMediaClient(self._random_settings(), self._get_http)
        return self._random_media_client

    def _checkin_settings(self) -> dict:
        """归一化每日签到配置（checkin 组）。

        所有取值都做范围收敛：面板被手改成离谱数字（如边框 9999、时区 99）时，
        按边界值生效而不是把非法值传进合成/日期计算。
        """
        raw = self._dict_cfg("checkin")
        headers = raw.get("quote_api_headers")
        source = str(raw.get("quote_source") or "").strip().lower()
        mode = str(raw.get("mode") or "").strip().lower()
        return {
            "enabled": bool_or(raw.get("enabled"), True),
            "mode": "weibo" if mode == "weibo" else "quote",
            "render_mode": "simple"
            if str(raw.get("render_mode") or "").strip().lower() == "simple"
            else "card",
            "border_px": max(0, min(200, as_int(raw.get("border_px"), 0))),
            "utc_offset_hours": max(-12, min(14, as_int(raw.get("utc_offset_hours"), 8))),
            "quote_source": source if source in {"api", "mixed"} else "builtin",
            "quote_local_text": str(raw.get("quote_local_text") or ""),
            "quote_api_url": str(raw.get("quote_api_url") or "").strip(),
            "quote_json_path": str(raw.get("quote_json_path") or "").strip(),
            "quote_api_headers": headers if isinstance(headers, dict) else {},
            "quote_timeout": max(1, min(30, as_int(raw.get("quote_timeout"), 5))),
            "font_path": str(raw.get("font_path") or "").strip(),
        }

    @staticmethod
    def _checkin_today(offset_hours: int) -> date:
        """按配置的 UTC 偏移算出「今天」，与服务器本地时区无关。"""
        return datetime.now(timezone(timedelta(hours=offset_hours))).date()

    def _quote_library(self) -> QuoteLibrary:
        """惰性构造语句库；文本框留空时读取 quotes.txt，缺失则用内置语句初始化。"""
        if self._quote_library_cache is None:
            settings = self._checkin_settings()
            self._quote_library_cache = QuoteLibrary(
                os.path.join(self.data_dir, "quotes.txt"),
                local_text=settings["quote_local_text"],
                source=settings["quote_source"],
                api_url=settings["quote_api_url"],
                json_path=settings["quote_json_path"],
                headers=settings["quote_api_headers"],
                timeout=settings["quote_timeout"],
                http_getter=self._get_http,
            )
        return self._quote_library_cache

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """会话唯一标识，用于隔离 /原图 的图片历史。"""
        return str(getattr(event, "unified_msg_origin", None) or "default")

    def _remember_blocks(self, event: AstrMessageEvent, blocks: list[dict]) -> None:
        """把本次回传的原图直链记入会话历史，供 /原图 找回。

        /溯源 的哈希与向量两条路径、以及 /随机图 现在都经 _yield_delivery
        回传（向量命中额外带 mark_compressed=True 生成逐图配文），因此只需在
        这一个入口收口，避免在各命令里分别维护历史而漏记。

        指向缩略图的直链**一律不入历史**：历史是 /原图 的取值来源，一旦记进
        缩略图，此后 /原图 只能拿回那张 720px 小图。/随机图 的发送路径本就
        通过 remember_blocks 传入原图（见 _random_media_delivery），这里是
        第二道闸，保证任何调用点都不会把替身写进历史。
        """
        session = self._session_key(event)
        for block in blocks:
            url = str(block.get("url") or "").strip()
            if not url:
                continue
            if is_thumbnail_url(url):
                logger.debug(f"跳过缩略图直链，不计入 /原图 历史: {url}")
                continue
            self.history.remember(
                session, url, display_name=clean_display_name(block.get("file_name"))
            )

    def _can_send_via_onebot(self, event: AstrMessageEvent) -> bool:
        """能否用 OneBot 原生接口直发（图片字节/URL 段、纯文本）。

        保留为薄封装：判定已收口到 _delivery_target（aiocqhttp 且 bot 就绪）。
        """
        return self._delivery_target(event).direct_send

    def _mention_prefix(self, event: AstrMessageEvent) -> list:
        """回传链开头的「@发送者」段；仅能渲染原生 At 的平台启用。

        qq_official 等平台没有原生 At 段：适配器会把 At 转成平台标记原文
        （`<qqbot-at-user id="..."/>`）拼进纯文本，而官方群消息的纯文本并不
        渲染该标记（仅 markdown 消息类型生效），用户看到的就是一串“代码”
        糊在文案前。故 @ 前缀只给能原生渲染它的 aiocqhttp，其余平台不加。
        """
        if not self._delivery_target(event).native_mention:
            return []
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return []
        return [Comp.At(qq=sender)] if sender else []

    @staticmethod
    def _event_already_sent(event: AstrMessageEvent) -> bool:
        """本次事件是否已经投递过图片（事件级去重盾）。"""
        return bool(getattr(event, "_image_trace_sent", False))

    @staticmethod
    def _reset_event_sent(event: AstrMessageEvent) -> None:
        """复位投递标记，供多图查询在两轮不同图片之间使用。

        去重盾的职责是阻止**同一批图片**在回退路径上重复发送，而不是限制
        整个事件只能发一次；/溯源 一条消息可带多张图（max_images_per_query），
        每张图都是独立查询，必须在下一轮开始前复位。
        """
        try:
            event._image_trace_sent = False
        except AttributeError:  # 少数事件实现带 __slots__，标记失败不影响发送
            logger.debug("事件对象不支持动态属性，跳过已发送标记复位")

    @staticmethod
    def _mark_event_sent(event: AstrMessageEvent) -> None:
        """标记本次事件已投递图片，禁止后续任何回退路径再发一次。"""
        try:
            event._image_trace_sent = True
        except AttributeError:  # 少数事件实现带 __slots__，标记失败不影响发送
            logger.debug("事件对象不支持动态属性，跳过已发送标记")

    @staticmethod
    def _defer_event_stop(event: AstrMessageEvent, defer: bool | None = None) -> bool:
        """多图溯源期间延迟 stop_event，避免管道在首图后中断生成器。

        AstrBot 的调度器一旦发现 event 已 stopped，会停止继续消费当前处理器
        的异步生成器；而 /溯源 需要按 max_images_per_query 依次处理多张图。
        故在整轮循环内置位，循环结束由 trace 统一 stop。
        """
        if defer is not None:
            try:
                event._image_trace_defer_stop = bool(defer)
            except AttributeError:
                logger.debug("事件对象不支持动态属性，跳过延迟 stop 标记")
            return bool(defer)
        return bool(getattr(event, "_image_trace_defer_stop", False))

    @staticmethod
    def _vector_caption(block: dict, evidence: CompressionEvidence) -> str:
        """按压缩证据拼配文。

        只有拿到证据才写「已压缩」：此前按「URL 被改写」推断压缩，而图床在
        图片不超 max_side、格式不可处理或图像处理不可用时会按 fallback=original
        原样返回，于是出现「文字说已压缩、用户收到的却是原图」。UNKNOWN 只给
        一句 /原图 用法提示，ORIGINAL 连提示都不给——发的本来就是原图。
        """
        fields = []
        if evidence is CompressionEvidence.COMPRESSED:
            fields.append("已压缩，可发送 /原图 获取原图")
        elif evidence is CompressionEvidence.UNKNOWN:
            fields.append("可发送 /原图 获取原图")
        file_name = (
            clean_display_name(block.get("file_name")) or str(block.get("file_name") or "").strip()
        )
        if file_name:
            fields.append(file_name)
        fields.extend(str(field) for field in block.get("fields", []) if field)
        prefix = str(block.get("prefix") or "").strip()
        caption = " · ".join(fields)
        return f"{prefix} {caption}".strip() if prefix else caption

    async def _send_via_onebot(
        self, event: AstrMessageEvent, header: str, blocks: list[dict], payloads: list[str | bytes]
    ) -> SendOutcome:
        """通过 OneBot 原生接口直发消息，消息段按「文字集中 → 图片集中」排列。

        文字在前、图片在后是**必须**的，不是排版偏好：QQ 客户端会把
        「text 段 + 紧邻其后的 image 段」合并渲染成一个图文混排卡片，卡片内的
        图片按行内富文本插图处理，**一律裁成正方形缩略图**（无论原图多高）。
        此前按「标题 →（配文 → 图）…」图文交替排列，多图时每一张图都恰好紧跟在
        一段文字后面，于是几张图全是正方形缩略图；只有排在最后、后面没有更多
        文字重新分组的那张偶尔能拿到正确尺寸——线上报的「前三张方、第四张正常」
        就是这个渲染分组的结果。把全部 text 段提到最前、image 段连续排在末尾后，
        QQ 会把连续图片段按普通大图渲染，宽高比得以保留。

        配文因此不再紧贴各自的图片，改为在配文里前置序号（block["prefix"]，如
        「1.」「2.」），与图片的先后顺序一一对应，用户仍能分辨哪句配文属于哪张图。

        payloads 与 blocks 中的有图 block 同序一一对应，元素为 URL 字符串（原样
        作 data.file）或图片字节（编码为 base64:// 段，压缩结果直达 QQ、不经
        协议端二次下载）。调用方（_yield_delivery 的两个直发分支、
        _send_prompt）已保证数量相等，故这里按遍历到的有图 block 依次取用，
        无需再做长度校验。

        image 段带 OneBot 标准的 `timeout` 字段（秒）：URL 段由协议端自行下载，
        大图 + 多图时下载耗时会让 NapCat 的 sendMsg 回调窗口吃紧（实测超时形态
        见 classify_send_error 注释）。显式放宽该窗口可减少「结果未知」。base64
        段不经下载，带该字段无副作用。

        返回三态而非布尔：只有协议端明确回报失败（FAILED）才允许换通道重发。
        超时等 UNKNOWN 情形请求可能已经送达，调用方必须先看返回值再决定是否
        回退，否则同一张图会被发两遍。
        """
        message: list[dict] = []
        sender_id = event.get_sender_id()
        if sender_id:
            message.append({"type": "at", "data": {"qq": str(sender_id)}})

        # 1) 全部文字段集中在最前：标题一段，随后每张图各一段配文。
        #    连续 text 段之间不会再插入 image 段，QQ 不会把它们拆成图文卡片。
        text_lines: list[str] = []
        if header:
            text_lines.append(header)
        for block in blocks:
            text = str(block.get("text") or "").strip()
            if text:
                text_lines.append(text)
        if text_lines:
            # 单段承载全部文字：段数越少，QQ 越不容易按「文字+图」重新分组
            message.append({"type": "text", "data": {"text": "\n".join(text_lines)}})

        # 2) 全部图片段连续排在末尾：连续 image 段按普通大图渲染，保留宽高比。
        image_timeout = self._onebot_image_timeout()
        pending_payloads = iter(payloads)
        for block in blocks:
            if not (block.get("url") or block.get("path")):
                continue
            payload = next(pending_payloads)
            if isinstance(payload, bytes):
                payload = "base64://" + base64.b64encode(payload).decode()
            message.append(
                {
                    "type": "image",
                    "data": {"file": payload, "timeout": image_timeout},
                }
            )

        params: dict = {"message": message}
        group_id = event.get_group_id()
        if group_id:
            action = "send_group_msg"
            params["group_id"] = int(group_id) if str(group_id).isdigit() else group_id
        else:
            action = "send_private_msg"
            user_id = sender_id
            params["user_id"] = int(user_id) if str(user_id).isdigit() else user_id
        self_id = getattr(event.message_obj, "self_id", None)
        if self_id:
            params["self_id"] = self_id

        image_count = sum(1 for seg in message if seg["type"] == "image")
        try:
            # 协议端不回执时不能让事件协程永久挂起：超时按 UNKNOWN 处理（不重发）
            await asyncio.wait_for(
                event.bot.call_action(action, **params), timeout=ONEBOT_SEND_TIMEOUT
            )
        except Exception as exc:
            outcome = classify_send_error(exc)
            # 日志按成因给结论：不再只写「结果未知」，而是讲清「请求已发出、
            # 未拿到回执、因此按不重发处理」以及成因与排查方向。三态判定与
            # 回退决策一字未改，这里只换表达。
            logger.warning(f"OneBot 图片直发（{action}，{image_count} 张）：{send_diagnosis(exc)}")
            logger.debug(
                f"OneBot 直发异常原始信息: {type(exc).__name__} -> {outcome.value}; "
                f"retcode={getattr(exc, 'retcode', None)}; echo={getattr(exc, 'echo', None)}; {exc}"
            )
            return outcome
        logger.info(f"OneBot 图片直发成功: {action}（{image_count} 张）")
        return SendOutcome.SENT

    async def _send_prompt(self, event: AstrMessageEvent, text: str) -> bool:
        """发送一条纯文本提示，返回是否已由 OneBot 原生处理。

        OneBot（aiocqhttp）下走原生 call_action 单发文本，命中提示就不会跟
        图片挤在同一条消息里；其余平台返回 False，由调用方 yield plain_result
        兜底。结果未知（超时）也按「已处理」返回：提示重复一遍无伤大雅，但
        让它再走一次消息链毫无意义。
        """
        if self._can_send_via_onebot(event):
            return await self._send_via_onebot(event, text, [], []) is not SendOutcome.FAILED
        return False

    def _mark_compressed_captions(
        self, blocks: list[dict], evidences: list[CompressionEvidence]
    ) -> None:
        """为 mark_compressed 路径写入逐图配文。

        evidences 与 blocks 中的有图 block 一一对应（同序于 URL 直传计划或
        _prepare_delivery_blocks 的输出）；无图 block（例如没有可用直链的条目）
        按 UNKNOWN 处理，但它也要拿到配文，才不会在合并消息里消失。仅在
        mark_compressed 为真时调用，默认路径不触碰 block["text"]。
        """
        index = 0
        for block in blocks:
            if block.get("url") or block.get("path"):
                evidence = evidences[index]
                index += 1
            else:
                evidence = CompressionEvidence.UNKNOWN
            block["text"] = self._vector_caption(block, evidence)

    async def _load_delivery_bytes(self, block: dict, *, validate: bool = True) -> bytes | None:
        """取回传要用的图片字节。

        validate=False 供本地压缩路径使用：压缩本身就要完整解码一次，再提前
        校验等于同一次解码做两遍（实测 4000x3000 JPEG 白花 78ms、外加一次
        全尺寸位图内存）。压缩失败时调用方再用 image_bytes_ok 判定「图本身
        坏」还是「这个格式不适合重编码」，坏图仍会被拦下。
        """
        url = str(block.get("url") or "")
        path = str(block.get("path") or "")
        try:
            if url:
                data = await self._download_bytes(url)
                if validate and not await asyncio.to_thread(image_bytes_ok, data):
                    return None
                return data
            if not path:
                return None
            if validate and not await asyncio.to_thread(image_file_ok, path):
                return None
            return await asyncio.to_thread(read_bytes, path)
        except Exception as exc:
            logger.warning(f"读取回传图片失败: {exc}")
            return None

    async def _prepare_delivery_blocks(
        self, blocks: list[dict], *, compress: bool
    ) -> list[tuple[bytes, bool]] | None:
        """准备要直发的字节，逐图压缩在并发上限内并行执行。

        顺序必须与传入的 image_blocks 一致（调用方按下标取用），因此用
        gather 收集结果而不是 as_completed。任一张失败即整体返回 None，
        调用方据此改走 URL 链——与逐张串行时的语义一致。
        """
        mode, max_side, quality, _ = self._delivery_config()
        target_bytes, use_webp = self._encode_options()
        semaphore = asyncio.Semaphore(COMPRESS_CONCURRENCY)

        async def prepare_one(block: dict) -> tuple[bytes, bool] | None:
            async with semaphore:
                data = await self._load_delivery_bytes(block, validate=not compress)
                if data is None:
                    return None
                if not compress or mode == "url":
                    return data, False
                result = await asyncio.to_thread(
                    prepare_image_bytes,
                    data,
                    max_side,
                    quality,
                    target_bytes=target_bytes,
                    use_webp=use_webp,
                )
                if result is not None:
                    return result
                if await asyncio.to_thread(image_bytes_ok, data):
                    # 压缩失败但图片本身可用（格式不支持重编码等）：回退原字节
                    logger.warning(
                        f"图片本地压缩失败，回退原字节: 格式={sniff_image_format(data) or '未知'}, "
                        f"大小={len(data) / 1024:.0f}KB"
                    )
                    return data, False
                if sniff_image_format(data) == "AVIF":
                    # Pillow<11.3 无 AVIF 解码器：探测、下载后既压不了也验不了，
                    # 只能让协议端自己拉 URL。给出可操作的提示而不是笼统的失败。
                    logger.warning(
                        f"AVIF 图片当前环境无法解码（Pillow 版本过低），改用 URL 发送: "
                        f"大小={len(data) / 1024:.0f}KB；升级 Pillow>=11.3 可启用 AVIF 本地压缩"
                    )
                    return None
                logger.warning(
                    f"回退图片无法解码，改用 URL 发送: 格式={sniff_image_format(data) or '未知'}, "
                    f"大小={len(data) / 1024:.0f}KB"
                )
                return None

        prepared = await asyncio.gather(
            *(prepare_one(block) for block in blocks if block.get("url") or block.get("path"))
        )
        if any(item is None for item in prepared):
            return None
        return list(prepared)

    async def _local_compress_delivery(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        image_blocks: list[dict],
        *,
        mark_compressed: bool,
    ) -> SendOutcome | None:
        """auto / bytes 模式下 OneBot 的混合载荷直发。

        逐图决定 URL 段还是 base64 段：已知小图（≤COMPRESS_MIN_BYTES 且协议端
        能解析宽高）跳过下载、直发原图 URL——本地压缩本就会跳过这类图，下载再
        base64 回发纯属浪费一次往返；其余并发下载+压缩后内联 base64，压缩结果
        不经协议端二次下载，100% 到达 QQ（QQ 协议端会剥离 URL 查询参数，缩放
        URL 直传拿回的常是原图）。未压缩且超内联上限的巨型图（动图等）退回
        URL 段，避免单条消息过大。

        返回 None 表示准备失败（任一张下载/解码失败），调用方整批改走 URL 链；
        否则返回 _send_via_onebot 的三态结果。
        """
        prepare_blocks = [block for block in image_blocks if not prefer_url_direct(block)]
        prepared = await self._prepare_delivery_blocks(prepare_blocks, compress=True)
        if prepared is None:
            return None

        payloads: list[str | bytes] = []
        evidences: list[CompressionEvidence] = []
        pending = iter(prepared)
        for block in image_blocks:
            url = str(block.get("url") or "")
            if prefer_url_direct(block):
                payloads.append(build_original_url(url) or url)
                evidences.append(CompressionEvidence.ORIGINAL)
                continue
            data, compressed = next(pending)
            if prefer_inline_bytes(len(data), compressed, url):
                payloads.append(data)
            else:
                payloads.append(build_original_url(url) or url)
            evidences.append(
                CompressionEvidence.COMPRESSED if compressed else CompressionEvidence.ORIGINAL
            )

        if mark_compressed:
            self._mark_compressed_captions(blocks, evidences)
        elif any(evidence is CompressionEvidence.COMPRESSED for evidence in evidences):
            header += "（已压缩）"
        return await self._send_via_onebot(event, header, blocks, payloads)

    def _encode_options(self) -> tuple[int, bool]:
        """本地压缩进阶选项：目标体积上限（字节，0 表示不限制）与 WebP 输出。"""
        raw = self._napcat_delivery_cfg()
        target_kb = bounded_int(raw.get("target_kb"), 0, 0, 10240)
        return target_kb * 1024, truthy(raw.get("webp"))

    @staticmethod
    def _known_bytes(block: dict) -> int:
        """命中条目里记录的原图体积（字节）；未知或非法一律返回 0。"""
        try:
            value = int(block.get("size_bytes") or 0)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    async def _url_delivery_plan(
        self,
        image_blocks: list[dict],
        max_side: int,
        mode: str,
        quality: int = 85,
        scaled_url_style: str = "query",
    ) -> tuple[list[str], list[CompressionEvidence]]:
        """决定每张图 URL 直传实际发哪个 URL，并给出压缩证据，两者同序对应。

        不再无条件改写 URL：只有最长边确实超过 max_side（或尺寸未知）时才追加
        图床缩放参数——不超过 max_side 的图，图床按 fallback=original 原样返回，
        追加参数只是白白触发一次处理。开启 verify_scaled 时进一步实测缩放版是否
        真的更小，这也是「已压缩」文案唯一的一手依据；实测不出来就按 UNKNOWN
        处理，配文宁可不写，也不能出现「文字说压缩、收到的是原图」。

        探测成本被压到最低：所有探测并发执行且有总预算，命中条目自带原图体积
        （size_bytes / file_size）时不必再探原图；图床明确拒绝缩放请求后进入
        冷却期，冷却期内直接跳过探测。
        """
        urls: list[str] = []
        sizes: list[tuple] = []
        jobs: list[tuple[str, str, int] | None] = []
        verify = (
            mode == "url-scaled" and self._verify_scaled() and not self._scaled_verify_cooling()
        )
        for block in image_blocks:
            original = build_original_url(str(block.get("url") or ""))
            size = (block.get("width"), block.get("height"))
            sizes.append(size)
            if mode != "url-scaled" or not original:
                urls.append(original or str(block.get("url") or ""))
                jobs.append(None)
                continue
            scaled = build_scaled_url(original, max_side, quality=quality, style=scaled_url_style)
            if is_scaling_needed(size, max_side) is False or scaled == original:
                # 尺寸证明不会被缩放，或这条直链本来就带不了缩放参数
                urls.append(original)
                jobs.append(None)
                continue
            urls.append(scaled)
            jobs.append((original, scaled, self._known_bytes(block)) if verify else None)

        verified = await self._verify_scaled_batch(jobs) if any(jobs) else {}
        evidences = [
            compression_evidence(sizes[index], max_side, verified_smaller=verified.get(index))
            for index in range(len(image_blocks))
        ]
        return urls, evidences

    async def _verify_scaled_batch(
        self, jobs: list[tuple[str, str, int] | None]
    ) -> dict[int, bool | None]:
        """并行实测「缩放版是否真的更小」，返回 {下标: 是否更小或 None}。

        已知原图体积（命中条目里的 size_bytes / file_size）时只探缩放版——
        少一次请求也少一次图床处理；未知时才把原图与缩放版一起并发探。
        超出总预算仍未返回的探测一律不判定（配文按「说不准」处理），
        绝不让探测无限期拖住发送。
        """
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
        cache: dict = {}

        async def run(index: int, job: tuple[str, str, int]) -> tuple[int, bool | None]:
            original, scaled, known = job
            async with semaphore:
                if known:
                    _, scaled_size = await self._probe_url(scaled, cache, allow_range=False)
                    return index, (None if scaled_size is None else scaled_size < known)
                (_, original_size), (_, scaled_size) = await asyncio.gather(
                    self._probe_url(original, cache),
                    self._probe_url(scaled, cache, allow_range=False),
                )
                if original_size is None or scaled_size is None:
                    return index, None
                return index, scaled_size < original_size

        tasks = [
            asyncio.create_task(run(index, job))
            for index, job in enumerate(jobs)
            if job is not None
        ]
        if not tasks:
            return {}
        done, pending = await asyncio.wait(tasks, timeout=PLAN_TOTAL_TIMEOUT)
        for task in pending:
            task.cancel()
        if pending:
            # 取消后回收，避免留下「已取消但仍挂着」的任务对象
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning(
                f"探测超出 {PLAN_TOTAL_TIMEOUT}s 预算，{len(pending)} 张图的压缩标识按"
                "「无法判定」处理（不阻塞图片发送）"
            )
        result: dict[int, bool | None] = {}
        for task in done:
            try:
                index, value = task.result()
            except Exception as exc:  # 单张探测失败不影响其余判定
                logger.debug(f"缩放实测失败: {exc}")
                continue
            result[index] = value
        return result

    async def _yield_delivery(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mode_override: str | None = None,
        allow_local_fallback: bool = True,
        mark_compressed: bool = False,
        remember_blocks: list[dict] | None = None,
    ):
        """统一回传入口：按通道选主路径，失败后逐级回退。

        mode 取统一词表（auto / native / bytes / url / url-scaled，两平台同名
        同义，见 image_delivery.normalize_delivery_mode）：
        - auto（默认）：交给插件自动决定。OneBot 下压缩字节经 base64 段直发
          （混合载荷，已知小图跳过下载直发 URL），失败改走标准消息链 URL；
          qq_official 下按体积分流到 native/url/bytes。
        - bytes：一律本机准备好字节再上传（OneBot 压缩后直发；qq_official 分片）。
        - url / url-scaled：URL 直传优先（原图 / 图床缩放版），本地压缩只作为
          失败回退。
        - native：不加工，直接交给适配器原样发送（排障对照用）。

        OneBot 侧仍默认压缩：QQ 协议端（NapCat 等）会剥离 URL 查询参数，缩放
        URL 直传拿回的常是原图，本地压缩是唯一能保证压缩 100% 生效的路径。

        mode_override 供 /原图 强制走 url、无视全局通道；
        allow_local_fallback=False 时彻底不进入下载字节/本地压缩分支。注意
        qq_official 等第三方转存平台例外：入口处的格式体检（_offbox_rewrite_blocks）
        可能把假格式图片改写为本地转码 JPEG 交付——官方富媒体上传拒收假格式，
        正确送达优先于「只发 URL」的提速承诺。

        mark_compressed 供 /溯源 向量命中启用：为真时按各回传路径实际的压缩
        证据写入 block["text"]（含文件名与「已压缩」用法说明），使合并消息里
        每张图各带一句配文。为假时一个字节都不碰 block——哈希 /随机图 /原图
        三处既有调用点行为与改造前一致（offbox 格式体检除外，见上）。

        qq_official 例外：格式体检之后还会按 `qq_upload` 组做上传通道分流，
        命中 url / url-scaled / bytes 时由 _qq_upload_takeover 自己上传并发消息
        （绕开适配器「图片恒 base64 直传、5MB 上限」）。该分支返回即视为已投递。

        单次发送保证：同一批图片在一次调用里只发起一次发送。只有 OneBot 直发
        明确失败（FAILED）才允许换通道重发；结果未知（超时）一律就此打住——
        请求可能已经送达，再发一次用户就会收到两张一模一样的图。

        投递成功后默认 stop_event 防 respond stage 重发；多图 /溯源 通过
        `_defer_event_stop` 把 stop 推迟到整轮结束，否则管道会在首图后中断
        当前处理器，后续图片不再处理。

        remember_blocks 供「发缩略图、记原图」使用：/随机图 发出去的是替身
        （thumb_url），但会话历史必须留下原图直链，否则 /原图 只能找回那张 720px
        的缩略图。缺省 None 表示记的就是本次发出的 blocks（其余调用点行为不变）。
        """
        if self._event_already_sent(event):
            logger.warning("本次事件已投递过图片，跳过重复投递")
            return
        target = self._delivery_target(event)
        self._remember_blocks(event, blocks if remember_blocks is None else remember_blocks)
        # 格式体检可能把假格式转成本地 JPEG（block["_tmp_owned"]）；这些临时文件
        # 只服务于本次投递，发送完成后必须删除，否则会随每次 /随机图、/原图 累积。
        owned_paths: list[str] = []
        try:
            if target.offbox_upload:
                # 第三方转存平台的两个前置动作（格式体检 → 上传通道接管）收在一个
                # 入口里：前者可能把假格式换成 path/缩略图 URL，后者必须拿着改写后
                # 的 block 才能判断该走 URL 还是分片，顺序不可交换
                blocks, handled = await self._offbox_pipeline(
                    event, header, blocks, mark_compressed=mark_compressed
                )
                owned_paths = [
                    str(block.get("path") or "")
                    for block in blocks
                    if block.get("_tmp_owned") and block.get("path")
                ]
                if handled:
                    # 通道接管已自行发完消息：必须标记已发送并 stop_event，否则
                    # respond stage 会照原路（base64 直传）再发一遍
                    self._mark_event_sent(event)
                    if not self._defer_event_stop(event):
                        event.stop_event()
                    return
            mode, max_side, quality, scaled_url_style = self._delivery_config()
            if mode_override is not None:
                mode = mode_override
            image_blocks = [block for block in blocks if block.get("url") or block.get("path")]
            raw_urls = [str(block["url"]) for block in image_blocks if block.get("url")]
            all_urls = bool(image_blocks) and len(raw_urls) == len(image_blocks)
            direct_mode = mode in {"url", "url-scaled"}
            # URL 直发条件：模式允许 + 全是 URL + 能走 OneBot + 所有 URL 扩展名
            # 能被协议端解析出宽高（否则 QQ 侧拿不到尺寸，体验差）。
            # url-scaled 模式下默认走 URL 直传；若 QQ 协议端会剥离查询参数导致
            # 图床返回原图，可把 scaled_url_style 改为 "cf-path"（路径式，参数
            # 嵌在路径里不会被剥离，但需图床域名开启 Cloudflare Image Resizing）。
            direct_ok = (
                direct_mode
                and all_urls
                and target.direct_send
                and (mode == "url" or all(is_napcat_parseable_url(u) for u in raw_urls))
            )

            # 回传计划最多只算一次：回退路径此前会重跑一遍规划，把同样的探测白做
            # 第二次（图床还会因此多做一次缩放处理）。
            plan: tuple[list[str], list[CompressionEvidence]] | None = None
            if direct_ok:
                plan = await self._url_delivery_plan(
                    image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style
                )
                urls, evidences = plan
                if mark_compressed:
                    self._mark_compressed_captions(blocks, evidences)
                outcome = await self._send_via_onebot(event, header, blocks, urls)
                if outcome is not SendOutcome.FAILED:
                    # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                    self._mark_event_sent(event)
                    if not self._defer_event_stop(event):
                        event.stop_event()
                    return

            if allow_local_fallback and not self._event_already_sent(event):
                should_compress = mode in {"auto", "bytes"} or (
                    mode == "url-scaled" and any(block.get("path") for block in image_blocks)
                )
                # 走 URL 直传但没能直发成功：既包括 FAILED 后的回退，也包括扩展名
                # 让协议端解析不了宽高、压根没尝试直发的情形（改本地压缩成 JPEG 再发）
                onebot_failed = target.direct_send and direct_mode and all_urls
                if should_compress or onebot_failed:
                    if mode in {"auto", "bytes"} and target.direct_send:
                        # OneBot 下 auto/bytes 直发压缩字节（混合载荷）：压缩结果
                        # 经 base64 段直达 QQ，不经协议端二次下载，压缩 100% 生效
                        outcome = await self._local_compress_delivery(
                            event, header, blocks, image_blocks, mark_compressed=mark_compressed
                        )
                        if outcome is not None and outcome is not SendOutcome.FAILED:
                            # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                            self._mark_event_sent(event)
                            if not self._defer_event_stop(event):
                                event.stop_event()
                            return
                        # None=准备失败 或 FAILED=直发失败：字节路径已试过，整批改走 URL 链
                        logger.warning("OneBot 压缩直发未成功，改用标准消息链 URL 发送")
                    else:
                        prepared = await self._prepare_delivery_blocks(
                            image_blocks, compress=mode != "url"
                        )
                        if prepared is not None:
                            evidences = [
                                CompressionEvidence.COMPRESSED
                                if compressed
                                else CompressionEvidence.ORIGINAL
                                for _, compressed in prepared
                            ]
                            if mark_compressed:
                                self._mark_compressed_captions(blocks, evidences)
                            fallback_header = header
                            if any(item is CompressionEvidence.COMPRESSED for item in evidences):
                                fallback_header += "（已压缩）"
                            chain = self._mention_prefix(event)
                            if fallback_header:
                                chain.append(Comp.Plain((" " if chain else "") + fallback_header))
                            image_index = 0
                            for block in blocks:
                                if block.get("text"):
                                    chain.append(Comp.Plain("\n" + str(block["text"])))
                                if block.get("url") or block.get("path"):
                                    data, _ = prepared[image_index]
                                    chain.append(Comp.Image.fromBytes(data))
                                    image_index += 1
                            self._mark_event_sent(event)
                            yield event.chain_result(chain)
                            return
                        logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

            if self._event_already_sent(event):
                return
            if plan is None:
                plan = await self._url_delivery_plan(
                    image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style
                )
            urls, evidences = plan
            if mark_compressed:
                self._mark_compressed_captions(blocks, evidences)
            chain = self._mention_prefix(event)
            if header:
                chain.append(Comp.Plain((" " if chain else "") + header))
            # urls / evidences 与 image_blocks（有 url 或 path 的 block）同序，
            # 而本循环遍历的是全部 blocks（含纯文本 block）。因此计数必须按
            # 「有图 block」推进，只对 url block 取用 urls——否则前面夹一张
            # 只有 path 的图（格式体检转码产物）就会让后续 URL 用错下标。
            image_index = 0
            for block in blocks:
                if block.get("text"):
                    chain.append(Comp.Plain("\n" + str(block["text"])))
                path = str(block.get("path") or "")
                if block.get("url"):
                    chain.append(Comp.Image.fromURL(urls[image_index]))
                elif path:
                    chain.append(Comp.Image.fromFileSystem(path))
                if block.get("url") or path:
                    image_index += 1
            self._mark_event_sent(event)
            yield event.chain_result(chain)
        finally:
            # 生成器被消费完/关闭时执行：此时消息已由 OneBot 直发或由 respond
            # stage 发完，删除转码产物是安全的
            for owned in owned_paths:
                self._remove_quiet(owned)
