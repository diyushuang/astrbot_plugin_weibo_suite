"""跨域共享常量；业务域常量应放在对应域模块中。"""

PLUGIN_NAME = "astrbot_plugin_weibo_suite"
PLUGIN_VERSION = "v0.9.2"
ACTIVITY_MAX = 100
ERRLOG_BUFFER = 2000

# WebUI 单次返回的明细上限；state.json/errors.log 仍保留完整数据。
PENDING_PAGE_LIMIT = 100
ACTIVITY_PAGE_LIMIT = 50
ERRLOG_PAGE_LIMIT = 50
