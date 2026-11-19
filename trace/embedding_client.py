"""多模态 embedding 客户端：图片字节 → OpenAI 兼容 /v1/embeddings 向量。

与服务器侧图床入库服务使用同一套输入序列化，保证同一向量空间：
- embed_image_input=nemotron-vl:  裸 dataURL 字符串 + input_type（非对称模型，默认；
  NVIDIA llama-nemotron-embed-vl 系只接受这一种）
- embed_image_input=qwen-vl:      content 数组 [{"type":"image","image":<dataURL>}]
  （仅 Qwen3-VL-Embedding 系需要）
- embed_image_input=dataurl:      裸 dataURL 字符串
- embed_image_input=jina-image:   [{"image":<base64 无前缀>}]
"""

from __future__ import annotations

import asyncio
import base64
import json
import math

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("embedding_client")

try:
    from ..shared.common import (
        ADDRESS_PLACEHOLDER,
        CREDENTIAL_PLACEHOLDER,
        read_bytes,
        sanitize_error_text,
    )
    from ..shared.http import (
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )
    from .features import image_mime
    from .image_bed import redact_url
    from .vector_errors import InvalidEmbeddingJSON, VectorEngineError
except ImportError:  # 兼容插件以独立模块方式加载
    from features import image_mime  # type: ignore[no-redef]
    from image_bed import redact_url  # type: ignore[no-redef]
    from vector_errors import (  # type: ignore[no-redef]
        InvalidEmbeddingJSON,
        VectorEngineError,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        ADDRESS_PLACEHOLDER,
        CREDENTIAL_PLACEHOLDER,
        read_bytes,
        sanitize_error_text,
    )
    from astrbot_plugin_weibo_suite.shared.http import (  # type: ignore[no-redef]
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )


IMAGE_INPUTS = ("qwen-vl", "nemotron-vl", "dataurl", "jina-image")

_PLACEHOLDER_TOKENS = (ADDRESS_PLACEHOLDER, CREDENTIAL_PLACEHOLDER)


def _sanitized_position(body: str, position: int, sanitized_len: int) -> int:
    """把原文位置映射到整段脱敏后的文本位置。

    跨窗口边界的长 URL 或裸 ``key=value`` 在「整段脱敏」与「只脱敏前缀」时都会
    被整体替换成同一占位符，长度差只可能来自边界处空白折叠，属于几个字符的
    误差，对诊断窗口足够。前缀为空（全是空白）时直接钳到 0，避免 empty 输入
    拿到「未知错误」占位文本。
    """
    if position <= 0 or not body[:position].strip():
        return 0
    # limit=None：裸凭据替换会加长文本，按原文长度截断会切掉尾部、让映射落空
    prefix = sanitize_error_text(body[:position], limit=None)
    return max(0, min(len(prefix), sanitized_len))


def _align_past_placeholder(sanitized: str, anchor: int) -> int:
    """锚点落在占位符内部时挪到它后面，避免窗口从半个占位符开始。"""
    for token in _PLACEHOLDER_TOKENS:
        start = sanitized.rfind(token, 0, anchor + len(token))
        if start != -1 and start + len(token) > anchor:
            anchor = start + len(token)
    return anchor


def _invalid_json_context(text: str, position: int | None, window: int = 80) -> tuple[str, str]:
    """取解析失败位置前后各 window 字的片段，并复用用户可见文案的脱敏规则。

    响应体可能来自反代/网关（HTML 错误页、带签名的跳转串），片段会写进
    errors.log 并被 /errors 面板读出来，所以**先整段脱敏、再切窗口**：
    窗口起点一旦落在长 URL 中间，片段里没有 scheme，正则匹配不到，
    ``X-Amz-Signature`` 之类的查询串会原样落进日志（审查时已实测复现）。
    脱敏会改写长度（URL / 裸 ``key=value`` → 占位符），因此位置先用原前缀长度
    映射，再把锚点对齐到占位符之后，避免窗口从半个占位符开始。
    """
    body = str(text or "")
    if not body:
        return "", ""
    pos = len(body) if position is None else max(0, min(int(position), len(body)))
    # limit=None：裸凭据替换会加长文本，按原文长度截断会切掉尾部
    sanitized = sanitize_error_text(body, limit=None)
    anchor = _align_past_placeholder(sanitized, _sanitized_position(body, pos, len(sanitized)))
    start = max(0, anchor - window)
    end = min(len(sanitized), anchor + window)
    return sanitized[start:anchor], sanitized[anchor:end]


