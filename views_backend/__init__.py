"""统一 WebUI 的路由与参数适配层。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def commit_config(plugin: Any, mutate: Callable[[dict], None]) -> bool:
    """批量改配置并落盘：优先异步接口，老宿主/测试桩回落同步入口。

    插件的 `_update_config_async` 会把写盘丢给 `save_config_async` 或线程池，
    避免在请求协程里同步写文件；没有该接口的旧代码/测试桩继续用同步版，
    语义保持一致。
    """
    async_committer = getattr(plugin, "_update_config_async", None)
    if callable(async_committer):
        return bool(await async_committer(mutate))
    sync_committer = getattr(plugin, "_update_config", None)
    if callable(sync_committer):
        return bool(sync_committer(mutate))
    raise AttributeError("插件未提供配置写入接口")
