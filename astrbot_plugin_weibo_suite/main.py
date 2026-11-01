"""微博全链路助手入口。

AstrBot 只把「入口模块」当作插件本体：`@filter` 装饰的处理器必须定义在
main.py 里，加载器才会把处理器绑定到本插件的实例上（它按
`handler.__module__ == 插件模块路径` 做绑定）。因此本文件是唯一注册指令的
位置，weibo/ 与 trace/ 域只保留可复用的业务方法；跨域装配也收敛在这里。

类名与装饰器都只在这里出现，避免 AstrBot 误把域内的 Star 子类当成独立插件。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context

from .sns.commands import SnsCommandsMixin as _SnsCommandsMixin
from .trace.main import ImageTracePlugin as _ImageTracePlugin
from .weibo.pipeline import PipelineServices
from .weibo.plugin import WeiboMonitorPlugin as _WeiboMonitorPlugin


class WeiboSuitePlugin(_WeiboMonitorPlugin, _SnsCommandsMixin):
    """Unified plugin: Weibo monitoring commands plus trace/check-in commands."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # Both domains use the suite's plugin_data directory but own separate
        # SQLite/cache handles and lifecycle resources.
        self._trace_plugin = _ImageTracePlugin(context, config if config is not None else {})
        self._trace_initialized = False
        self._weibo_initialized = False

    def _bind_pipeline_services(self) -> None:
        """把溯源域的图床/向量/图库交给图床管线。

        双向隔离的代价是 weibo 域不能 import trace，跨域装配点只能是这里：
        main.py 是计划里唯一允许跨域调用的位置。
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

    def _bind_checkin_source(self) -> None:
        """把归档库当作签到文案源接给溯源域（P5/F3）。

        溯源域只认 (指纹, 行列表) 这个窄端口，惰性求值：签到那一刻 archive.db
        是否就绪、清洗到哪一条，都由 copylib_snapshot() 现场决定；归档未启用时
        返回空，签到自动回退 weibo/ 下的 CSV/MD（迁移期兼容）。
        """
        trace = self._trace_plugin
        if not hasattr(trace, "_weibo_external_source"):
            return
        trace._weibo_external_source = self.copylib_snapshot

    async def initialize(self) -> None:
        # Trace first: the monitoring initializer installs the shared logger last,
        # so one handler captures both domains' warnings and errors.
        await self._trace_plugin.initialize()
        self._trace_initialized = True
        # 服务要在微博域 initialize 之前绑定：那边的 pipeline_start() 会起 worker
        self._bind_pipeline_services()
        # 文案源是惰性供应器，先接上；archive.db 在微博域 initialize 里才打开
        self._bind_checkin_source()
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

    # ------------------------------------------------------------------
    # 溯源 / 签到（委托给 trace 域；trace/main.py 不再自己注册指令）
    # ------------------------------------------------------------------

    @filter.command("溯源", alias={"找原图"})
    async def trace(self, event: AstrMessageEvent):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
        async for result in self._trace_plugin.trace(event):
            yield result

    @filter.command("登记原图", alias={"原图登记"})
    async def register_image(self, event: AstrMessageEvent):
        """登记一张原图到本地图库或图床哈希索引"""
        async for result in self._trace_plugin.register_image(event):
            yield result

    @filter.command("溯源状态")
    async def status(self, event: AstrMessageEvent):
        """查看图库、索引与引擎状态"""
        async for result in self._trace_plugin.status(event):
            yield result

    @filter.command("溯源重扫")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rescan(self, event: AstrMessageEvent):
        """重新扫描本地图库并刷新特征索引（管理员）"""
        async for result in self._trace_plugin.rescan(event):
            yield result

    @filter.command("溯源删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_entry(self, event: AstrMessageEvent, entry_id: int):
        """按编号删除一条图库记录（管理员）"""
        async for result in self._trace_plugin.delete_entry(event, entry_id):
            yield result

    @filter.command("溯源帮助")
    async def trace_help(self, event: AstrMessageEvent):
        """查看图片溯源指令用法"""
        async for result in self._trace_plugin.trace_help(event):
            yield result

    @filter.command("随机图", alias={"随机图片"})
    async def random_image(self, event: AstrMessageEvent):
        """随机返回一张图库图片"""
        async for result in self._trace_plugin.random_image(event):
            yield result

    @filter.command("随机视频")
    async def random_video(self, event: AstrMessageEvent):
        """随机返回一个图库视频"""
        async for result in self._trace_plugin.random_video(event):
            yield result

    @filter.command("签到", alias={"打卡"})
    async def checkin(self, event: AstrMessageEvent):
        """每日签到，返回一张签到卡片"""
        async for result in self._trace_plugin.checkin(event):
            yield result

    @filter.command("签到文案导入", alias={"签到文案上传"})
    async def checkin_import(self, event: AstrMessageEvent):
        """上传 CSV/Markdown/TXT 文案文件作为签到文案库"""
        async for result in self._trace_plugin.checkin_import(event):
            yield result

    @filter.command("原图修复")
    async def original_repair(self, event: AstrMessageEvent):
        """修复图库中缺失的媒体文件"""
        async for result in self._trace_plugin.original_repair(event):
            yield result

    @filter.command("原图")
    async def original_image(self, event: AstrMessageEvent):
        """按图片名直取图床中的原图"""
        async for result in self._trace_plugin.original_image(event):
            yield result

    @filter.llm_tool(name="sendRandomMedia")
    async def send_random_media(
        self, event: AstrMessageEvent, directory: str | None = None, content_type: str | None = None
    ):
        """随机发送图库中的图片或视频。

        Args:
            directory(string): 可选，指定图库子目录。
            content_type(string): 可选，image 或 video。
        """
        async for result in self._trace_plugin.send_random_media(event, directory, content_type):
            yield result

    # ------------------------------------------------------------------
    # 微博监控（CommandsMixin 只保留业务方法，注册集中在这里）
    # ------------------------------------------------------------------

    @filter.command_group("微博")
    async def weibo(self, event: AstrMessageEvent):
        """微博监控转发指令组"""
        async for result in super().weibo(event):
            yield result

    @weibo.command("帮助", alias={"help", "用法"})
    async def weibo_help(self, event: AstrMessageEvent):
        """查看微博监控指令用法"""
        async for result in super().weibo_help(event):
            yield result

    @weibo.command("绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_bind(self, event: AstrMessageEvent):
        """把当前群聊绑定为微博推送目标（管理员）"""
        async for result in super().weibo_bind(event):
            yield result

    @weibo.command("解绑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_unbind(self, event: AstrMessageEvent):
        """解除当前群聊的微博推送（管理员）"""
        async for result in super().weibo_unbind(event):
            yield result

    @weibo.command("添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_add(self, event: AstrMessageEvent, uid_text: str = ""):
        """添加监控的微博账号，参数为 uid 或主页链接（管理员）"""
        async for result in super().weibo_add(event, uid_text):
            yield result

    @weibo.command("删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_remove(self, event: AstrMessageEvent, uid_text: str = ""):
        """删除监控的微博账号（管理员）"""
        async for result in super().weibo_remove(event, uid_text):
            yield result

    @weibo.command("列表")
    async def weibo_list(self, event: AstrMessageEvent):
        """列出正在监控的微博账号"""
        async for result in super().weibo_list(event):
            yield result

    @weibo.command("相册绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_bind(self, event: AstrMessageEvent, uid_text: str = ""):
        """把当前群的相册绑定到某个微博账号（管理员）"""
        async for result in super().weibo_album_bind(event, uid_text):
            yield result

    @weibo.command("相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_unbind(self, event: AstrMessageEvent, uid_text: str = ""):
        """移除当前群与微博账号的相册绑定（管理员）"""
        async for result in super().weibo_album_unbind(event, uid_text):
            yield result

    @weibo.command("相册列表")
    async def weibo_album_rules_cmd(self, event: AstrMessageEvent):
        """查看当前群的相册绑定"""
        async for result in super().weibo_album_rules_cmd(event):
            yield result

    @weibo.command("列相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_list(self, event: AstrMessageEvent):
        """列出 QQ 群可用的相册（管理员）"""
        async for result in super().weibo_album_list(event):
            yield result

    @weibo.command("关键词相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_keyword_album(self, event: AstrMessageEvent, kw: str = ""):
        """按关键词把微博图片路由到指定相册（管理员）"""
        async for result in super().weibo_keyword_album(event, kw):
            yield result

    @weibo.command("关键词相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_keyword_album_remove(self, event: AstrMessageEvent, kw: str = ""):
        """移除关键词相册规则（管理员）"""
        async for result in super().weibo_keyword_album_remove(event, kw):
            yield result

    @weibo.command("博主关键词相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_blogger_keyword_album(
        self, event: AstrMessageEvent, uid_text: str = "", kw: str = ""
    ):
        """按博主 + 关键词把图片路由到指定相册（管理员）"""
        async for result in super().weibo_blogger_keyword_album(event, uid_text, kw):
            yield result

    @weibo.command("博主关键词相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_blogger_keyword_album_remove(
        self, event: AstrMessageEvent, uid_text: str = "", kw: str = ""
    ):
        """移除博主关键词相册规则（管理员）"""
        async for result in super().weibo_blogger_keyword_album_remove(event, uid_text, kw):
            yield result

    @weibo.command("关键词相册列表")
    async def weibo_keyword_album_list(self, event: AstrMessageEvent):
        """列出全部关键词相册规则"""
        async for result in super().weibo_keyword_album_list(event):
            yield result

    @weibo.command("检测")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_check(self, event: AstrMessageEvent):
        """立即检查一轮微博更新（管理员）"""
        async for result in super().weibo_check(event):
            yield result

    @weibo.command("状态")
    async def weibo_status(self, event: AstrMessageEvent):
        """查看监控、推送队列与风控状态"""
        async for result in super().weibo_status(event):
            yield result

    @weibo.command("回填")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_backfill(self, event: AstrMessageEvent, uid_text: str = ""):
        """全量回填某个账号的历史微博（管理员）"""
        async for result in super().weibo_backfill(event, uid_text):
            yield result

    @weibo.command("回填状态")
    async def weibo_backfill_status(self, event: AstrMessageEvent):
        """查看历史回填进度"""
        async for result in super().weibo_backfill_status(event):
            yield result

    @weibo.command("回填停止")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_backfill_stop(self, event: AstrMessageEvent):
        """停止当前回填任务（管理员）"""
        async for result in super().weibo_backfill_stop(event):
            yield result

    @weibo.command("归档导入")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_archive_import(self, event: AstrMessageEvent, path_text: str = ""):
        """导入 JSON 归档种子文件（管理员）"""
        async for result in super().weibo_archive_import(event, path_text):
            yield result

    @weibo.command("归档查询")
    async def weibo_archive_query_cmd(self, event: AstrMessageEvent, uid_text: str = ""):
        """按 uid 查询已归档的微博"""
        async for result in super().weibo_archive_query_cmd(event, uid_text):
            yield result

    @weibo.command("对账")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_archive_reconcile(self, event: AstrMessageEvent, uid_text: str = ""):
        """核对归档、图床、向量与文案的缺口（管理员）"""
        async for result in super().weibo_archive_reconcile(event, uid_text):
            yield result

    @weibo.command("管线状态")
    async def weibo_pipeline_status(self, event: AstrMessageEvent):
        """查看图床与向量管线状态"""
        async for result in super().weibo_pipeline_status(event):
            yield result

    @weibo.command("管线补跑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_pipeline_backfill(self, event: AstrMessageEvent):
        """重新入队未完成的图床管线任务（管理员）"""
        async for result in super().weibo_pipeline_backfill(event):
            yield result

    @weibo.command("文案库")
    async def weibo_copylib_status(self, event: AstrMessageEvent, uid_text: str = ""):
        """查看签到文案库统计"""
        async for result in super().weibo_copylib_status(event, uid_text):
            yield result

    @weibo.command("文案列表")
    async def weibo_copylib_list(self, event: AstrMessageEvent, args_text: str = ""):
        """列出最近清洗的签到文案"""
        async for result in super().weibo_copylib_list(event, args_text):
            yield result

    @weibo.command("文案清洗")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_copylib_rebuild(self, event: AstrMessageEvent, args_text: str = ""):
        """重建签到文案清洗结果（管理员）"""
        async for result in super().weibo_copylib_rebuild(event, args_text):
            yield result

    @weibo.command("文案导出")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_copylib_export(self, event: AstrMessageEvent, args_text: str = ""):
        """导出签到文案为 CSV/Markdown（管理员）"""
        async for result in super().weibo_copylib_export(event, args_text):
            yield result

    @weibo.command("文案导入")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_copylib_import(self, event: AstrMessageEvent, path_text: str = ""):
        """导入 CSV/Markdown 文案文件（管理员）"""
        async for result in super().weibo_copylib_import(event, path_text):
            yield result

    # ---------------------------------------------------------------- 订阅（多平台）

    @filter.command_group("订阅")
    async def sns_group(self, event: AstrMessageEvent):
        """抖音 / 小红书 / Instagram 博主更新订阅"""
        async for result in super().sns_group(event):
            yield result

    @sns_group.command("帮助", alias={"help", "用法"})
    async def sns_help(self, event: AstrMessageEvent):
        """查看多平台订阅指令用法"""
        async for result in super().sns_help(event):
            yield result

    @sns_group.command("抖音", alias={"douyin", "dy"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_add_douyin(self, event: AstrMessageEvent, target: str = ""):
        """订阅抖音博主（管理员）"""
        async for result in self._sns_add(event, "douyin", target or self._sns_rest(event, 2)):
            yield result

    @sns_group.command("小红书", alias={"xhs", "红书"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_add_xhs(self, event: AstrMessageEvent, target: str = ""):
        """订阅小红书博主（管理员）"""
        async for result in self._sns_add(event, "xhs", target or self._sns_rest(event, 2)):
            yield result

    @sns_group.command("IG", alias={"ig", "instagram", "ins"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_add_instagram(self, event: AstrMessageEvent, target: str = ""):
        """订阅 Instagram 博主（管理员）"""
        async for result in self._sns_add(event, "instagram", target or self._sns_rest(event, 2)):
            yield result

    @sns_group.command("RSS", alias={"rss"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_rss(self, event: AstrMessageEvent, url: str = ""):
        """订阅任意 RSS 源（管理员）"""
        async for result in super().sns_rss(event):
            yield result

    @sns_group.command("列表")
    async def sns_list(self, event: AstrMessageEvent):
        """查看全部订阅"""
        async for result in super().sns_list(event):
            yield result

    @sns_group.command("删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_remove(self, event: AstrMessageEvent):
        """取消订阅（管理员）"""
        async for result in super().sns_remove(event):
            yield result

    @sns_group.command("检测")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_check(self, event: AstrMessageEvent):
        """立刻检查一轮订阅更新（管理员）"""
        async for result in super().sns_check(event):
            yield result

    @sns_group.command("登录")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_login(self, event: AstrMessageEvent):
        """打开浏览器窗口手动登录（管理员）"""
        async for result in super().sns_login(event):
            yield result

    @sns_group.command("状态")
    async def sns_status(self, event: AstrMessageEvent):
        """查看订阅登录态、健康度与冷却"""
        async for result in super().sns_status(event):
            yield result

    @sns_group.command("演练")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sns_drill(self, event: AstrMessageEvent):
        """取一次数但不推送（验证登录态，管理员）"""
        async for result in super().sns_drill(event):
            yield result

    @staticmethod
    def _sns_rest(event: AstrMessageEvent, skip: int) -> str:
        """取指令名之后的剩余文本（链接里可能没有空格，但用户可能连备注一起抄）。

        `订阅 抖音 https://www.douyin.com/user/MS4w...` 这种写法里，框架会把
        整个链接当成第一个参数；带空格的情况（比如随手粘了「看这个 <链接>」）
        则要靠这里把后面的词拼回来。
        """
        parts = (getattr(event, "message_str", "") or "").strip().split()
        return " ".join(parts[skip:]).strip()


# Star.__init_subclass__ 会给每个 Star 子类建一条元数据；trace/weibo 域内的类
# 只是复用 KV/生命周期能力的实现细节，不是独立插件。若不清掉，WebUI 会把它们
# 当成没有名字的插件列出来，重载时还会尝试终止空实例。
try:
    from astrbot.core.star.star import star_map, star_registry
except ImportError:  # 轻量测试桩不提供内部注册表
    star_map = None
    star_registry = None

if star_map is not None and star_registry is not None and __package__:
    _prefix = __package__ + "."
    for _module_path, _metadata in list(star_map.items()):
        if _module_path == __name__ or not _module_path.startswith(_prefix):
            continue
        star_map.pop(_module_path, None)
        if _metadata in star_registry:
            star_registry.remove(_metadata)


__all__ = ["WeiboSuitePlugin"]
