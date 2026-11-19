"""向量检索引擎（图片溯源双引擎之“向量”）。

查询图片 -> 配置的多模态向量 AI（OpenAI 兼容 /v1/embeddings）生成向量
-> Qdrant REST 检索最相似的图床图片（相似度 = Qdrant cosine score）。

实现按职责拆成三个模块，本模块保留配置解析、HTTP 传输与集合校验：
- `embedding_client.py`：embedding 输入序列化、响应解析与图片向量化；
- `qdrant_client.py`：Qdrant 检索、反查与登记同步；
- `vector_errors.py`：三个模块共用的异常类型。

出网请求一律经 url_guard.guarded_request 逐跳 SSRF 校验 + IP 钉扎
（qdrant_url / embed_base_url 必须是公网可达的 http/https 地址）。
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("vector_search")

try:
    from ..shared.common import (
        as_float,
        as_int,
    )
    from ..shared.http import (
        HttpClientGetter,
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )
    from .embedding_client import (
        IMAGE_INPUTS,
        EmbeddingMixin,
        _invalid_json_context,
        parse_embedding_response,
    )
    from .qdrant_client import (
        BATCH_EMBED_CONCURRENCY,
        DUPLICATE_SCAN_CAP,
        QdrantMixin,
        VectorPointInput,
        thumb_path_key,
    )
    from .vector_errors import HttpError, InvalidEmbeddingJSON, VectorEngineError
except ImportError:  # 兼容插件以独立模块方式加载
    from embedding_client import (  # type: ignore[no-redef]
        IMAGE_INPUTS,
        EmbeddingMixin,
        _invalid_json_context,
        parse_embedding_response,
    )
    from qdrant_client import (  # type: ignore[no-redef]
        BATCH_EMBED_CONCURRENCY,
        DUPLICATE_SCAN_CAP,
        QdrantMixin,
        VectorPointInput,
        thumb_path_key,
    )
    from vector_errors import (  # type: ignore[no-redef]
        HttpError,
        InvalidEmbeddingJSON,
        VectorEngineError,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        as_float,
        as_int,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        HttpClientGetter,  # type: ignore[no-redef]
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )


class VectorEngine(EmbeddingMixin, QdrantMixin):
    """Qdrant + 多模态 embedding 的只读检索与登记同步客户端。"""

    def __init__(
        self,
        config,
        http_getter: HttpClientGetter,
    ):
        """http_getter: 异步可调用，返回共享 GuardedHttpClient。"""
        raw = config.get("vector_search")
        if not isinstance(raw, dict):
            raw = {}
        self.config_raw = raw
        self.qdrant_url = self._normalize_base_url(raw.get("qdrant_url"))
        self.qdrant_key = str(raw.get("qdrant_api_key") or "")
        self.collection = str(raw.get("collection_name") or "imgbed_images")
        self.embed_base_url = self._normalize_base_url(raw.get("embed_base_url"))
        self.embed_key = str(raw.get("embed_api_key") or "")
        self.embed_model = str(raw.get("embed_model") or "")
        self.image_input = str(raw.get("embed_image_input") or "nemotron-vl")
        if self.image_input not in IMAGE_INPUTS:
            # 配置错误在加载期就暴露（日志可见），而不是拖到第一次请求才失败
            logger.warning(
                f"embed_image_input 配置无效: {self.image_input!r}，"
                f"可选 {' / '.join(IMAGE_INPUTS)}；已回退为 nemotron-vl。"
            )
            self.image_input = "nemotron-vl"
        # 仅 nemotron-vl 的非对称模型区分 input_type；图片侧只能走 passage，
        # 这里保留配置值供诊断展示，实际请求强制 passage（见 embed_bytes）
        self.input_type = str(raw.get("embed_input_type") or "passage")
        if self.input_type not in ("query", "passage"):
            self.input_type = "passage"
        self.threshold = max(0.0, min(1.0, as_float(raw.get("similarity_threshold"), 0.80)))
        self.duplicate_threshold = max(
            0.0,
            min(1.0, as_float(raw.get("duplicate_vector_threshold"), 0.995)),
        )
        self.top_k = max(1, min(50, as_int(raw.get("top_k"), 5)))
        timeout_s = max(5, as_int(raw.get("request_timeout"), 30))
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._http_getter = http_getter
        # embed_api_key 不参与启用判定：网关未开鉴权时留空是合法场景
        self.enabled = bool(self.qdrant_url and self.embed_base_url and self.embed_model)
        # 反查通道（按 thumb_url / file_name 查 payload）只依赖 Qdrant、不碰
        # embed，因此启用判定必须与 enabled 解耦：只配了 Qdrant、缺 embed 时
        # enabled=False，但反查必须仍然可用。2026-10-20 线上排查实录：另一台
        # AstrBot 实例（v4.29.1）配了 Qdrant 却因 enabled 一刀切为 False，
        # 随机图命中缩略图后的反查全部静默失败，warning 里又无线索可查。
        self.qdrant_ready = bool(self.qdrant_url)
        # 集合元信息只在首次检索/登记前取一次：维度/距离度量在集合生命周期内
        # 不变，每次请求都 GET /collections/{name} 是纯浪费。
        self._collection_meta: dict | None = None
        self._collection_lock = asyncio.Lock()
        self._index_warned = False

    @staticmethod
    def _normalize_base_url(value: object) -> str:
        base_url = str(value or "").strip().rstrip("/")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            logger.warning("忽略非法向量服务地址（必须以 http/https 开头）")
            return ""
        return base_url

    @staticmethod
    def _debug_disabled(action: str) -> None:
        """反查入口不可用时的统一 DEBUG 说明。

        这些入口长期「静默返回 None」：调用方的 warning 只说「反查不到」，
        根因（Qdrant 未配置）连 DEBUG 日志里都找不到——2026-10-20 跨实例排查
        时只能逐台实测。统一留一行 DEBUG，把「为什么没查」说清楚。
        """
        logger.debug(
            f"向量引擎不可用（Qdrant 未配置），{action}按未命中处理；"
            "补齐 vector_search.qdrant_url 并重启插件后生效"
        )

    # ------------------------------------------------------------------
    # 配置诊断
    # ------------------------------------------------------------------

    def missing_fields(self) -> list:
        """返回未配置的必填项名称列表（供提示文案使用）。

        qdrant_api_key / embed_api_key 不列入：自托管无鉴权的 Qdrant、
        未开鉴权的 embedding 网关都是合法场景（与 __init__ 的 enabled
        判定口径一致）。
        """
        missing = []
        if not self.qdrant_url:
            missing.append("vector_search.qdrant_url")
        if not self.embed_base_url:
            missing.append("vector_search.embed_base_url")
        if not self.embed_model:
            missing.append("vector_search.embed_model")
        raw_qdrant_url = str(self.config_raw.get("qdrant_url") or "").strip()
        raw_embed_url = str(self.config_raw.get("embed_base_url") or "").strip()
        if raw_qdrant_url and not self.qdrant_url:
            missing.append("vector_search.qdrant_url（必须以 http/https 开头）")
        if raw_embed_url and not self.embed_base_url:
            missing.append("vector_search.embed_base_url（必须以 http/https 开头）")
        return missing

    def config_hint(self) -> str:
        return (
            "⚠️ 向量引擎未启用：请在插件配置的 vector_search 一节补齐："
            + "、".join(self.missing_fields() or ["（请检查 search_engine 设置）"])
            + "，然后在 WebUI 重启插件。"
        )

    # ------------------------------------------------------------------
    # 基础请求（一律走 SSRF 校验）
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
        max_bytes: int | None = None,
    ):
        client = await self._http_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            return {
                "json": json_body,
                # 跨域重定向到第三方时不再携带 api-key 等鉴权头
                "headers": {} if cross_origin else (headers or {}),
                "timeout": self.timeout,
            }

        try:
            async with await client.request(method, url, prepare=prepare) as resp:
                text = (
                    await read_limited_text(resp, max_bytes)
                    if max_bytes
                    else await read_limited_text(resp)
                )
                status = resp.status
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(
                "目标地址被安全策略拒绝",
                detail=f"目标地址被安全策略拒绝: {e}",
            ) from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(
                "网络请求失败（详情见日志）",
                detail=f"网络请求失败: {url}: {e}",
            ) from e
        return status, text

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
        max_bytes: int | None = None,
    ):
        status, text = await self._request(
            method, url, json_body=json_body, headers=headers, max_bytes=max_bytes
        )
        if status != 200:
            lowered = str(text or "").lower()
            # Qdrant 对维度不匹配只回 400 + 一句 "Vector dimension error"，
            # 直接抛 HttpError 时用户/面板只看到「服务返回 HTTP 400」，完全
            # 不知道是 embedding 维度配错了
            if "vector dimension error" in lowered or "wrong vector dimension" in lowered:
                raise VectorEngineError(
                    "向量维度不匹配：Qdrant 集合要求的维度与当前 embedding 输出不一致。"
                    "请让入库侧与查询侧使用同一个多模态模型，或按新维度重建集合。",
                    detail=f"HTTP {status}: {text[:500]}",
                )
            raise HttpError(status, text)
        try:
            return json.loads(text)
        except ValueError as e:
            raise VectorEngineError(
                "服务响应不是合法 JSON",
                detail=f"响应不是合法 JSON: {text[:500]}",
            ) from e

    async def _collection_config(self) -> dict:
        """取（并缓存）集合元信息；集合不存在给出可读错误。"""
        if self._collection_meta is not None:
            return self._collection_meta
        async with self._collection_lock:
            if self._collection_meta is None:
                try:
                    obj = await self._request_json(
                        "GET",
                        f"{self.qdrant_url}/collections/{quote(self.collection, safe='')}",
                        headers=self._qd_headers(),
                    )
                except HttpError as exc:
                    if exc.status == 404:
                        raise VectorEngineError(
                            f"Qdrant 集合 {self.collection} 不存在：请先在 Qdrant 侧创建"
                            "集合（或核对 vector_search.collection_name）",
                            detail=exc.detail,
                        ) from exc
                    raise
                result = obj.get("result") if isinstance(obj, dict) else None
                self._collection_meta = result if isinstance(result, dict) else {}
        return self._collection_meta

    async def _ensure_collection(self, dim: int) -> None:
        """首次检索/登记前校验集合的向量维度与距离度量。

        只靠服务端 400 报错是不够的：Qdrant 的响应体只写一句
        `Vector dimension error`，面板和日志里用户看到的往往是「HTTP 400」。
        这里主动 GET 集合配置，把「维度要求 N / 实际 M」和「距离不是 Cosine」
        直接翻译成可执行建议。校验结果缓存，集合生命周期内只查一次。
        """
        if not self.qdrant_ready or int(dim or 0) <= 0:
            return
        meta = await self._collection_config()
        config = meta.get("config") if isinstance(meta.get("config"), dict) else {}
        params = config.get("params") if isinstance(config.get("params"), dict) else {}
        vectors = params.get("vectors")
        size = 0
        distance = ""
        if isinstance(vectors, dict):
            if isinstance(vectors.get("size"), int):
                size = int(vectors["size"])
                distance = str(vectors.get("distance") or "")
            else:
                # 命名向量：任一命名向量的 size/distance 都足以诊断配置错误
                for named in vectors.values():
                    if isinstance(named, dict) and isinstance(named.get("size"), int):
                        size = int(named["size"])
                        distance = str(named.get("distance") or "")
                        break
        if size and size != int(dim):
            raise VectorEngineError(
                f"向量维度不匹配：集合 {self.collection} 要求 {size} 维，当前 embedding"
                f" 输出 {int(dim)} 维。请让入库侧与查询侧使用同一个多模态模型，"
                "或按新维度重建集合。"
            )
        if distance and distance.lower() != "cosine":
            raise VectorEngineError(
                f"集合 {self.collection} 的距离度量是 {distance}，不是 Cosine；"
                "相似度分数与本插件的阈值不可比，请在 Qdrant 重建集合为 Cosine。"
            )

    def _warn_missing_index(self, label: str, exc: Exception) -> None:
        """反查命中 0 条时，把「缺索引」与「真的没有」区分开。

        Qdrant 对未建索引的 `match.text` / `match.value` 过滤回 400
        `Index required but not found`；原实现把它当普通异常打进 DEBUG，
        于是所有按缩略图/文件名反查都长期静默「未命中」。
        """
        detail = str(getattr(exc, "detail", "") or exc).lower()
        if "index required" not in detail and "index not found" not in detail:
            return
        if self._index_warned:
            return
        self._index_warned = True
        logger.warning(
            "Qdrant 集合缺少反查所需索引（%s）：请为 payload.thumb_url 建 full-text"
            " 索引、为 payload.file_name 建 keyword 索引，否则按缩略图/文件名反查"
            "会一直返回 0 条。详情：%s",
            label,
            exc,
        )


__all__ = [
    "BATCH_EMBED_CONCURRENCY",
    "DUPLICATE_SCAN_CAP",
    "IMAGE_INPUTS",
    "HttpError",
    "InvalidEmbeddingJSON",
    "VectorEngine",
    "VectorEngineError",
    "VectorPointInput",
    "_invalid_json_context",
    "parse_embedding_response",
    "thumb_path_key",
]
