"""P1 structural and command-surface contract."""

from __future__ import annotations

import ast
from pathlib import Path

from astrbot_plugin_weibo_suite import trace


def test_all_migrated_trace_modules_exist():
    expected = {
        "checkin_card.py",
        "checkin_store.py",
        "common.py",
        "features.py",
        "hash_index.py",
        "http_client.py",
        "image_bed.py",
        "image_delivery.py",
        "library.py",
        "main.py",
        "media_history.py",
        "random_media.py",
        "s3_store.py",
        "vector_search.py",
        "weibo_posts.py",
    }
    root = Path(trace.__file__).resolve().parent
    assert expected <= {path.name for path in root.glob("*.py")}


def test_trace_command_and_llm_tool_surface_are_preserved():
    root = Path(trace.__file__).resolve().parent
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    commands: list[str] = []
    llm_tools: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr == "command":
                commands.append(node.name)
            elif decorator.func.attr == "llm_tool":
                llm_tools.append(node.name)

    assert len(commands) == 12
    assert llm_tools == ["send_random_media"]
    assert {"trace", "register_image", "checkin", "original_image", "random_image"} <= set(commands)
