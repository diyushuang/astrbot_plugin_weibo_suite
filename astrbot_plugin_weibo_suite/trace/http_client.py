"""共享受保护 HTTP 客户端的兼容性导入。"""

from ..shared.http import GuardedHttpClient, HttpClientGetter

__all__ = ["GuardedHttpClient", "HttpClientGetter"]
