"""微博全链路助手入口。

P0 只建立可加载的生命周期骨架；业务域按阶段从旧插件平移并拆分。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .constants import PLUGIN_NAME, PLUGIN_VERSION


class WeiboSuitePlugin(Star):
    """微博监控、归档、图床管线和图片溯源的统一入口。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 保留 AstrBotConfig 的引用，面板写配置后才能通过 save_config 落盘。
        self.config: dict = config if config is not None else {}
        self.data_dir: Path | None = None

    async def initialize(self) -> None:
        logger.info("%s %s 已加载", PLUGIN_NAME, PLUGIN_VERSION)

    async def terminate(self) -> None:
        logger.info("%s 已卸载", PLUGIN_NAME)
