"""本地开发用的包名别名：让插件代码在"目录名不是包名"的检出里也能导入。

AstrBot 要求插件目录（= 仓库根目录）里直接放 metadata.yaml + main.py，安装/克隆
时目录名恒为 astrbot_plugin_weibo_suite，所以插件内部一律用
`astrbot_plugin_weibo_suite.xxx` 绝对导入（trace/*.py 里还有相对导入兜底）。

但本地检出的目录名由用户决定（本机是「微博全链路辅助插件」），直接跑 pytest 会
ModuleNotFoundError。这里在 collection 之前把仓库根目录以 astrbot_plugin_weibo_suite
的名字注册进 sys.modules，子模块搜索路径指向仓库根，等价于 git clone 后的真实布局。

CI 的 actions/checkout 目录名就是 astrbot_plugin_weibo_suite，那时这里什么都不做。
本文件只服务于开发/测试，不属于发布面（见 scripts/build_release.py 的 EXCLUDED_FILES）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PACKAGE = "astrbot_plugin_weibo_suite"
ROOT = Path(__file__).resolve().parent


def _alias_repo_root_as_package() -> None:
    """把 ROOT 注册成名为 PACKAGE 的包；目录名已匹配时直接跳过。"""
    if ROOT.name == PACKAGE or PACKAGE in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 仅结构损坏时发生
        return
    module = importlib.util.module_from_spec(spec)
    # 必须先入 sys.modules，__init__.py 里的相对导入才找得到父包
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)


_alias_repo_root_as_package()
