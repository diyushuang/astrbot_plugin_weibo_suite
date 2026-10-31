"""Compose the monitoring and image-trace domains into one AstrBot plugin."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context

from .trace.main import ImageTracePlugin
from .weibo.pipeline import PipelineServices
from .weibo.plugin import WeiboMonitorPlugin


class WeiboSuitePlugin(WeiboMonitorPlugin):
    """Unified plugin: Weibo monitoring commands plus trace/check-in commands."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # Both domains use the suite's plugin_data directory but own separate
        # SQLite/cache handles and lifecycle resources.
        self._trace_plugin = ImageTracePlugin(context, config if config is not None else {})
        self._trace_initialized = False
        self._weibo_initialized = False

    def _bind_pipeline_services(self) -> None:
        """把溯源域的图床/向量/图库交给图床管线。

        双向隔离的代价是 weibo 域不能 import trace，跨域装配点只能是这里：
        main.py / composite.py 是计划里唯一允许跨域调用的位置。
        """
        trace = self._trace_plugin
        try:
            bed_mode = ""
            mode_fn = getattr(trace, "_bed_mode", None)
            if callable(mode_fn):
                bed_mode = str(mode_fn() or "")
            services = PipelineServices(
                bed=getattr(trace, "bed", None),
                vector=getattr(trace, "vector", None),
                library=getattr(trace, "library", None),
                hash_size=int(getattr(trace, "hash_size", 16) or 16),
                bed_mode=bed_mode,
            )
        except Exception as exc:  # 绑定失败只降级管线，不能影响监控主链
            logger.warning(f"图床管线服务绑定失败（管线降级）: {exc}")
            return
        self.pipeline_bind(services)

    async def initialize(self) -> None:
        # Trace first: the monitoring initializer installs the shared logger last,
        # so one handler captures both domains' warnings and errors.
        await self._trace_plugin.initialize()
        self._trace_initialized = True
        # 服务要在微博域 initialize 之前绑定：那边的 pipeline_start() 会起 worker
        self._bind_pipeline_services()
        await super().initialize()
        self._weibo_initialized = True

    async def terminate(self) -> None:
        errors: list[str] = []
        if self._trace_initialized:
            try:
                await self._trace_plugin.terminate()
            except Exception as exc:
                errors.append(f"溯源域: {exc}")
            self._trace_initialized = False
        if self._weibo_initialized:
            try:
                await super().terminate()
            except Exception as exc:
                errors.append(f"微博域: {exc}")
            self._weibo_initialized = False
        if errors:
            logger.error("插件卸载时部分资源清理失败: " + "；".join(errors))

    @filter.command("溯源", alias={"找原图"})
    async def trace(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.trace(event):
            yield result

    @filter.command("登记原图", alias={"原图登记"})
    async def register_image(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.register_image(event):
            yield result

    @filter.command("溯源状态")
    async def status(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.status(event):
            yield result

    @filter.command("溯源重扫")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rescan(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.rescan(event):
            yield result

    @filter.command("溯源删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_entry(self, event: AstrMessageEvent, entry_id: int):
        async for result in self._trace_plugin.delete_entry(event, entry_id):
            yield result

    @filter.command("溯源帮助")
    async def trace_help(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.trace_help(event):
            yield result

    @filter.command("随机图", alias={"随机图片"})
    async def random_image(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.random_image(event):
            yield result

    @filter.command("随机视频")
    async def random_video(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.random_video(event):
            yield result

    @filter.command("签到", alias={"打卡"})
    async def checkin(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.checkin(event):
            yield result

    @filter.command("签到文案导入", alias={"签到文案上传"})
    async def checkin_import(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.checkin_import(event):
            yield result

    @filter.command("原图修复")
    async def original_repair(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.original_repair(event):
            yield result

    @filter.command("原图")
    async def original_image(self, event: AstrMessageEvent):
        async for result in self._trace_plugin.original_image(event):
            yield result

    @filter.llm_tool(name="sendRandomMedia")
    async def send_random_media(
        self, event: AstrMessageEvent, directory: str | None = None, content_type: str | None = None
    ):
        async for result in self._trace_plugin.send_random_media(event, directory, content_type):
            yield result