def parse_embedding_response(text: str) -> list[float]:
    """解析并校验 OpenAI 兼容 embeddings 响应中的 data[0].embedding。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidEmbeddingJSON(str(exc), exc.pos) from exc

    try:
        embedding = obj["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("embedding 响应缺少 data[0].embedding") from exc
    if not isinstance(embedding, list) or not embedding:
        raise ValueError("embedding 响应缺少 data[0].embedding")
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
        for value in embedding
    ):
        raise ValueError("embedding 结果含非数值或非有限数值元素")
    return [float(value) for value in embedding]


def _mime_for(path: str) -> str:
    return image_mime(path) or "image/jpeg"


class EmbeddingMixin:
    """依赖宿主 VectorEngine 上的 embed_* / timeout / _http_getter 配置。"""

    def _build_input(self, data_url: str, b64: str):
        if self.image_input == "qwen-vl":
            return [{"type": "image", "image": data_url}]
        if self.image_input == "nemotron-vl":
            # NVIDIA llama-nemotron-embed-vl 系：dataURL 字符串 + input_type
            # （非对称模型，图片只能走 passage 侧；入库与查询两侧必须一致）
            return data_url
        if self.image_input == "dataurl":
            return data_url
        if self.image_input == "jina-image":
            return [{"image": b64}]
        raise VectorEngineError(
            f"不支持的 embed_image_input={self.image_input}"
            "（可选 qwen-vl / nemotron-vl / dataurl / jina-image）"
        )

    async def embed_bytes(self, data: bytes, mime: str) -> list:
        """把图片字节向量化，返回浮点向量列表。

        请求按 embed_image_input 序列化；网络/响应/解析错误统一归为
        VectorEngineError（可展示给用户），429 带 retry-after 提示。
        """
        if not self.embed_base_url:
            raise VectorEngineError("embed_base_url 未配置，无法向量化图片。")
        if not self.embed_model:
            raise VectorEngineError("embed_model 未配置，无法向量化图片。")
        b64 = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        payload = {
            "model": self.embed_model,
            "input": self._build_input(data_url, b64),
            "encoding_format": "float",
        }
        if self.image_input == "nemotron-vl":
            # 非对称模型：图片只能走 passage 侧（query 侧拒绝图片输入），
            # 与图床入库服务的图片 embed 侧保持一致
            payload["input_type"] = "passage"
        url = f"{self.embed_base_url}/embeddings"
        client = await self._http_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            # 未配置 embed_api_key 时不带鉴权头；跨域重定向时一律剥离
            headers = (
                {}
                if cross_origin
                else ({"Authorization": f"Bearer {self.embed_key}"} if self.embed_key else {})
            )
            return {
                "json": payload,
                "headers": headers,
                "timeout": self.timeout,
            }

        try:
            for attempt in (1, 2):
                async with await client.request("POST", url, prepare=prepare) as resp:
                    text = await read_limited_text(resp)
                    if resp.status != 200:
                        if resp.status == 429:
                            # NVIDIA 限流：给出冷静提示，避免插件侧重复请求加重流控
                            try:
                                retry_after = max(
                                    1, int(float(resp.headers.get("retry-after") or 5))
                                )
                            except (TypeError, ValueError):
                                retry_after = 5
                            raise VectorEngineError(
                                f"向量服务限流（HTTP 429），请在约 {retry_after} 秒后重试"
                            )
                        mismatch_hint = "'dict' object has no attribute" in text
                        if (
                            resp.status in (500, 502, 503, 504)
                            and attempt == 1
                            and not mismatch_hint
                        ):
                            # 部分网关对中等体积请求会偶发 500/502（如 NVIDIA 的
                            # "Missing request extension"），短暂退避后单次重试；
                            # 格式不匹配类 500 是确定性错误，重试无意义
                            await asyncio.sleep(2)
                            continue
                        hint = ""
                        if resp.status == 400:
                            if "cannot identify image" in text.lower():
                                hint = (
                                    "；图片内容无效（下载不完整、链接已过期或格式不受支持），"
                                    "请重新发送图片再试"
                                )
                            else:
                                hint = (
                                    "；模型拒绝该图片输入，请核对 embed_model 是否支持图片，"
                                    "或调整 embed_image_input"
                                    "（qwen-vl / nemotron-vl / dataurl / jina-image）"
                                )
                        elif mismatch_hint:
                            # 服务端把 input 逐项当字符串处理却收到 content 数组字典：
                            # embed_image_input 的序列化格式与该模型实际接受的不一致
                            hint = (
                                "；embedding 服务把 input 当纯文本解析，"
                                "embed_image_input 与该模型不匹配，"
                                "请改成模型/入库侧实际使用的格式（如 nemotron-vl / dataurl）"
                            )
                        raise VectorEngineError(
                            f"向量服务 HTTP {resp.status}{hint}",
                            # 响应体可能回显图片 data URL / 内网地址：只留长度
                            detail=f"embedding HTTP {resp.status}: 响应长度={len(text)}",
                        )
                    try:
                        emb = parse_embedding_response(text)
                    except InvalidEmbeddingJSON as exc:
                        # 只记长度/状态/解析位置与脱敏后的片段：响应体可能回显
                        # 内网地址与请求内容，进 errors.log 后会被 /errors 面板读出来
                        before, after = _invalid_json_context(text, exc.position)
                        logger.error(
                            "embedding 响应无效 JSON: "
                            f"响应长度={len(text)}, "
                            f"Content-Length={resp.headers.get('Content-Length', '<missing>')}, "
                            f"Content-Type={resp.headers.get('Content-Type', '<missing>')}, "
                            f"解析位置={exc.position}, 错误={exc}, "
                            f"片段前={before!r}, 片段后={after!r}"
                        )
                        if attempt == 1:
                            await asyncio.sleep(2)
                            continue
                        raise VectorEngineError(
                            "向量服务返回无效 JSON（已重试一次）",
                            detail=f"embedding JSON 解析失败: {exc}; 响应长度={len(text)}",
                        ) from exc
                break
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(
                "embedding 目标地址被安全策略拒绝",
                detail=f"embedding 目标地址被安全策略拒绝: {e}",
            ) from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"embedding 响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(
                "embedding 请求失败（详情见日志）",
                detail=f"embedding 请求失败: {redact_url(url)}: {e}",
            ) from e
        except (KeyError, IndexError, ValueError) as e:
            raise VectorEngineError(
                "向量服务响应解析失败",
                detail=f"embedding 响应解析失败: {e}",
            ) from e

        # parse_embedding_response 已保证：非空 list[float]、逐元素为数值且有限，
        # 因此这里无需再做一遍 O(dim) 的维度校验。
        return emb

    async def embed_file(self, path: str, *, mime: str | None = None) -> list:
        """把本地图片向量化；mime 由调用方提供时不再重复探测。

        mime 来自同一张图刚完成的一次解码（compute_features_with_mime 或
        image_mime），传进来即可省掉 embed_file 自己的 `image_mime()` 解码。
        """
        if not mime:
            mime = await asyncio.to_thread(image_mime, path)
        if mime is None:
            raise VectorEngineError("图片内容无效（下载不完整、链接已过期或格式不受支持）")
        data = await asyncio.to_thread(read_bytes, path)
        return await self.embed_bytes(data, mime)
