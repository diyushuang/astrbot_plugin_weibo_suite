"""泛用图床/对象存储客户端。

mode=local        : 原图副本保存到机器人数据目录（零配置，默认）。
mode=generic_http : 通过可配置的通用 HTTP 接口上传，兼容 Lsky Pro、
                    EasyImages、Chevereto 等常见自建图床——只需配置
                    上传地址、鉴权头、文件字段名与响应直链的 JSON 路径。
mode=cloudflare_r2: Cloudflare R2 对象存储（官方 S3 兼容 API），
                    endpoint https://<ACCOUNT_ID>.r2.cloudflarestorage.com，
                    签名 region 固定 auto；公开直链需在 Cloudflare 侧
                    开启 r2.dev 公开访问或绑定自定义域名。
mode=oracle_oci   : 甲骨文 OCI Object Storage（官方 Amazon S3 兼容 API），
                    endpoint https://<namespace>.compat.objectstorage.
                    <region>.oraclecloud.com，凭据为 Customer Secret Key；
                    公开直链支持公共存储桶原生 URL 或自定义地址（如
                    带前缀的预认证请求 PAR 地址）。
mode=cloudflare_imgbed: CloudFlare-ImgBed 自建图床（官方 REST API）：
                    POST /upload 上传（API Token 或上传鉴权码，均走请求头），
                    /溯源删除 时按 POST /api/manage/delete/batch 同步
                    删除远端文件（Token 需含 delete 权限）。

上传失败或无法生成长期有效的公开直链时，自动回退为本地副本保存，
保证登记流程不因图床故障而中断（溯源回图需要能长期访问的链接）。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp

try:
    from ..shared.common import USER_AGENT, as_int, read_bytes, truthy
    from ..shared.http import GuardedHttpClient, read_limited_text
    from .s3_store import S3CompatClient
except ImportError:  # 兼容插件以独立模块方式加载
    from s3_store import S3CompatClient  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        USER_AGENT,
        as_int,
        read_bytes,
        truthy,
    )
    from astrbot_plugin_weibo_suite.shared.http import (
        GuardedHttpClient,  # type: ignore[no-redef]
        read_limited_text,  # type: ignore[no-redef]
    )

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_bed")

MODE_LOCAL = "local"
MODE_HTTP = "generic_http"
MODE_R2 = "cloudflare_r2"
MODE_OCI = "oracle_oci"
MODE_CFB = "cloudflare_imgbed"


@dataclass
class StoreResult:
    ok: bool
    url: str | None = None  # 图床直链（若有）
    file_path: str | None = None  # 本地文件路径（若有）
    message: str = ""


def extract_json_path(data: Any, path: str) -> Any:
    """按点号路径从嵌套 JSON 中取值，支持数组下标（如 data.links.0.url）。

    约定：键名含 "." 无法表达；任何一段取不到（键缺失/下标越界/类型
    不符）即返回 None；列表支持负下标（current[-1] 取末元素）。
    """
    current = data
    for part in path.split("."):
        part = part.strip()
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _public_url(base: str, key: str) -> str:
    """拼接公开直链：base 去尾斜杠 + URI 编码后的对象名。"""
    return f"{base.rstrip('/')}/{quote(key)}"


def redact_url(url: str) -> str:
    """把带签名的直链收敛成可安全写日志的形态。

    对象存储/图床的直链常带 `?...sign=...&token=...` 这类一次性凭据：
    整条写进日志后，任何能看日志或 /errors 面板的人都拿到了可直接使用的
    下载权限。这里只保留 scheme://host/path，查询串一律换成 `?***`。
    """
    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return "<地址已隐藏>"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return text
    redacted = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{redacted}?***" if parsed.query else redacted


def _host_port(parsed) -> tuple[str, int | None]:
    """URL 的归一化 (主机名小写, 端口)，端口缺省按协议补全。"""
    try:
        port = parsed.port
    except ValueError:
        return "", None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname or "").lower(), port


def _same_origin(a, b) -> bool:
    """两个 parsed URL 是否同源（协议 + 主机 + 端口）。"""
    if a.scheme != b.scheme:
        return False
    return _host_port(a) == _host_port(b)


def _clean_folder(folder: object) -> str:
    """图床内目录名归一化：去首尾空白与首尾 "/"。

    store(folder=...) 的四种模式共用这一份归一化：CFB 的 uploadFolder、
    R2/OCI 的对象 key 前缀都按「无首尾斜杠」的形态拼接；空值表示沿用
    各模式现状（CFB 回落到 cfi_upload_folder、对象存储不加前缀）。
    """
    return str(folder or "").strip().strip("/").strip()


class ImageBedClient:
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

    # ---------- 对外接口 ----------

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
                if raw_folder:
                    url = await upload(src_path, _clean_folder(raw_folder))
                else:
                    # 保持单参调用，兼容既有单参上传实现与测试替身
                    url = await upload(src_path)
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

    # ---------- 通用 HTTP 图床 ----------

    async def _copy_local(self, src_path: str, folder: str = "") -> str:
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        name = f"{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        dest_dir = self._local_subdir(folder)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        # 文件复制放到线程里避免阻塞事件循环；大小上游已校验
        await asyncio.to_thread(shutil.copyfile, src_path, dest)
        return dest

    def _local_subdir(self, folder: str) -> str:
        """把 folder 解析为 local_dir 下的落盘目录（本地模式的越界防护）。

        folder 来自面板配置，本地模式会真的落盘：绝对路径、"../" 这类
        能逃出数据目录的取值必须在写文件前拒绝，而不是交给 makedirs。
        """
        text = str(folder or "").strip().replace("\\", "/")
        if not text:
            return self.local_dir
        if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
            raise ValueError(f"不允许使用绝对路径: {text!r}")
        segments = []
        for segment in text.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                raise ValueError(f"不允许使用上级目录: {text!r}")
            segments.append(segment)
        if not segments:
            return self.local_dir
        return os.path.join(self.local_dir, *segments)

    def _timeout(self) -> aiohttp.ClientTimeout:
        """图床 HTTP 请求的统一超时（配置缺失或非法时取 30 秒）。"""
        return aiohttp.ClientTimeout(total=max(5, as_int(self.config.get("timeout"), 30)))

    def _upload_meta(self, src_path: str) -> tuple[str, str]:
        """生成上传用的 (文件名, Content-Type)，供两种上传共用。

        不再把文件整读进内存：调用方把文件对象直接交给 aiohttp 流式发送，
        避免「先 read_bytes 再 add_field」让峰值内存达到文件大小的 2~3 倍。
        """
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        filename = f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return filename, content_type

    async def _upload(self, src_path: str, folder: str = "") -> str:
        """按配置调用通用 HTTP 上传接口，成功返回图片直链。

        通用 HTTP 图床的目录由服务端接口自身决定，folder 在此不起作用
        （store 侧已记过 DEBUG）；保留参数只为与其它上传器同签名。
        """
        del folder  # 服务端不接受目录参数
        api_url = str(self.config.get("api_url") or "").strip()
        if not api_url:
            raise ValueError("未配置图床上传接口地址 api_url")

        field = str(self.config.get("file_field") or "file").strip() or "file"
        url_path = str(self.config.get("url_path") or "data.url").strip() or "data.url"
        extra_fields = self.config.get("extra_fields") or {}
        if not isinstance(extra_fields, dict):
            extra_fields = {}

        auth_header = str(self.config.get("auth_header") or "").strip()
        token = str(self.config.get("token") or "").strip()
        auth_value = ""
        if auth_header and token:
            auth_value = f"{self.config.get('auth_prefix') or ''}{token}"

        filename, content_type = self._upload_meta(src_path)

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            # 跨域重定向会把请求交给第三方，鉴权信息一律不再附带
            if auth_value and not cross_origin:
                headers[auth_header] = auth_value
            if method != "POST":
                return {"headers": headers}
            # FormData 不可复用，每次请求（含重定向后的重发）都需重新构建；
            # 复用同一个文件句柄，重发前把读指针复位
            file_handle.seek(0)
            form = aiohttp.FormData()
            for key, value in extra_fields.items():
                form.add_field(str(key), str(value))
            form.add_field(field, file_handle, filename=filename, content_type=content_type)
            return {"data": form, "headers": headers}

        client = await self._http_getter()
        # 句柄必须活到请求体发完，所以把 request 包在 with open 里面（流式上传）
        with open(src_path, "rb") as file_handle:
            async with await client.request(
                "POST", api_url, prepare=prepare, timeout=self._timeout()
            ) as resp:
                body = await read_limited_text(resp)
                if not 200 <= resp.status < 300:
                    raise RuntimeError(f"图床返回 HTTP {resp.status}: {body[:500]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"图床响应不是 JSON: {body[:200]}") from e

        url = extract_json_path(payload, url_path)
        if not url or not str(url).startswith(("http://", "https://")):
            raise RuntimeError(f"未能从响应路径 {url_path!r} 提取到图片直链: {body[:200]}")
        return str(url)

    # ---------- CloudFlare-ImgBed（官方 REST API） ----------
    #
    # 接口文档：https://cfbed.sanyue.de/<lang>/api/（上传 /upload、
    # 删除 /api/manage/delete/batch）。

    def _cf_base(self) -> str:
        base = str(self.config.get("cfi_base_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置 CloudFlare-ImgBed 站点地址 cfi_base_url")
        return base

    async def _upload_cf(self, src_path: str, folder: str = "") -> str:
        """按官方文档调用 POST /upload（multipart，文件字段名 file）。

        鉴权二选一（可同时配置）：cfi_token 以 Authorization: Bearer
        请求头发送（Token 需 upload 权限，官方推荐格式）；cfi_auth_code
        默认以 X-Auth-Code 请求头发送（不进查询串：查询串会随 URL 进日志、
        异常和面板，等价于把上传鉴权码明文外泄）。老版本站点只认
        `?authCode=` 查询参数，被 401/403 拒绝时自动回落一次。两者均未
        配置且站点未开启登录校验时无需鉴权。uploadChannel / channelName /
        uploadFolder 为官方可选查询参数。

        响应为非空数组 [{"src": "/file/xxx.jpg", "publicUrl": "..."}]。
        直链优先取站点地址 + src（忽略 publicUrl），保证落库的链接与
        站点同源，/溯源删除 才能按前缀反解出文件路径。
        """
        base = self._cf_base()
        token = str(self.config.get("cfi_token") or "").strip()
        auth_code = str(self.config.get("cfi_auth_code") or "").strip()

        params: dict = {}
        upload_channel = str(self.config.get("cfi_upload_channel") or "").strip()
        if upload_channel:
            params["uploadChannel"] = upload_channel
        channel_name = str(self.config.get("cfi_channel_name") or "").strip()
        if channel_name:
            params["channelName"] = channel_name
        # 显式传入的 folder（缩略图走 "thumbnails/"）覆盖配置里的默认目录；
        # 未传时保持旧行为（只认 cfi_upload_folder）
        configured_folder = str(self.config.get("cfi_upload_folder") or "").strip()
        upload_folder = _clean_folder(folder) or configured_folder
        if upload_folder:
            params["uploadFolder"] = upload_folder

        api_url = f"{base}/upload"
        if params:
            api_url = f"{api_url}?{urlencode(params)}"
        # 老版本 CloudFlare-ImgBed 只认 `?authCode=` 查询参数，版本较新的还认
        # `X-Auth-Code` 请求头。默认走请求头——查询串会随 URL 进日志、异常栈和
        # 面板（等价于把上传鉴权码明文外泄）；只有站点明确拒绝时才回落到查询串，
        # 且只回落一次、只告警一次，保证老站点不会静默上传失败。
        fallback_url = ""
        if auth_code:
            fallback_url = f"{base}/upload?{urlencode({**params, 'authCode': auth_code})}"
        use_query_auth = {"on": False}

        filename, content_type = self._upload_meta(src_path)

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            if token and not cross_origin:
                headers["Authorization"] = f"Bearer {token}"
            # 上传鉴权码优先走请求头，不拼进 URL：查询串会原样出现在异常/日志里
            if auth_code and not cross_origin and not use_query_auth["on"]:
                headers["X-Auth-Code"] = auth_code
            if method != "POST":
                return {"headers": headers}
            # FormData 不可复用，每次请求（含重定向后的重发）都需重新构建；
            # 复用同一个文件句柄，重发前把读指针复位
            file_handle.seek(0)
            form = aiohttp.FormData()
            form.add_field("file", file_handle, filename=filename, content_type=content_type)
            return {"data": form, "headers": headers}

        client = await self._http_getter()
        status = 0
        body = ""
        # 句柄必须活到请求体发完，所以把 request 包在 with open 里面（流式上传）
        with open(src_path, "rb") as file_handle:
            targets = [api_url]
            if fallback_url:
                targets.append(fallback_url)
            for index, target in enumerate(targets):
                async with await client.request(
                    "POST", target, prepare=prepare, timeout=self._timeout()
                ) as resp:
                    body = await read_limited_text(resp)
                    status = resp.status
                if 200 <= status < 300:
                    break
                # 只有"鉴权被拒 + 还有查询串这条路"才值得重试一次；
                # 5xx/网络层错误回落毫无意义，只会把真正的原因冲掉。
                if not (index == 0 and fallback_url and status in (401, 403)):
                    raise RuntimeError(f"CloudFlare-ImgBed 返回 HTTP {status}: {body[:500]}")
                use_query_auth["on"] = True
                if not getattr(self, "_cf_authcode_query_warned", False):
                    self._cf_authcode_query_warned = True
                    logger.warning(
                        "图床不认 X-Auth-Code 请求头，已回落到 ?authCode= 查询串上传"
                        "（站点版本较旧；建议升级站点或改用 API Token，"
                        "鉴权码出现在 URL 里会进访问日志）"
                    )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"CloudFlare-ImgBed 响应不是 JSON: {body[:200]}") from e
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError(f"CloudFlare-ImgBed 上传响应不是非空数组: {body[:200]}")
        src = str(payload[0].get("src") or "").strip()
        if not src:
            raise RuntimeError(f"上传响应缺少 src 字段: {body[:200]}")
        url = src if src.startswith(("http://", "https://")) else f"{base}/{src.lstrip('/')}"
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"未能从上传响应提取到图片直链: {body[:200]}")
        return url

    async def _cf_delete(self, token: str, file_id: str) -> bool:
        """按官方文档调用 POST /api/manage/delete/batch 删除单个文件。"""
        base = self._cf_base()
        api_url = f"{base}/api/manage/delete/batch"
        body = json.dumps({"fileIds": [file_id]})

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            if not cross_origin:
                headers["Authorization"] = f"Bearer {token}"
            if method != "POST":
                return {"headers": headers}
            return {
                "data": body,
                "headers": {**headers, "Content-Type": "application/json"},
            }

        timeout = self._timeout()
        client = await self._http_getter()
        async with await client.request("POST", api_url, prepare=prepare, timeout=timeout) as resp:
            resp_body = await read_limited_text(resp)
            if not 200 <= resp.status < 300:
                raise RuntimeError(
                    f"CloudFlare-ImgBed 删除返回 HTTP {resp.status}: {resp_body[:500]}"
                )
        try:
            payload = json.loads(resp_body)
        except json.JSONDecodeError:
            return False
        return bool(isinstance(payload, dict) and payload.get("success"))

    # ---------- 对象存储（R2 / OCI，S3 兼容 API） ----------

    @staticmethod
    def _object_name(src_path: str) -> tuple[str, str]:
        """生成对象名与 Content-Type（形如 trace_20260906_ab12cd34ef56ab78.jpg）。"""
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        key = f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return key, ctype

    @staticmethod
    def _require(*pairs) -> None:
        missing = [name for value, name in pairs if not str(value or "").strip()]
        if missing:
            raise ValueError(f"缺少必填配置: {'、'.join(missing)}")

    def _r2_client(self) -> tuple[S3CompatClient, str]:
        cached = self._s3_clients.get("r2")
        if cached is not None:
            return cached
        cfg = self.config
        account_id = str(cfg.get("r2_account_id") or "").strip()
        access_key = str(cfg.get("r2_access_key_id") or "").strip()
        secret_key = str(cfg.get("r2_secret_access_key") or "").strip()
        bucket = str(cfg.get("r2_bucket") or "").strip()
        self._require(
            (account_id, "r2_account_id"),
            (access_key, "r2_access_key_id"),
            (secret_key, "r2_secret_access_key"),
            (bucket, "r2_bucket"),
        )
        # 官方默认 endpoint；欧盟等司法区可在 r2_endpoint 中覆盖
        endpoint = (
            str(cfg.get("r2_endpoint") or "").strip()
            or f"https://{account_id}.r2.cloudflarestorage.com"
        )
        client = S3CompatClient(
            endpoint,
            access_key,
            secret_key,
            region="auto",  # R2 官方文档：签名 region 固定为 auto
            timeout=max(5, as_int(cfg.get("timeout"), 30)),
            http_getter=self._http_getter,
        )
        self._s3_clients["r2"] = (client, bucket)
        return client, bucket

    def _oci_client(self) -> tuple[S3CompatClient, str]:
        cached = self._s3_clients.get("oci")
        if cached is not None:
            return cached
        cfg = self.config
        namespace = str(cfg.get("oci_namespace") or "").strip()
        region = str(cfg.get("oci_region") or "").strip()
        access_key = str(cfg.get("oci_access_key_id") or "").strip()
        secret_key = str(cfg.get("oci_secret_access_key") or "").strip()
        bucket = str(cfg.get("oci_bucket") or "").strip()
        self._require(
            (namespace, "oci_namespace"),
            (region, "oci_region"),
            (access_key, "oci_access_key_id"),
            (secret_key, "oci_secret_access_key"),
            (bucket, "oci_bucket"),
        )
        # 官方 S3 兼容 API endpoint（仅支持 path-style）；
        # 特殊 realm 场景可用 oci_endpoint 覆盖
        endpoint = (
            str(cfg.get("oci_endpoint") or "").strip()
            or f"https://{namespace}.compat.objectstorage.{region}.oraclecloud.com"
        )
        client = S3CompatClient(
            endpoint,
            access_key,
            secret_key,
            region=region,  # 签名 region 使用 OCI 区域标识
            timeout=max(5, as_int(cfg.get("timeout"), 30)),
            http_getter=self._http_getter,
        )
        self._s3_clients["oci"] = (client, bucket)
        return client, bucket

    def _oci_public_url(self, key: str) -> str:
        """OCI 公开直链：自定义地址（含前缀 PAR）优先，公共桶官方 URL 兜底。

        两者都不可用时抛错，由 store() 回退本地副本。
        """
        cfg = self.config
        custom = self._public_base("oci_public_base_url", "oci_public_base_url")
        if custom:
            return _public_url(custom, key)
        if self._bool_cfg(cfg.get("oci_public_bucket")):
            namespace = str(cfg.get("oci_namespace") or "").strip()
            region = str(cfg.get("oci_region") or "").strip()
            bucket = str(cfg.get("oci_bucket") or "").strip()
            self._require(
                (namespace, "oci_namespace"),
                (region, "oci_region"),
                (bucket, "oci_bucket"),
            )
            # 官方文档的对象 URL 格式（公共存储桶可直接匿名读取）
            return (
                f"https://objectstorage.{region}.oraclecloud.com"
                f"/n/{namespace}/b/{bucket}/o/{quote(key)}"
            )
        raise ValueError(
            "未配置公开访问方式：请设置 oci_public_base_url（自定义地址或"
            "带前缀的 PAR 地址），或开启 oci_public_bucket（公共存储桶）"
        )

    @staticmethod
    def _bool_cfg(value) -> bool:
        return truthy(value)

    def _oci_public_hosts(self) -> set:
        """OCI 公开直链允许的主机名集合（按当前配置推导）。"""
        hosts = set()
        region = str(self.config.get("oci_region") or "").strip()
        if region:
            hosts.add(f"objectstorage.{region}.oraclecloud.com")
        endpoint = str(self.config.get("oci_endpoint") or "").strip()
        if endpoint:
            host = urlparse(endpoint).hostname
            if host:
                hosts.add(host.lower())
        return hosts

    def _oci_key_from_url(self, image_url: str) -> tuple[str, bool]:
        """从 OCI 公开直链反解对象名；仅接受与当前配置同源的 URL。

        必须校验主机：否则任何形如 https://任意域名/n/<命名空间>/b/<桶>/o/<对象名>
        的历史 URL 都能触发对配置存储桶内对象的删除。
        """
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "", False
        # 用 parsed.path 而非原始字符串，顺带剥掉 query/fragment，
        # 避免把 "对象名?token=..." 当成对象名去删除
        key_path = parsed.path

        custom = self._public_base("oci_public_base_url", "oci_public_base_url")
        if custom:
            base_parsed = urlparse(custom)
            if _same_origin(parsed, base_parsed):
                prefix = base_parsed.path.rstrip("/") + "/"
                if key_path.startswith(prefix):
                    key = unquote(key_path[len(prefix) :])
                    if key:
                        return key, True

        if self._bool_cfg(self.config.get("oci_public_bucket")):
            namespace = str(self.config.get("oci_namespace") or "").strip()
            bucket = str(self.config.get("oci_bucket") or "").strip()
            if (parsed.hostname or "").lower() not in self._oci_public_hosts():
                return "", False
            parts = key_path.split("/o/", 1)
            if len(parts) == 2 and key_path.startswith("/n/"):
                seg = parts[0].strip("/").split("/")
                # /n/{namespace}/b/{bucket}/o/{key}
                if (
                    len(seg) == 4
                    and seg[0] == "n"
                    and seg[1] == namespace
                    and seg[2] == "b"
                    and seg[3] == bucket
                ):
                    key = unquote(parts[1])
                    if key:
                        return key, True
        return "", False

    async def _put_object_bytes(
        self,
        client: S3CompatClient,
        bucket: str,
        src_path: str,
        folder: str = "",
    ) -> str:
        """把文件上传为新的对象名，返回对象键（R2/OCI 共用）。

        folder 非空时作为对象键前缀（缩略图走 "thumbnails/"）：同一存储桶里
        原图与缩略图分开存放，按前缀批量清理/统计才有依据。
        """
        key, ctype = self._object_name(src_path)
        prefix = _clean_folder(folder)
        if prefix:
            key = f"{prefix}/{key}"
        data = await asyncio.to_thread(read_bytes, src_path)
        await client.put_object(bucket, key, data, ctype)
        return key

    async def _upload_object(self, src_path: str, folder: str = "") -> str:
        """上传到 R2 / OCI 对象存储，返回长期有效的公开直链。"""
        if self.mode == MODE_R2:
            client, bucket = self._r2_client()
            public_base = self._public_base("r2_public_base_url", "r2_public_base_url")
            if not public_base:
                raise ValueError(
                    "未配置有效的 r2_public_base_url（需在 Cloudflare 侧为存储桶开启"
                    " r2.dev 公开访问或绑定自定义域名，https:// 开头），溯源回图"
                    "需要长期有效的直链"
                )
            key = await self._put_object_bytes(client, bucket, src_path, folder)
            return _public_url(public_base, key)

        # MODE_OCI
        client, bucket = self._oci_client()
        return self._oci_public_url(await self._put_object_bytes(client, bucket, src_path, folder))
