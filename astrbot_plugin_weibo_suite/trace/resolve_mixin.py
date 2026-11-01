"""图片来源解析、下载、探测与临时文件清理。"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import time
from urllib.parse import (
    urlparse,
)

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import (
    logger,
)
from astrbot.api.event import (
    AstrMessageEvent,
)

try:
    from ..shared.common import (
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        TMP_MAX_AGE_SECONDS,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        truthy,
    )
    from ..shared.http import (
        GuardedHttpClient,
    )
    from .features import (
        heif_available,
        image_mime,
    )
    from .image_delivery import (
        bounded_int,
        classify_probe_status,
        content_length_from_headers,
        is_scaling_capability_rejection,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from features import (  # type: ignore[no-redef]
        heif_available,
        image_mime,
    )
    from image_delivery import (  # type: ignore[no-redef]
        bounded_int,
        classify_probe_status,
        content_length_from_headers,
        is_scaling_capability_rejection,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        TMP_MAX_AGE_SECONDS,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        truthy,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        GuardedHttpClient,
    )

try:
    from .constants import (
        _STREAM_WRITE_BATCH_BYTES,
        ResolvedImage,
        _describe_bad_image,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        _STREAM_WRITE_BATCH_BYTES,
        ResolvedImage,
        _describe_bad_image,
    )


class ResolveMixin:
    """图片来源解析、下载、探测与临时文件清理。"""

    # ------------------------------------------------------------------
    # 图片提取与解析
    # ------------------------------------------------------------------

    @staticmethod
    def _image_identity(seg) -> set:
        """图片段可用于去重的标识集合（url / file / path 全部收集）。

        同一张图在消息链与被引用消息里往往只带其中一部分字段（链里是 url、
        Reply.chain 里只有 file），只取其一就会把同一张图判成两张：/溯源 于是
        对同一张图跑两遍、回传两遍，表现为「图片被重复发送」。
        """
        keys = set()
        for name in ("url", "file", "path"):
            value = getattr(seg, name, None)
            if value:
                keys.add(str(value))
        return keys

    @classmethod
    def _extract_images(cls, event: AstrMessageEvent) -> list:
        """提取消息链与被引用消息中的图片段（按链接/文件名去重）。"""
        found: list = []
        seen: set = set()

        def push(seg) -> None:
            keys = cls._image_identity(seg) or {f"id:{id(seg)}"}
            if keys & seen:
                return
            seen.update(keys)
            found.append(seg)

        for seg in event.message_obj.message or []:
            if isinstance(seg, Comp.Image):
                push(seg)
            elif isinstance(seg, Comp.Reply):
                for sub in getattr(seg, "chain", None) or []:
                    if isinstance(sub, Comp.Image):
                        push(sub)
        return found

    @staticmethod
    def _probe_mime(path: str):
        """解析探测：返回 (None, mime) 表示有效，None 表示无效；不抛错。"""
        mime = image_mime(path)
        return (None, mime) if mime else None

    async def _resolve_local(self, seg, *, probe) -> ResolvedImage:
        """把图片段解析为本地文件，并顺带执行 probe 的一次解码。

        probe(path) 成功时返回 (features|None, mime)，无效时返回 None 或抛错
        （两者都按“无效”处理）。这样「格式校验」与「特征计算」可以共用一次
        Image.open()：需要特征的调用方传 compute_features_with_mime，只需要
        MIME 的调用方传 _probe_mime。

        解析失败时的分诊与旧实现一致：
        - 认不出任何图片容器（错误体/防盗链响应）→ 才走自带下载兜底；
        - 认得出容器却解不开（缺解码器的 HEIC、截断文件）→ 直接返回具体原因，
          不再为同一内容白发一次下载。
        """

        def probe_path(path: str, is_tmp: bool) -> ResolvedImage | None:
            try:
                result = probe(path)
            except Exception:
                return None
            if not result:
                return None
            features, mime = result
            return ResolvedImage(path=path, is_tmp=is_tmp, mime=mime or "", features=features)

        try:
            path = await seg.convert_to_file_path()
            if path and os.path.isfile(path):
                resolved = await asyncio.to_thread(probe_path, path, False)
                if resolved is not None:
                    return resolved
                summary, sniffed = await asyncio.to_thread(_describe_bad_image, path)
                if sniffed is not None:
                    logger.warning(
                        f"内置解析产物无法解码（{summary}，容器格式 {sniffed}），"
                        "链接指向同一内容，跳过兜底下载"
                    )
                    return ResolvedImage(message=self._decode_fail_message(sniffed))
                logger.warning(f"内置解析产物不是有效图片（{summary}），改走兜底下载")
        except Exception as e:
            logger.debug(f"convert_to_file_path 失败，尝试兜底下载: {e}")

        url = getattr(seg, "url", None)
        if url and str(url).startswith(("http://", "https://")):
            try:
                path = await self._download(str(url))
            except Exception as e:
                logger.warning(f"图片兜底下载失败: {e}")
                return ResolvedImage(message=self._download_fail_message())
            resolved = await asyncio.to_thread(probe_path, path, True)
            if resolved is not None:
                return resolved
            summary, _ = await asyncio.to_thread(_describe_bad_image, path)
            logger.warning(f"兜底下载内容不是有效图片（{summary}），链接可能已过期或被防盗链拦截")
            self._remove_quiet(path)
        return ResolvedImage(message=self._download_fail_message())

    @staticmethod
    def _download_fail_message() -> str:
        return (
            "⚠️ 未能获取图片内容：图片链接可能已过期（QQ 图床签名失效）或被防盗链拦截，"
            "请重新发送、或引用该图片后再试。"
        )

    @staticmethod
    def _decode_fail_message(sniffed: str) -> str:
        """认得出容器却解不开时的提示；HEIC 缺解码器是最常见的一种。"""
        if sniffed == "HEIF" and not heif_available():
            return (
                "⚠️ 检测到 HEIC/HEIF 图片（iPhone 默认格式），但服务端未安装 pillow-heif "
                "解码支持，无法溯源。安装该依赖后重启机器人即可。"
            )
        return (
            f"⚠️ 未能解析图片内容：识别为 {sniffed} 但解码失败，"
            "文件可能已损坏或格式不受当前服务端支持，详情见机器人日志。"
        )

    def _download_limit_bytes(self) -> int:
        """回传兜底下载的大小上限（字节）：limits.max_download_mb。"""
        return max(1, self._int_cfg("max_download_mb", 20)) * 1024 * 1024

    @staticmethod
    def _download_timeout(limit: int = 0) -> aiohttp.ClientTimeout:
        """兜底下载/格式体检统一的出网超时。

        固定 30s 对分片通道的 100MB 原图必然超时；按最低 512KB/s 的保守带宽
        估算总时限（上限 10 分钟），小文件仍是原来的 30s。
        """
        total = DOWNLOAD_TIMEOUT
        if limit > 0:
            total = max(total, min(600.0, limit / (512 * 1024)))
        return aiohttp.ClientTimeout(total=total)

    async def _stream_response_to_temp(self, resp, limit: int) -> str:
        """把已打开的响应流式写入临时文件，返回路径；超限或失败即删文件。

        临时文件名完全不受外部输入影响（固定扩展名），图片解码由 Pillow
        按内容判断，与扩展名无关。
        """
        path = os.path.join(self.tmp_dir, f"q_{secrets.token_hex(8)}.jpg")
        part_path = path + ".part"

        def _append(buf: bytes) -> None:
            # 每批单独 open/append/fsync/close，不在 await 之间持有文件句柄：
            # 任务被取消时 Windows 上不会留下"句柄没释放、删不掉"的 .part。
            # 先落盘再改名：下载中途被取消/网络中断时只会留下 .part，
            # 不会留下一个"看着像完整图片"的截断文件被下游发出去。
            with open(part_path, "ab") as file:
                file.write(buf)
                file.flush()
                os.fsync(file.fileno())

        try:
            received = 0
            pending = bytearray()
            async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                received += len(chunk)
                if received > limit:
                    raise ValueError("图片超过大小上限")
                pending += chunk
                if len(pending) >= _STREAM_WRITE_BATCH_BYTES:
                    batch = bytes(pending)
                    pending.clear()
                    # 写盘必须离开事件循环线程：几十 MB 的图片 write+fsync 会把
                    # 同进程其它会话的响应全部卡住（shared/http.stream_to_file
                    # 也是同样的处理）。
                    await asyncio.to_thread(_append, batch)
            # 空响应也要落一个空 .part，否则下面的 os.replace 会 FileNotFoundError
            await asyncio.to_thread(_append, bytes(pending))
            os.replace(part_path, path)
        except BaseException:
            # CancelledError 也要清掉半成品，否则重启后残留被当缓存命中
            self._remove_quiet(part_path)
            self._remove_quiet(path)
            raise
        return path

    async def _download(self, url: str, *, size_limit: int | None = None) -> str:
        """下载到临时文件，返回路径。

        size_limit 供「分片上传」这类明确知道上限的调用方覆盖默认值：QQ 分片
        通道允许 200MB，而 max_download_mb（默认 20MB）是给回传兜底用的，按它
        截断会把本可以发出去的大图判成「超过大小上限」。缺省 None 时仍按配置。
        """
        client = await self._get_http()
        limit = (
            size_limit
            if size_limit is not None and size_limit > 0
            else self._download_limit_bytes()
        )
        async with await client.request(
            "GET",
            url,
            # prepare 契约为三参（url, method, cross_origin），签名不符
            # 会在请求发出前就 TypeError
            prepare=self._image_download_headers,
            timeout=self._download_timeout(limit),
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise ValueError("图片超过大小上限")
            return await self._stream_response_to_temp(resp, limit)

    async def _download_bytes(self, url: str) -> bytes:
        """下载到内存；声明体积超过内存阈值时退回落盘读回。

        回退交付只需要字节（压缩、发送都在内存里完成）。内存占用由两道闸门
        控制：声明长度超过 MEMORY_DOWNLOAD_MAX_BYTES、或长度未知（分块传输）
        时一律落盘后读回——未知长度无法在读取前判阈值，留在内存等于按
        max_download_mb（默认 20MB）撑爆并发上限 3 的预算。落盘分支复用当前
        已打开的响应，不再发第二次 GET。
        """
        client = await self._get_http()
        size_limit = self._download_limit_bytes()
        async with await client.request(
            "GET",
            url,
            prepare=self._image_download_headers,
            timeout=self._download_timeout(size_limit),
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > size_limit:
                raise ValueError("图片超过大小上限")
            if not declared.isdigit():
                # 分块传输没有声明长度，无法在读取前判断是否超过内存阈值：
                # 直接落盘再读回，避免未知长度按 20MB 上限塞满内存。
                path = await self._stream_response_to_temp(resp, size_limit)
                try:
                    return await asyncio.to_thread(read_bytes, path)
                finally:
                    self._remove_quiet(path)
            if int(declared) > MEMORY_DOWNLOAD_MAX_BYTES:
                # 大图不冒险占内存：复用当前响应落盘后读回
                path = await self._stream_response_to_temp(resp, size_limit)
                try:
                    return await asyncio.to_thread(read_bytes, path)
                finally:
                    self._remove_quiet(path)
            buffer = io.BytesIO()
            received = 0
            async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                received += len(chunk)
                if received > size_limit:
                    raise ValueError("图片超过大小上限")
                buffer.write(chunk)
            return buffer.getvalue()

    @staticmethod
    def _image_download_headers(u: str, _m: str, _c: bool) -> dict:
        """图片兜底下载的请求头。

        NTQQ（multimedia.nt.qq.com.cn）与旧版 gchat.qpic.cn 图床均启用了
        防盗链：缺 Referer 时会返回 403 或只有几十~几百字节的错误体。两者的
        防盗链规则按各自站点校验，因此 Referer 必须与图片同族——拿旧图床的
        Referer 去请求 NTQQ，反而会被判成跨站盗用。Referer 不是凭据，跨域
        重定向是否剥离交给 url_guard；重定向离开该域名后因 host 不匹配自然
        不再携带。
        """
        host = (urlparse(u).hostname or "").lower()
        if not is_qq_image_bed_host(host):
            return {}
        if host == "multimedia.nt.qq.com.cn" or host.endswith(".nt.qq.com.cn"):
            return {"headers": {"Referer": "https://multimedia.nt.qq.com.cn/"}}
        return {"headers": {"Referer": "https://gchat.qpic.cn/"}}

    @classmethod
    def _range_probe_headers(cls, u: str, m: str, c: bool) -> dict:
        """在下载请求头基础上加 Range: bytes=0-0，用于退化探测响应体长度。"""
        base = dict(cls._image_download_headers(u, m, c))
        headers = dict(base.get("headers") or {})
        headers["Range"] = "bytes=0-0"
        base["headers"] = headers
        return base

    async def _probe_url(
        self, url: str, cache: dict | None = None, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """轻量探测 URL 是否存在及其响应体长度，返回 (是否存在, 字节数)。

        两个用途：判定图床缩放 URL 是否真的比原图小（压缩标识的唯一一手
        证据），以及 /原图 按文件名拼出的直链是否真的存在于图床。HEAD 优先，
        不支持时退化为只取 0-0 一个字节的 Range GET（仅限不带缩放参数的 URL：
        ImgBed 明确拒绝「Range + 缩放」的组合，退化为 Range 只会白花一次请求）。
        任何网络异常都返回 (None, None)——调用方必须把「探测不出来」与「确定
        不存在」分开处理，网络抖动不该被当成「图床没有这张图」。
        """
        if cache is not None and url in cache:
            return cache[url]
        result = await self._probe_url_uncached(url, allow_range=allow_range)
        if cache is not None:
            cache[url] = result
        return result

    async def _probe_url_uncached(
        self, url: str, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """探测实现。探测用 PROBE_TIMEOUT 而非下载超时：探测只读响应头，
        此前共用 30s 会让一次卡住的探测把交付拖到分钟级。

        缩放 URL（allow_range=False）的 HEAD 405 特殊处理：CloudFlare-ImgBed
        的缩放 API 只接受 GET 不接受 HEAD，405 不等于「缩放不支持」。
        遇到 405 时回退到 GET（不带 Range）再探一次，确认缩放功能是否真的不可用。
        """
        client = await self._get_http()
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)

        # ---------- 第一步：HEAD 探测 ----------
        head_status: int | None = None
        try:
            async with await client.request(
                "HEAD", url, prepare=self._image_download_headers, timeout=timeout
            ) as resp:
                head_status = resp.status
                # 405 先不记冷却：它可能只是 HEAD 方法不被允许，而不是缩放
                # 功能本身不可用，下面回退 GET 再确认。
                if (
                    not allow_range
                    and resp.status != 405
                    and is_scaling_capability_rejection(resp.status)
                ):
                    self._note_scaled_unsupported(resp.status)
                exists = classify_probe_status(resp.status)
                if exists is False:
                    return False, None
                length = content_length_from_headers(resp.headers)
                if exists is True and length is not None:
                    return True, length
                if not allow_range and resp.status != 405:
                    # 非 405 的缩放探测失败（400/501/网络问题等）：不回退，
                    # 400/501 已在上文记冷却，其余按「无法判定」处理。
                    return None, None
        except Exception as exc:
            logger.debug(f"HEAD 探测失败 {url}: {exc}")
            if not allow_range:
                return None, None

        # ---------- 缩放 URL 的 405 回退：GET（不带 Range） ----------
        if not allow_range and head_status == 405:
            try:
                async with await client.request(
                    "GET", url, prepare=self._image_download_headers, timeout=timeout
                ) as resp:
                    # 只读响应头，不读响应体
                    if is_scaling_capability_rejection(resp.status):
                        # GET 也被拒绝：这才是真的缩放能力缺失
                        self._note_scaled_unsupported(resp.status)
                        return None, None
                    exists = classify_probe_status(resp.status)
                    length = content_length_from_headers(resp.headers)
                    if exists is True and length is not None:
                        return True, length
                    return None, None
            except Exception as exc:
                logger.debug(f"缩放 URL GET 回退探测失败 {url}: {exc}")
                return None, None

        # ---------- 普通 URL 的 Range GET 回退 ----------
        try:
            async with await client.request(
                "GET", url, prepare=self._range_probe_headers, timeout=timeout
            ) as resp:
                # 只读响应头，绝不读响应体：Range 被忽略而返回 200 时，
                # 完整读下来就等于把整张图下载了一遍，探测也就失去了意义
                return classify_probe_status(resp.status), content_length_from_headers(resp.headers)
        except Exception as exc:
            logger.debug(f"URL 探测失败（无法判定存在性）{url}: {exc}")
        return None, None

    def _note_scaled_unsupported(self, status: int) -> None:
        """记录「图床处理不了缩放请求」，随后一段时间跳过缩放探测。"""
        self._scaled_verify_unsupported_until = time.monotonic() + SCALED_VERIFY_COOLDOWN_SECONDS
        logger.info(
            f"图床拒绝了缩放请求（HTTP {status}）：{SCALED_VERIFY_COOLDOWN_SECONDS} 秒内"
            "不再探测缩放版（配文相应不再标注「已压缩」）"
        )

    def _scaled_verify_cooling(self) -> bool:
        """是否处在「图床不支持缩放」的冷却期内。"""
        return time.monotonic() < self._scaled_verify_unsupported_until

    def _note_qq_url_unsupported(self, exc: BaseException) -> None:
        """记录「QQ 拉不到图床公网直链」，随后一段时间 auto 模式改走分片。

        与「图床拒绝缩放请求」同一套思路：公网域名存在不代表腾讯侧拉得到
        （域名停用、防盗链、防火墙、跨境链路），而这类失败每次都要白等一轮
        往返。失败一次就记冷却，冷却期内 auto 直接分片；固定 `url` 通道不受
        影响——用户显式钉死一条通道时就该照做，失败由 on_failure 决定。
        """
        self._qq_url_unsupported_until = time.monotonic() + QQ_URL_COOLDOWN_SECONDS
        logger.warning(
            f"QQ URL 上传失败：{QQ_URL_COOLDOWN_SECONDS} 秒内 auto 模式改走分片"
            f"（固定 url 通道不受影响）; {type(exc).__name__}: {exc}"
        )

    def _qq_url_cooling(self) -> bool:
        """是否处在「URL 上传通道不可用」的冷却期内。"""
        return time.monotonic() < self._qq_url_unsupported_until

    def _verify_scaled(self) -> bool:
        """delivery.napcat_verify_scaled：图床缩放是否实测校验，未配置时默认开启。"""
        raw = self._napcat_delivery_cfg().get("verify_scaled")
        return True if is_blank(raw) else truthy(raw)

    def _onebot_image_timeout(self) -> int:
        """delivery.napcat_timeout：OneBot image 段的 timeout 字段（秒）。

        该字段是协议端（NapCat 等）下载网络图片的窗口，不是插件的出网超时。
        未配置或填了非数字时用 common.ONEBOT_IMAGE_TIMEOUT；其余值收敛到
        [10, 300]——填 0 会落到 10 而不是原样写 0，否则等于告诉协议端「立刻
        放弃下载」，图片必然拉不到。
        """
        raw = self._napcat_delivery_cfg().get("onebot_image_timeout")
        return bounded_int(raw, ONEBOT_IMAGE_TIMEOUT, 10, 300)

    async def _get_http(self) -> GuardedHttpClient:
        if self._http is None:
            # 连接器启用 IP 钉扎：出网连接只允许落在 url_guard 校验过的地址上。
            # 内网直连白名单按实例传入，避免多实例共享模块级全局集合。
            self._http = GuardedHttpClient(self._allowed_private_hosts)
        return self._http

    def _cleanup_tmp(self) -> None:
        """清理超过保留时限的临时文件（插件加载与卸载时各执行一次）。"""
        try:
            now = time.time()
            for name in os.listdir(self.tmp_dir):
                path = os.path.join(self.tmp_dir, name)
                if os.path.isfile(path) and now - os.path.getmtime(path) > TMP_MAX_AGE_SECONDS:
                    os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _remove_quiet(path: str) -> None:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
