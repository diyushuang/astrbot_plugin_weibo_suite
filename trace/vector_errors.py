"""向量引擎的异常类型。

单独成模块，避免 `vector_search`、`embedding_client`、`qdrant_client` 三者
为了共用异常而互相 import 形成环。
"""

from __future__ import annotations


class VectorEngineError(Exception):
    """向量引擎可展示给用户的错误。"""

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message


class HttpError(VectorEngineError):
    """带 HTTP 状态码的向量服务错误（供调用方按状态码分支，如 404）。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"服务返回 HTTP {status}", detail=f"HTTP {status}: {body[:500]}")
        self.status = status


class InvalidEmbeddingJSON(Exception):
    """Embedding 服务返回 HTTP 200，但响应体不是合法 JSON。"""

    def __init__(self, message: str, position: int | None = None):
        super().__init__(message)
        self.position = position
