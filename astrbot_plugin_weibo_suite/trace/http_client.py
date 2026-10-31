"""Compatibility import for the shared guarded HTTP client."""

from ..shared.http import GuardedHttpClient, HttpClientGetter

__all__ = ["GuardedHttpClient", "HttpClientGetter"]
