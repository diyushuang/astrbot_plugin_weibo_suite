"""本地副本与通用 HTTP 图床后端。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import shutil
import time

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("storage_local")

try:
    from ..shared.common import (
        USER_AGENT,
    )
    from ..shared.http import read_limited_text
    from .storage_utils import extract_json_path
except ImportError:  # 兼容插件以独立模块方式加载
    from storage_utils import extract_json_path  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        USER_AGENT,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        read_limited_text,
    )


class LocalHttpBackendMixin:
    """依赖宿主 ImageBedClient 上的 local_dir / config / _http_getter。"""

    async def _copy_local(self, src_path: str, folder: str = "") -> str:
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        name = f"{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        dest_dir = self._local_subdir(folder)
        # mkdir 也是阻塞 syscall：网络图床主机/慢盘上同样会卡事件循环
        await asyncio.to_thread(os.makedirs, dest_dir, exist_ok=True)
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
