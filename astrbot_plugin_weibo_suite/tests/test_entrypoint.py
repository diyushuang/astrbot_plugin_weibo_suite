"""入口结构契约。

AstrBot 的加载器按 `handler.__module__ == 插件入口模块路径` 绑定处理器
（star_manager: `get_handlers_by_module_name(metadata.module_path)`），
所以所有 `@filter` 装饰器都必须写在插件入口 main.py 里；域模块只放业务方法。
这些用例把该约定钉死，避免以后再把指令装饰器挪回 weibo/、trace/ 而线上静默失效。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import astrbot_plugin_weibo_suite.main as entry

ROOT = Path(entry.__file__).resolve().parent
HANDLER_DECORATOR_PREFIXES = (
    "filter.command",
    "filter.command_group",
    "filter.llm_tool",
    "filter.regex",
    "filter.event_message_type",
    "filter.permission_type",
    "filter.platform_adapter_type",
    "weibo.command",
    "weibo.group",
)


def test_plugin_class_is_defined_in_entry_module():
    assert entry.WeiboSuitePlugin.__module__ == entry.__name__


def test_entry_module_exposes_single_candidate_class():
    """旧版加载路径按类名回退时，也只能选中套件类。

    AstrBot 的 `_get_classes()` 取 `inspect.getmembers()` 里第一个名字以
    Plugin 结尾（或等于 Main）的类；这里复刻同一顺序，防止入口又摆出多个类。
    """
    candidates: list[str] = []
    for name, _obj in inspect.getmembers(entry, inspect.isclass):
        if name.lower().endswith("plugin") or name.lower() == "main":
            candidates.append(name)
            break
    assert candidates == ["WeiboSuitePlugin"]


def test_no_filter_decorators_outside_entry_module():
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or path.name == "main.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if text.startswith(HANDLER_DECORATOR_PREFIXES):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{text}")
    assert not offenders, offenders


SNS_SUBCOMMANDS = {
    "帮助",
    "抖音",
    "小红书",
    "IG",
    "RSS",
    "列表",
    "删除",
    "检测",
    "登录",
    "状态",
    "演练",
}
# 「演练」会真的去抓一次数据（只是不推送），同样属于会触达外部平台的操作，
# 必须和管理员门保持一致，否则任何群成员都能拿它当探针打平台。
SNS_ADMIN_COMMANDS = {"抖音", "小红书", "IG", "RSS", "删除", "检测", "登录", "演练"}


def _plugin_class_body() -> list[ast.stmt]:
    tree = ast.parse(Path(entry.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WeiboSuitePlugin":
            return node.body
    raise AssertionError("入口模块里没有 WeiboSuitePlugin")


def _decorator_names(node: ast.AST) -> list[str]:
    """把装饰器还原成文本，方便同时断言 'sns_group.command' 与 'permission_type'。"""
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return []
    return [ast.unparse(d) for d in node.decorator_list]


def _sns_subcommand(node: ast.AST) -> str | None:
    """若该函数挂了 `@sns_group.command("xxx")`，返回 "xxx"。"""
    for decorator in getattr(node, "decorator_list", []):
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr != "command":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "sns_group":
            continue
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            return str(decorator.args[0].value)
    return None


def test_sns_command_group_is_declared_in_entry_module():
    """`订阅` 指令组必须完整出现在 main.py，且与聊天帮助里列出的子指令一致。"""
    body = _plugin_class_body()
    groups = [n for n in body if isinstance(n, ast.AsyncFunctionDef) and n.name == "sns_group"]
    assert len(groups) == 1, "订阅 指令组应当只声明一次"
    # ast.unparse 统一用单引号，这里不去猜引号风格，只比对结构
    assert any(
        text.startswith("filter.command_group(") and "订阅" in text
        for text in _decorator_names(groups[0])
    ), _decorator_names(groups[0])

    seen: dict[str, ast.AST] = {}
    for node in body:
        name = _sns_subcommand(node)
        if name is not None:
            assert name not in seen, f"订阅子指令重复：{name}"
            seen[name] = node
    assert set(seen) == SNS_SUBCOMMANDS, set(seen) ^ SNS_SUBCOMMANDS


def test_sns_subcommands_forward_to_mixin_and_gate_admin():
    """每个子指令都要真正转到 mixin（否则是空壳指令），写操作必须加管理员门。"""
    body = _plugin_class_body()
    for node in body:
        name = _sns_subcommand(node)
        if name is None:
            continue
        decorators = " ".join(_decorator_names(node))
        requires_admin = "PermissionType.ADMIN" in decorators
        assert requires_admin is (name in SNS_ADMIN_COMMANDS), name
        # super().sns_xxx(...) 或 self._sns_add(...)，两者都包在 async for 里转发结果
        forwards = [ast.unparse(n.iter) for n in ast.walk(node) if isinstance(n, ast.AsyncFor)]
        assert any("super()" in text or "_sns_add" in text for text in forwards), (
            f"{name} 没有把结果转发给调用方：{forwards}"
        )
