"""P0 发布面质量门禁。"""

from __future__ import annotations

from astrbot_plugin_weibo_suite.scripts import build_release


def test_manifest_contains_only_existing_runtime_files():
    files = build_release.verify_manifest()
    assert files
    assert all(path.is_file() for path in files)
    assert all("__pycache__" not in path.parts for path in files)


def test_every_runtime_python_module_is_registered():
    root = build_release.plugin_root()
    listed = {path.relative_to(root).as_posix() for path in build_release.verify_manifest()}
    runtime = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if not set(path.relative_to(root).parts) & build_release.EXCLUDED_PARTS
    }
    assert runtime <= listed
