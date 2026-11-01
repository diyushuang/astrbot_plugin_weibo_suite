"""运行期全局名解析门禁。

wildcard import（`from .runtime import *`）会让 ruff 看不到未定义的名字：
调用方写了 `_jsonp_data` / `_header_int` 这类不存在的名字时，pyflakes 不报，
只有真正跑到那一行才 NameError。本用例导入全部运行模块，再用 symtable 检查
每个函数引用的全局名是否真的能在模块命名空间（或 builtins）里解析到。
"""

from __future__ import annotations

import builtins
import importlib
import pathlib
import symtable

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 仓库根就是插件根，rglob 会一路扫到 .venv / .git；这里显式跳过非源码目录。
# 不跳过的话 .venv 里的第三方模块会被当成 astrbot_plugin_weibo_suite..venv.* 导入，
# 既产生一堆无关的 import 报错，又会给 PIL/numpy 造出第二份模块副本，
# 让依赖 isinstance 判断的用例在同一次 pytest 会话里莫名其妙地失败。
SKIP_DIRS = {
    ".git",
    ".mimosa",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".workbuddy",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "scripts",
    "tests",
}


def _runtime_modules() -> list[str]:
    modules = []
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if set(parts) & SKIP_DIRS:
            continue
        if path.name in {"__init__.py", "runtime.py"}:
            continue
        module = "astrbot_plugin_weibo_suite." + ".".join(parts).removesuffix(".py")
        modules.append(module)
    return sorted(modules)


def _unresolved_globals(table: symtable.SymbolTable, module_defined: set[str]) -> list[str]:
    missing = []
    for symbol in table.get_symbols():
        if symbol.is_referenced() and symbol.is_global():
            name = symbol.get_name()
            if name not in module_defined and not hasattr(builtins, name):
                missing.append(name)
    for child in table.get_children():
        missing.extend(_unresolved_globals(child, module_defined))
    return missing


def test_runtime_modules_only_use_resolvable_globals():
    problems: list[str] = []
    for module_name in _runtime_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # 导入失败本身就是问题
            problems.append(f"{module_name}: import failed: {exc!r}")
            continue
        relative = module_name.removeprefix("astrbot_plugin_weibo_suite.").replace(".", "/")
        source_path = ROOT / f"{relative}.py"
        source = source_path.read_text(encoding="utf-8")
        table = symtable.symtable(source, str(source_path), "exec")
        # 模块级已赋值的名字（含条件分支里的 import / 循环变量）视为已定义；
        # 只把“从未在模块级出现过”的引用判为拼写错误。
        module_defined = {symbol.get_name() for symbol in table.get_symbols()}
        module_defined |= set(vars(module))
        missing = sorted(set(_unresolved_globals(table, module_defined)))
        if missing:
            problems.append(f"{module_name}: unresolved globals {missing}")
    assert not problems, problems
