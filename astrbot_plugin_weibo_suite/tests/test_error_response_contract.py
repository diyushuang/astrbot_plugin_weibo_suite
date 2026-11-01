"""契约门禁：`error_response(message, *, status_code=...)` 的第二个参数只能按关键字传。

AstrBot 4.28 起 `astrbot.api.web.error_response` 的签名是
`error_response(message: str, *, status_code: int = 400, data=None, headers=None)`，
status_code 是 **keyword-only**。插件面板的所有错误分支以前都写成
`error_response("...", 409)`，在真机上会抛
`TypeError: error_response() takes 1 positional argument but 2 were given`，
表现为"每个错误分支都变成 500"——而单测里 monkeypatch 成了
`lambda message, status_code=400`，一直是假绿。

这里不跑任何运行时代码，纯 ast 静态扫描，因此**没有装 astrbot 的环境也能跑**，
CI 里也是同一份判定：

1. views_backend/ 下任何 `error_response(...)` 调用，位置参数不得超过 1 个；
2. 模块内本地兜底定义的 `error_response`，status_code 必须是 keyword-only，
   否则本地兜底与真实 API 的调用约定又会分叉。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "views_backend"
BACKEND_FILES = sorted(BACKEND_DIR.glob("*.py"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def find_positional_status_calls(tree: ast.AST, path: Path) -> list[str]:
    """收集 status_code 被按位置传入的调用点。"""
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "error_response":
            continue
        # 允许 0 个或 1 个位置参数（只传 message）；第 2 个起必须走关键字
        if len(node.args) > 1:
            problems.append(f"{path.name}:{node.lineno} 有 {len(node.args)} 个位置参数")
    return problems


def find_non_kwonly_fallback(tree: ast.AST, path: Path) -> list[str]:
    """本地兜底定义里 status_code 必须是 keyword-only（`*, status_code=400`）。"""
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "error_response":
            continue
        kwonly = {arg.arg for arg in node.args.kwonlyargs}
        if "status_code" not in kwonly:
            problems.append(f"{path.name}:{node.lineno} 的 status_code 不是 keyword-only")
    return problems


def test_backend_files_exist():
    """扫描目标存在本身就是断言：路径写错会让下面的用例空跑通过。"""
    names = {path.name for path in BACKEND_FILES}
    assert "dashboard.py" in names
    assert "sns_api.py" in names


@pytest.mark.parametrize("path", BACKEND_FILES, ids=lambda p: p.name)
def test_error_response_status_code_never_positional(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems = find_positional_status_calls(tree, path)
    assert not problems, (
        "error_response 的 status_code 是 keyword-only，按位置传会在真机抛 TypeError：\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize("path", BACKEND_FILES, ids=lambda p: p.name)
def test_error_response_fallback_is_keyword_only(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems = find_non_kwonly_fallback(tree, path)
    assert not problems, "本地兜底定义必须与 astrbot.api.web.error_response 同签名：\n" + "\n".join(
        problems
    )


def test_scanner_detects_positional_regression(tmp_path: Path):
    """反向用例：扫描器必须真的能抓到坏代码，否则门禁只是装饰。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def handler():\n    return error_response('boom', 409)\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    assert find_positional_status_calls(tree, bad) == ["bad.py:2 有 2 个位置参数"]


def test_scanner_detects_bad_fallback(tmp_path: Path):
    bad = tmp_path / "bad_fallback.py"
    bad.write_text(
        "def error_response(message: str, status_code: int = 400):\n    return message\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    assert find_non_kwonly_fallback(tree, bad) == [
        "bad_fallback.py:1 的 status_code 不是 keyword-only"
    ]
