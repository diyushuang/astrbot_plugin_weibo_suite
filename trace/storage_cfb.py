"""CloudFlare-ImgBed 官方 REST API 后端（上传 / 删除）。"""

from __future__ import annotations

import json
from urllib.parse import urlencode

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("storage_cfb")

try:
    from ..shared.common import USER_AGENT
    from ..shared.http import read_limited_text
    from .storage_utils import clean_folder
except ImportError:  # 兼容插件以独立模块方式加载
    from storage_utils import clean_folder  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.common import USER_AGENT  # type: ignore[no-redef]
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        read_limited_text,
    )


class CloudflareImgBedMixin:
    """依赖宿主 ImageBedClient 上的 config / _http_getter / _timeout / _upload_meta。"""

    def _cf_base(self) -> str:
        base = str(self.config.get("cfi_base_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置 CloudFlare-ImgBed 站点地址 cfi_base_url")
        return base

    async def _upload_cf(self, src_path: str, folder: str = "") -> str:
        """按官方文档调用 POST /upload（multipart，文件字段名 file）。

        鉴权二选一（可同时配置）：cfi_token 以 Authorization: Bearer
        请求头发送（Token 需 upload 权限，官方推荐格式）；cfi_auth_code
        默认以 `authCode` 请求头发送（不是 `X-Auth-Code`：上游站点读的是
        前者，写错头名会被当成"未鉴权"直接 401；也不进查询串，避免查询串
        随 URL 进日志、异常和面板，等价于把上传鉴权码明文外泄）。老版本
        站点只认 `?authCode=` 查询参数，被 401/403 拒绝时自动回落一次。
        两者均未配置且站点未开启登录校验时无需鉴权。uploadChannel /
        channelName / uploadFolder 为官方可选查询参数。

        响应为非空数组 [{"src": "/file/xxx.jpg", "publicUrl": "..."}]。
        直链优先用响应里的 `publicUrl`（站点绑定自定义域名时 `src` 只是
        相对路径，站点域名拼出来的链接可能不可达）；`publicUrl` 缺失或不是
        绝对地址时，才回落到站点地址 + src。
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
        upload_folder = clean_folder(folder) or configured_folder
        if upload_folder:
            params["uploadFolder"] = upload_folder

        api_url = f"{base}/upload"
        if params:
            api_url = f"{api_url}?{urlencode(params)}"
        # 老版本 CloudFlare-ImgBed 只认 `?authCode=` 查询参数，版本较新的认
        # `authCode` 请求头（上游读的就是这个名字；曾用 `X-Auth-Code`，站点
        # 侧不认，表现为"配了鉴权码仍然 401"）。默认走请求头——查询串会随
        # URL 进日志、异常栈和面板（等价于把上传鉴权码明文外泄）；只有站点
        # 明确拒绝时才回落到查询串，且只回落一次、只告警一次。
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
                headers["authCode"] = auth_code
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
                        "图床不认 authCode 请求头，已回落到 ?authCode= 查询串上传"
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
        public_url = str(payload[0].get("publicUrl") or "").strip()
        if public_url.startswith(("http://", "https://")):
            # 站点绑了自定义域名/CDN 时，src 的相对路径拼站点域名未必可达；
            # 官方响应给了绝对 publicUrl 就直接用
            return public_url
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
