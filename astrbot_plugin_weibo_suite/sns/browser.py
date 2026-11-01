"""Playwright 持久化上下文的生命周期（M1 取数的共用底座）。

设计要点（都是为了"保号"）：

1. **每个平台一份带用户数据目录的持久化上下文，跨轮询复用**。每次检查都新开
   浏览器，等于每次都给平台一个全新的、没有历史的新指纹——这本身就是异常。
   分平台的 user_data_dir 与 `identity.profile_dir()` 是同一个口径：清某个
   平台的 Cookie/profile 不会连累其他平台。
2. **优先用系统已装的 Edge（`channel="msedge"`）**，不下载 Playwright 自带的
   Chromium：TLS/JA3 指纹与真实用户完全一致，也不需要用户为了插件再下 100MB。
   退路顺序 msedge → chrome → 自带 chromium。
3. **登录只发生在用户手动点的那一次**。插件不会碰登录表单，也不会把帐号密码
   存在任何地方；`open_login()` 只是把窗口导航到登录页，剩下的交给用户。
4. **Cookie 不出浏览器**。持久化上下文自己管 Cookie，本模块不读、不导出、
   不写盘；`stop()` 只关窗口。

依赖是软的：没装 `playwright` 或缺少浏览器都只让 M1 不可用
（`BrowserUnavailable`），插件其余功能照常工作——这是 import 放在函数内的原因。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from . import constants as C


class BrowserUnavailable(RuntimeError):
    """M1 所需环境不满足（没装 playwright / 浏览器启动失败）。"""


def _logger():
    try:
        from astrbot.api import logger  # type: ignore

        return logger
    except Exception:  # 轻量测试桩里没有 astrbot.api
        import logging

        return logging.getLogger("astrbot_plugin_weibo_suite.sns")


# 启动参数：抹掉最刺眼的自动化特征。刻意不做激进伪装——伪装成"完美无瑕的
# 浏览器"反而是另一种指纹，这里只去掉 `navigator.webdriver` 这一项。
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
]

_CHANNEL_CANDIDATES: tuple[str | None, ...] = ("msedge", "chrome", None)


class BrowserSession:
    """持久化浏览器上下文（有头窗口，用户在里面登录一次）。"""

    def __init__(
        self,
        profile_root: Path,
        *,
        headed: bool = True,
        timeout_ms: int = C.BROWSER_TIMEOUT_MS,
        logger: Any = None,
    ):
        self.profile_root = Path(profile_root)
        self.headed = bool(headed)
        self.timeout_ms = int(timeout_ms)
        self.log = logger or _logger()
        self._playwright: Any = None
        # platform -> 持久化上下文。每个平台一个独立 user_data_dir，
        # 一个平台风控清 Cookie 时不会把另一个平台连带登出。
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._channel: str = ""
        self._error: str = ""

    # ------------------------------------------------------------ 属性

    @property
    def running(self) -> bool:
        return bool(self._contexts)

    @property
    def channel(self) -> str:
        return self._channel or "未启动"

    def _platform_dir(self, platform: str) -> Path:
        # 每个平台一个独立 profile：抖音和小红书的 Cookie 混在一个 profile 里，
        # 一旦某个平台风控清 Cookie，另一个平台会被连带登出。
        return self.profile_root / platform

    # ------------------------------------------------------------ 生命周期

    @staticmethod
    def probe() -> tuple[bool, str]:
        """检查 playwright 是否可用；返回 (可用, 说明)。纯静态，不启动浏览器。"""
        try:
            import playwright  # noqa: F401
        except Exception as exc:  # pragma: no cover - 取决于部署环境
            return False, f"未安装 playwright（{exc.__class__.__name__}），M1 浏览器取数不可用"
        return True, ""

    async def start(self, platform: str) -> None:
        """启动该平台的持久化上下文；失败时抛 `BrowserUnavailable`（带可执行建议）。

        同一个平台的并发启动由 `_start_locks[platform]` 串行化：后到的调用方会
        等前一次启动结束再判断，不会因为"看到别人正在启动"就空手返回、把
        `None` 上下文当可用上下文用。
        """
        if platform in self._contexts:
            return
        lock = self._start_locks.setdefault(platform, asyncio.Lock())
        async with lock:
            if platform in self._contexts:
                return
            await self._start_locked(platform)

    async def _start_locked(self, platform: str) -> None:
        available, reason = self.probe()
        if not available:
            raise BrowserUnavailable(reason)
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright  # type: ignore

                self._playwright = await async_playwright().start()
            except Exception as exc:
                raise BrowserUnavailable(f"playwright 启动失败：{exc}") from exc
        errors: list[str] = []
        for channel in _CHANNEL_CANDIDATES:
            try:
                context = await self._launch(platform, channel)
            except Exception as exc:
                errors.append(f"{channel or 'chromium'}: {exc}")
                continue
            self._contexts[platform] = context
            self._channel = channel or "chromium"
            self._error = ""
            self.log.info(
                f"[sns] 浏览器已就绪（{self._channel}，有头={self.headed}，"
                f"profile={self._platform_dir(platform)}）"
            )
            return
        self._error = "；".join(errors)
        if not self._contexts:
            # 一个上下文都没起来：node driver 也停掉，否则每次重试都漏一个进程。
            await self._stop_playwright()
        raise BrowserUnavailable(
            "没有可用的浏览器：请先装 Microsoft Edge，或执行 "
            f"`playwright install chromium`。（{self._error}）"
        )

    async def _launch(self, platform: str, channel: str | None):
        assert self._playwright is not None
        # 每个平台用各自的 user_data_dir（与 identity.profile_dir 同一口径）：
        # Playwright 的持久化上下文一个目录只能被一个上下文独占。
        profile_dir = self._platform_dir(platform)
        profile_dir.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": not self.headed,
            "args": list(_LAUNCH_ARGS),
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 900},
        }
        if channel:
            options["channel"] = channel
        return await self._playwright.chromium.launch_persistent_context(**options)

    async def _stop_playwright(self) -> None:
        """尽力停掉 node driver（不存在/已停都当成功）。"""
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    async def stop(self) -> None:
        self._pages.clear()
        self._locks.clear()
        self._start_locks.clear()
        contexts, self._contexts = dict(self._contexts), {}
        for context in contexts.values():
            with contextlib.suppress(Exception):
                await context.close()
        await self._stop_playwright()

    # ------------------------------------------------------------ 页面

    def lock(self, platform: str) -> asyncio.Lock:
        """平台级串行锁：同一平台同一时刻只允许一个页面在动。

        这不是性能优化而是风控要求：并发打开两个博主主页，在平台上看起来就是
        "一个用户在同时开两个标签页"，比顺序访问可疑得多。
        """
        return self._locks.setdefault(platform, asyncio.Lock())

    async def page(self, platform: str):
        """取该平台复用的页面（没有就新建一个）。"""
        if platform not in self._contexts:
            await self.start(platform)
        existing = self._pages.get(platform)
        if existing is not None:
            try:
                if not existing.is_closed():
                    return existing
            except Exception:
                pass
        context = self._contexts.get(platform)
        if context is None:
            # 启动"成功"但上下文没落地（或并发 stop 抢先清掉）：明确报错，
            # 绝不把 None 当上下文继续用。
            raise RuntimeError(f"浏览器上下文未就绪：{platform}")
        page = await context.new_page()
        try:
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
        except Exception:
            pass
        self._pages[platform] = page
        return page

    async def goto(self, platform: str, url: str, *, wait_until: str = "domcontentloaded"):
        """在平台页面上导航，返回 (page, 最终 URL)。"""
        page = await self.page(platform)
        response = await page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
        status = 0
        try:
            status = int(response.status) if response is not None else 0
        except Exception:
            status = 0
        final_url = ""
        try:
            final_url = str(page.url or "")
        except Exception:
            final_url = ""
        return page, status, final_url

    async def open_login(self, platform: str) -> str:
        """把窗口导航到该平台的登录页，交给用户手动登录；返回当前 URL。"""
        url = C.PLATFORM_LOGIN_URL.get(platform, C.PLATFORM_HOME.get(platform, ""))
        if not url:
            raise BrowserUnavailable(f"未知平台：{platform}")
        page, _status, final_url = await self.goto(platform, url)
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        return final_url or url

    async def read_state(self, page, expression: str) -> Any:
        """在页面里求值并解析 JSON（拿 `window.__INITIAL_STATE__` 用）。"""
        try:
            raw = await page.evaluate(expression)
        except Exception:
            return None
        if raw is None:
            return None
        if isinstance(raw, (dict, list)):
            return raw
        try:
            import json

            return json.loads(raw)
        except Exception:
            return None

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        available, reason = self.probe()
        return {
            "available": available,
            "reason": reason,
            "running": self.running,
            "channel": self.channel,
            "headed": self.headed,
            "profile_root": str(self.profile_root),
            "error": self._error,
            "pages": sorted(self._pages.keys()),
        }
