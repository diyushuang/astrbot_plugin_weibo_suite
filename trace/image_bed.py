"""泛用图床/对象存储客户端（门面）。

mode=local        : 原图副本保存到机器人数据目录（零配置，默认）。
mode=generic_http : 通过可配置的通用 HTTP 接口上传，兼容 Lsky Pro、
                    EasyImages、Chevereto 等常见自建图床——只需配置
                    上传地址、鉴权头、文件字段名与响应直链的 JSON 路径。
mode=cloudflare_r2: Cloudflare R2 对象存储（官方 S3 兼容 API）。
mode=oracle_oci   : 甲骨文 OCI Object Storage（官方 S3 兼容 API）。
mode=cloudflare_imgbed: CloudFlare-ImgBed 自建图床（官方 REST API）。

后端实现按模式拆到 `storage_local` / `storage_cfb` / `storage_object`，
共用结果类型与纯函数在 `storage_utils`；本模块只保留装配、重试与降级状态。

上传失败或无法生成长期有效的公开直链时，自动回退为本地副本保存，
保证登记流程不因图床故障而中断（溯源回图需要能长期访问的链接）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from urllib.parse import unquote, urlparse

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_bed")

try:
    from ..shared.common import as_int
    from ..shared.http import GuardedHttpClient
    from .storage_cfb import CloudflareImgBedMixin
    from .storage_local import LocalHttpBackendMixin
    from .storage_object import ObjectStorageMixin
    from .storage_utils import (
        _UPLOAD_HTTP_STATUS_RE,
        MODE_CFB,
        MODE_HTTP,
        MODE_LOCAL,
        MODE_OCI,
        MODE_R2,
        UPLOAD_RETRY_ATTEMPTS,
        UPLOAD_RETRY_BASE_DELAY,
        UPLOAD_TERMINAL_STATUS,
        BedListUnsupported,
        StoreResult,
        _clean_folder,
        _host_port,
        _public_url,
        _same_origin,
        bed_file_id,
        extract_json_path,
        is_retryable_upload_error,
        redact_url,
        sha256_of_file,
        upload_error_status,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from storage_cfb import CloudflareImgBedMixin  # type: ignore[no-redef]
    from storage_local import LocalHttpBackendMixin  # type: ignore[no-redef]
    from storage_object import ObjectStorageMixin  # type: ignore[no-redef]
    from storage_utils import (  # type: ignore[no-redef]
        _UPLOAD_HTTP_STATUS_RE,
        MODE_CFB,
        MODE_HTTP,
        MODE_LOCAL,
        MODE_OCI,
        MODE_R2,
        UPLOAD_RETRY_ATTEMPTS,
        UPLOAD_RETRY_BASE_DELAY,
        UPLOAD_TERMINAL_STATUS,
        BedListUnsupported,  # type: ignore[no-redef]
        StoreResult,
        _clean_folder,
        _host_port,
        _public_url,
        _same_origin,
        bed_file_id,
        extract_json_path,
        is_retryable_upload_error,
        redact_url,
        sha256_of_file,
        upload_error_status,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        as_int,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        GuardedHttpClient,  # type: ignore[no-redef]
    )


class ImageBedClient(
    LocalHttpBackendMixin,
    CloudflareImgBedMixin,
    ObjectStorageMixin,
):
    def __init__(
        self,
        data_dir: str,
        bed_config: dict,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
    ):
        self.mode = str(bed_config.get("mode") or MODE_LOCAL).strip()
        self.config = bed_config or {}
        self.local_dir = os.path.join(data_dir, "images")
        os.makedirs(self.local_dir, exist_ok=True)
        self._http_getter = http_getter
        # S3 兼容客户端按模式缓存复用；HTTP 会话由共享客户端统一释放
        self._s3_clients: dict = {}
        # 上传失败只告警一次：重试/批处理里每条都打 warning 会刷屏，
        # 还会把真正的原因（第一条）埋掉
        self._upload_warned = False
        # 降级状态：上传持续失败、当前所有登记都在存本地副本。为空表示图床可用。
        # /trace 状态接口会读它——只打一条 warning 的话，用户过几天根本不会
        # 记得图床挂了，只会疑惑"为什么直链都是本地路径"。
        self._degraded_reason = ""
        self._degraded_since = 0.0

    # ---------- 对外接口 ----------

    async def list_files(self, *, limit: int = 0) -> list:
        """枚举图床文件（仅实现支持列表的两种模式）。

        `limit=0` 表示不限。返回值是 `storage_utils.BedFile` 列表；不支持枚举
        的后端抛 `BedListUnsupported`，让调用方把「查不了」和「没有文件」分开。
        """
        if self.mode == MODE_CFB:
            return await self._list_cf(limit=limit)
        if self.mode == MODE_LOCAL:
            return await self._list_local(limit=limit)
        raise BedListUnsupported(
            f"图床模式 {self.mode!r} 不支持文件枚举："
            "目前只支持 cloudflare_imgbed 与 local；"
            "R2 / OCI 请改用 S3 控制台核对，generic_http 没有列表接口。"
        )

    async def store(self, src_path: str, *, folder: str = "") -> StoreResult:
        """登记原图的落库：优先上传图床/对象存储，失败回退本地副本。

        folder 是可选的床内目录（缩略图走 "thumbnails/"），各模式语义：
        - cloudflare_imgbed：覆盖 URL 的 uploadFolder 参数，空则回落配置项；
        - local：作为 local_dir 下的子目录；"../" 或绝对路径一律拒绝；
        - cloudflare_r2 / oracle_oci：作为对象 key 前缀；
        - generic_http：服务端没有目录概念，忽略并留一行 DEBUG。
        folder 为空时与旧行为完全一致。
        """
        raw_folder = str(folder or "").strip()
        uploaders = {
            MODE_HTTP: (self._upload, "图床上传失败", "已上传至图床"),
            MODE_CFB: (self._upload_cf, "CloudFlare-ImgBed 上传失败", "已上传至 CloudFlare-ImgBed"),
            MODE_R2: (self._upload_object, "Cloudflare R2上传失败", "已上传至Cloudflare R2"),
            MODE_OCI: (self._upload_object, "OCI 对象存储上传失败", "已上传至OCI 对象存储"),
        }
        entry = uploaders.get(self.mode)
        if entry is not None:
            upload, failure, ok_message = entry
            if self.mode == MODE_HTTP and raw_folder:
                # 通用 HTTP 图床的目录由服务端 API 决定，客户端不传
                logger.debug(f"generic_http 图床不支持目录参数，已忽略 folder={raw_folder!r}")
            try:
                url = await self._upload_with_retry(upload, src_path, raw_folder)
                self.mark_recovered()
                return StoreResult(ok=True, url=url, message=ok_message)
            except Exception as e:
                # 第三方响应体可能很长、还可能回显内部地址：只留前 200 字符；
                # traceback 一律不记（栈帧局部变量里带直链与凭据）；同一次运行
                # 只发一次 warning，后续同类失败降为 debug
                detail = str(e)[:200]
                if self._upload_warned:
                    logger.debug(f"{failure}（本次运行已记过告警）: {detail}")
                else:
                    self._upload_warned = True
                    logger.warning(f"{failure}: {detail}", exc_info=False)
                self._mark_degraded(detail)
                try:
                    local = await self._copy_local(src_path, raw_folder)
                except ValueError:
                    # 床内目录在本地无意义（如 "../x"），回退到数据目录根下
                    local = await self._copy_local(src_path)
                return StoreResult(
                    ok=True,
                    file_path=local,
                    message=f"{failure}，已保存本地副本（详情见日志）",
                )
        try:
            local = await self._copy_local(src_path, raw_folder)
        except ValueError as e:
            logger.warning(f"图床目录不合法，已拒绝保存: {e}")
            return StoreResult(ok=False, message=f"图床目录不合法：{e}（未保存）")
        if self.mode != MODE_LOCAL:
            # schema 的 options 会拦住常见笔误，但手工改配置文件可绕过；
            # 静默按 local 处理会让人误以为配置生效了
            logger.warning(f"未知的图床模式 {self.mode!r}，已按本地副本保存。")
            return StoreResult(
                ok=True,
                file_path=local,
                message=f"图床模式 {self.mode!r} 无法识别，已保存本地副本",
            )
        return StoreResult(ok=True, file_path=local, message="已保存到本地图床")

    async def _upload_with_retry(self, upload, src_path: str, raw_folder: str) -> str:
        """带指数退避的上传：只对瞬时故障重试，鉴权/配置类错误立即放弃。

        每次重试都重新调用 upload（内部会重开文件句柄），因此不会复用已被
        上一次失败读空/关闭的流。CancelledError 属于 BaseException，不会被
        这里的 except Exception 吞掉，取消语义不变。
        """
        attempts = max(1, int(UPLOAD_RETRY_ATTEMPTS))
        for attempt in range(attempts):
            try:
                if raw_folder:
                    return await upload(src_path, _clean_folder(raw_folder))
                # 保持单参调用，兼容既有单参上传实现与测试替身
                return await upload(src_path)
            except Exception as exc:
                last = attempt + 1 >= attempts
                if last or not is_retryable_upload_error(exc):
                    raise
                delay = UPLOAD_RETRY_BASE_DELAY * (2**attempt)
                logger.debug(
                    f"图床上传失败（第 {attempt + 1}/{attempts} 次），"
                    f"{delay:.2f}s 后重试: {str(exc)[:200]}"
                )
                await asyncio.sleep(delay)
        raise RuntimeError("上传重试耗尽")  # pragma: no cover - 循环内必然 return/raise

    def _mark_degraded(self, detail: str) -> None:
        """标记图床不可用（自本次降级开始的第一次失败计时）。"""
        if not self._degraded_reason:
            self._degraded_since = time.time()
        self._degraded_reason = detail

    def mark_recovered(self) -> None:
        """上传成功后清除降级标记（图床恢复不需要重启插件）。"""
        self._degraded_reason = ""
        self._degraded_since = 0.0

    def status(self) -> dict:
        """图床可用性快照，供 /trace 状态接口展示。"""
        return {
            "mode": self.mode,
            "degraded": bool(self._degraded_reason),
            "degraded_reason": self._degraded_reason,
            "degraded_since": self._degraded_since,
        }

    def _public_base(self, key: str, name: str) -> str:
        """取公开直链 base；缺少 http(s):// 前缀时按未配置处理。

        直链会入库并用于回图，scheme 不全的 base（如漏写 https:// 的
        r2.dev 子域）会产生非法直链：上传虽成功，回图却静默失败。
        """
        base = str(self.config.get(key) or "").strip().rstrip("/")
        if base and not base.lower().startswith(("http://", "https://")):
            logger.warning(f"{name} 缺少 http(s):// 前缀（当前值: {base}），已忽略。")
            return ""
        return base

    def _timeout(self) -> aiohttp.ClientTimeout:
        """图床 HTTP 请求的统一超时（配置缺失或非法时取 30 秒）。"""
        return aiohttp.ClientTimeout(total=max(5, as_int(self.config.get("timeout"), 30)))

    async def delete_remote(self, image_url: str) -> bool:
        """删除对象存储中的远端对象；URL 不属于当前配置时返回 False。

        对 cloudflare_r2 / oracle_oci 模式按公开直链反解对象名，与当前
        配置同源且路径前缀匹配后调用 DeleteObject；cloudflare_imgbed 模式
        按站点直链反解文件路径后调用官方删除 API（Token 需 delete 权限）。
        """
        if not image_url:
            return False
        try:
            parsed = urlparse(image_url)
            if self.mode == MODE_CFB:
                base = self._cf_base()
                base_parsed = urlparse(base)
                if not _same_origin(parsed, base_parsed):
                    return False
                prefix = base_parsed.path.rstrip("/") + "/file/"
                if not parsed.path.startswith(prefix):
                    return False
                file_id = unquote(parsed.path[len(prefix) :])
                if not file_id:
                    return False
                token = str(self.config.get("cfi_token") or "").strip()
                if not token:
                    return False
                return await self._cf_delete(token, file_id)
            if self.mode == MODE_R2:
                base = self._public_base("r2_public_base_url", "r2_public_base_url")
                if not base:
                    return False
                base_parsed = urlparse(base)
                if not _same_origin(parsed, base_parsed):
                    return False
                prefix = base_parsed.path.rstrip("/") + "/"
                if not parsed.path.startswith(prefix):
                    return False
                key = unquote(parsed.path[len(prefix) :])
                if not key:
                    return False
                client, bucket = self._r2_client()
                await client.delete_object(bucket, key)
                return True
            if self.mode == MODE_OCI:
                key, matched = self._oci_key_from_url(image_url)
                if not matched:
                    return False
                client, bucket = self._oci_client()
                await client.delete_object(bucket, key)
                return True
        except Exception as e:
            # 不吞异常原因：删除失败（token 权限不足、签名错误、网络故障）需要可诊断。
            # 直链本身可能带签名查询串，写日志前先脱敏
            logger.warning(f"删除远端对象失败 [{self.mode}] {redact_url(image_url)}: {e}")
            return False
        return False

    def src_path_for(self, image_url: str) -> str:
        """从公开直链反解床内路径（仅用于向量 payload 的 src 字段）。

        能反解则返回床内相对路径（R2/OCI 为对象名、CFB 为 /file/xxx），
        否则原样返回直链。插件自身不依赖该字段，仅为兼容图床侧约定。
        """
        if not image_url:
            return ""
        try:
            parsed = urlparse(image_url)
            if self.mode == MODE_CFB:
                base = self._cf_base()
                base_parsed = urlparse(base)
                prefix = base_parsed.path.rstrip("/") + "/file/"
                if _same_origin(parsed, base_parsed) and parsed.path.startswith(prefix):
                    return f"/file/{unquote(parsed.path[len(prefix) :])}"
            elif self.mode == MODE_R2:
                base = self._public_base("r2_public_base_url", "r2_public_base_url")
                if base:
                    base_parsed = urlparse(base)
                    if _same_origin(parsed, base_parsed):
                        prefix = base_parsed.path.rstrip("/") + "/"
                        if parsed.path.startswith(prefix):
                            key = unquote(parsed.path[len(prefix) :])
                            if key:
                                return key
            elif self.mode == MODE_OCI:
                key, matched = self._oci_key_from_url(image_url)
                if matched:
                    return key
        except Exception as e:
            logger.debug(f"反解床内路径失败，回退直链: {e}")
        return image_url

    async def close(self) -> None:
        """保留兼容；HTTP 会话由共享客户端统一释放。"""
        return None


__all__ = [
    "MODE_CFB",
    "MODE_HTTP",
    "MODE_LOCAL",
    "MODE_OCI",
    "MODE_R2",
    "UPLOAD_RETRY_ATTEMPTS",
    "UPLOAD_RETRY_BASE_DELAY",
    "UPLOAD_TERMINAL_STATUS",
    "_UPLOAD_HTTP_STATUS_RE",
    "ImageBedClient",
    "StoreResult",
    "_clean_folder",
    "_host_port",
    "_public_url",
    "_same_origin",
    "bed_file_id",
    "extract_json_path",
    "is_retryable_upload_error",
    "redact_url",
    "sha256_of_file",
    "upload_error_status",
]
