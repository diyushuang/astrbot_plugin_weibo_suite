"""微博全链路助手入口。

P1 先接入溯源域；P2 起在同一入口编排微博监控与归档。
"""

from .trace.main import ImageTracePlugin

# 过渡期别名：AstrBot 通过 main.py 中的 Star 子类加载插件。
WeiboSuitePlugin = ImageTracePlugin

__all__ = ["ImageTracePlugin", "WeiboSuitePlugin"]
