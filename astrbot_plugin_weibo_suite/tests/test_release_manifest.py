"""P2-4 发布清单硬校验：--check 不能假通过。

原来的 verify_manifest 有两个假通过面：
1. 版本用**子串包含**判断——metadata.yaml 里留一行注释 `# version: v0.8.1`
   就能让 v0.9.9 的包通过；
2. 只比对 `*.py` 是否登记——views/ 下新增的 js/html/css、data/ 下的资源漏
   登记也能通过，发布包里缺文件却要等用户打开面板才发现 404。

这里用"拷贝一份仓库到 tmp_path 再改坏"的方式验证负例，避免动真实工作区。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.scripts import build_release

# 拷贝整个插件目录太慢，只拷发布面需要的部分
_COPY_ITEMS = (
    "main.py",
    "constants.py",
    "metadata.yaml",
    "requirements.txt",
    "_conf_schema.json",
    "logo.png",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "__init__.py",
    "weibo",
    "shared",
)


def _fake_root(tmp_path: Path) -> Path:
    source = build_release.plugin_root()
    target = tmp_path / build_release.PLUGIN_NAME
    target.mkdir()
    for item in _COPY_ITEMS:
        src = source / item
        dst = target / item
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
            )
        else:
            shutil.copy2(src, dst)
    for folder in ("views", "trace", "sns", "views_backend", "data"):
        src = source / folder
        if src.is_dir():
            shutil.copytree(
                src, target / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
    (target / "views" / "dashboard").mkdir(parents=True, exist_ok=True)
    return target


def test_clean_copy_passes(tmp_path):
    root = _fake_root(tmp_path)
    build_release.verify_manifest(root)


def test_unregistered_non_python_dir_fails(tmp_path):
    """新增一个顶层目录（非 .py 内容）→ 必须失败，以前只查 .py 会放过。"""
    root = _fake_root(tmp_path)
    extra = root / "assets"
    extra.mkdir()
    (extra / "icon.svg").write_text("<svg/>", encoding="utf-8")
    with pytest.raises(ValueError, match="未登记"):
        build_release.verify_manifest(root)


def test_files_inside_whitelisted_dir_are_auto_included(tmp_path):
    """白名单目录内的新增文件自动进包（这是设计意图，不算未登记）。"""
    root = _fake_root(tmp_path)
    (root / "views" / "dashboard" / "extra-widget.js").write_text("// x", encoding="utf-8")
    files = {path.relative_to(root).as_posix() for path in build_release.verify_manifest(root)}
    assert "views/dashboard/extra-widget.js" in files


def test_unregistered_root_file_fails(tmp_path):
    root = _fake_root(tmp_path)
    (root / "NEW_NOTES.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="未登记"):
        build_release.verify_manifest(root)


def test_metadata_version_mismatch_fails(tmp_path):
    root = _fake_root(tmp_path)
    path = root / "metadata.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"version: {build_release.EXPECTED_VERSION}", "version: v9.9.9"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        build_release.verify_manifest(root)


def test_metadata_version_cannot_be_spoofed_by_comment(tmp_path):
    """旧实现用子串包含：留一行注释就能骗过校验。"""
    root = _fake_root(tmp_path)
    path = root / "metadata.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"version: {build_release.EXPECTED_VERSION}", "version: v9.9.9"
    )
    text += f"\n# version: {build_release.EXPECTED_VERSION}\n"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        build_release.verify_manifest(root)


def test_constants_version_mismatch_fails(tmp_path):
    root = _fake_root(tmp_path)
    path = root / "constants.py"
    text = path.read_text(encoding="utf-8").replace(
        f'PLUGIN_VERSION = "{build_release.EXPECTED_VERSION}"',
        'PLUGIN_VERSION = "v9.9.9"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        build_release.verify_manifest(root)
