"""R2 / OCI 对象存储后端（S3 兼容 API）。"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import secrets
import time
from urllib.parse import quote, unquote, urlparse

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("storage_object")

try:
    from ..shared.common import as_int, truthy
    from .s3_store import S3CompatClient
    from .storage_utils import (
        MODE_R2,
        sha256_of_file,
    )
    from .storage_utils import (
        clean_folder as _clean_folder,
    )
    from .storage_utils import (
        public_url as _public_url,
    )
    from .storage_utils import (
        same_origin as _same_origin,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from s3_store import S3CompatClient  # type: ignore[no-redef]
    from storage_utils import (  # type: ignore[no-redef]
        MODE_R2,
        sha256_of_file,
    )
    from storage_utils import (
        clean_folder as _clean_folder,
    )
    from storage_utils import (
        public_url as _public_url,
    )
    from storage_utils import (
        same_origin as _same_origin,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        as_int,
        truthy,
    )


class ObjectStorageMixin:
    """依赖宿主 ImageBedClient 上的 config / mode / _s3_clients / _public_base。"""

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
        # 官方 S3 兼容 API endpoint（仅支持 path-style）：新 OCI 区域使用
        # `oci.customer-oci.com` 域，旧区域仍可用 `oraclecloud.com`，用
        # oci_endpoint 覆盖即可（不覆盖时按新域拼）
        endpoint = (
            str(cfg.get("oci_endpoint") or "").strip()
            or f"https://{namespace}.compat.objectstorage.{region}.oci.customer-oci.com"
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
            # 官方文档的对象 URL 格式（公共存储桶可直接匿名读取）；新区域用
            # `oci.customer-oci.com` 域，旧区域用 `oraclecloud.com`（历史链接
            # 仍可反解删除，见 _oci_public_hosts）
            return (
                f"https://objectstorage.{region}.oci.customer-oci.com"
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
        """OCI 公开直链允许的主机名集合（按当前配置推导）。

        同时收新域（`oci.customer-oci.com`）与旧域（`oraclecloud.com`）：
        默认生成新域，但历史落库的旧域链接仍要能被 /溯源删除 反解。
        """
        hosts = set()
        region = str(self.config.get("oci_region") or "").strip()
        namespace = str(self.config.get("oci_namespace") or "").strip()
        if region:
            hosts.add(f"objectstorage.{region}.oci.customer-oci.com")
            hosts.add(f"objectstorage.{region}.oraclecloud.com")
        if region and namespace:
            hosts.add(f"{namespace}.compat.objectstorage.{region}.oci.customer-oci.com")
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

    async def _put_object(
        self,
        client: S3CompatClient,
        bucket: str,
        src_path: str,
        folder: str = "",
    ) -> str:
        """把文件上传为新的对象名，返回对象键（R2/OCI 共用）。

        folder 非空时作为对象键前缀（缩略图走 "thumbnails/"）：同一存储桶里
        原图与缩略图分开存放，按前缀批量清理/统计才有依据。

        大文件（视频/原图）不整体读进内存：sha256 分块计算，请求体直接
        用文件句柄流式上传。R2 上单文件可达几十 MB，`read_bytes` 的峰值
        内存等于文件大小。
        """
        key, ctype = self._object_name(src_path)
        prefix = _clean_folder(folder)
        if prefix:
            key = f"{prefix}/{key}"
        digest = await asyncio.to_thread(sha256_of_file, src_path)
        await client.put_object_file(bucket, key, src_path, ctype, digest)
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
            key = await self._put_object(client, bucket, src_path, folder)
            return _public_url(public_base, key)

        # MODE_OCI
        client, bucket = self._oci_client()
        return self._oci_public_url(await self._put_object(client, bucket, src_path, folder))
