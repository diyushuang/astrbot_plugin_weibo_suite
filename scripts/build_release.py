"""校验并构建 AstrBot 发布包。

白名单是发布面的唯一事实源：运行期新增文件未登记时 `--check` 必须失败，
避免本地测试通过、发布包却因 ImportError 无法加载。
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_weibo_suite"
EXPECTED_VERSION = "v0.9.1"

# 目录项以 / 结尾，表示递归收录；文件项必须精确匹配。开发/测试/缓存文件不在白名单。
PLUGIN_FILES = (
    "__init__.py",
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
    "weibo/",
    "trace/",
    "shared/",
    "sns/",
    "views/",
    "views_backend/",
    "data/",
)

# 仓库根就是插件根（AstrBot 要求 metadata.yaml/main.py 在仓库根目录），所以
# 覆盖面校验会把仓库级文件也算进来。下面两类是"仓库有、插件不跑"的东西：
# 开发/CI 目录、以及根目录的工程配置文件。
EXCLUDED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    ".git",
    ".mimosa",
    ".workbuddy",
    ".vscode",
    ".idea",
    "tests",
    "scripts",
    "docs",
    ".github",
    "dist",
    "build",
}
EXCLUDED_FILES = {
    "conftest.py",
    "pytest.ini",
    "ruff.toml",
    "requirements-dev.txt",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".part"}

# 版本比对必须取"整行字段值"，不能做子串包含：以前用
# f"version: {EXPECTED_VERSION}" in metadata 判断，metadata 里留一行注释
# "# version: v0.8.1" 就能让 v0.9.9 的包蒙混过关。
_VERSION_LINE_RE = re.compile(r"^version[ \t]*:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_PLUGIN_VERSION_RE = re.compile(r"""^PLUGIN_VERSION[ \t]*=[ \t]*["']([^"']+)["']""", re.MULTILINE)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def iter_manifest_files(root: Path | None = None) -> list[Path]:
    base = root or plugin_root()
    selected: dict[str, Path] = {}
    for entry in PLUGIN_FILES:
        target = base / entry.rstrip("/")
        if entry.endswith("/"):
            if not target.is_dir():
                raise FileNotFoundError(f"白名单目录不存在: {target}")
            paths = (path for path in target.rglob("*") if path.is_file())
        else:
            if not target.is_file():
                raise FileNotFoundError(f"白名单文件不存在: {target}")
            paths = iter((target,))
        for path in paths:
            relative = path.relative_to(base)
            if set(relative.parts) & EXCLUDED_PARTS or path.suffix in EXCLUDED_SUFFIXES:
                continue
            selected[relative.as_posix()] = path
    return [selected[key] for key in sorted(selected)]


def _declared_version(text: str, pattern: re.Pattern[str], source: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"{source} 缺少版本声明")
    return match.group(1)


def verify_versions(base: Path) -> None:
    """三处版本声明必须与 EXPECTED_VERSION 完全相同（不做子串包含）。"""
    checks = [(base / "metadata.yaml", _VERSION_LINE_RE, "metadata.yaml")]
    constants = base / "constants.py"
    if constants.is_file():
        checks.append((constants, _PLUGIN_VERSION_RE, "constants.py"))
    # 微博域历史上自己又定义了一份 PLUGIN_VERSION（日志里显示用）；两处不同步
    # 会让"版本号到底是多少"取决于谁读的，这里一并校验。
    domain_constants = base / "weibo" / "constants.py"
    if domain_constants.is_file():
        checks.append((domain_constants, _PLUGIN_VERSION_RE, "weibo/constants.py"))
    for path, pattern, label in checks:
        if not path.is_file():
            continue
        found = _declared_version(path.read_text(encoding="utf-8"), pattern, label)
        if found != EXPECTED_VERSION:
            raise ValueError(f"{label} 版本 {found!r} 与 {EXPECTED_VERSION!r} 不一致")


def verify_manifest(root: Path | None = None) -> list[Path]:
    base = root or plugin_root()
    verify_versions(base)

    files = iter_manifest_files(base)
    present = {path.relative_to(base).as_posix() for path in files}
    missing_runtime = {
        "main.py",
        "metadata.yaml",
        "requirements.txt",
        "_conf_schema.json",
        "logo.png",
        "LICENSE",
    } - present
    if missing_runtime:
        raise ValueError(f"发布清单缺少运行文件: {sorted(missing_runtime)}")

    # 覆盖面校验针对"所有要发布的文件"，不只是 .py：以前只比 *.py，
    # views/ 下新增的 js/html/css、data/ 下的资源漏登记也能 --check 通过，
    # 发布包缺文件却直到用户打开面板才 404。
    shipped = {
        relative
        for path in base.rglob("*")
        if path.is_file()
        and (relative := path.relative_to(base).as_posix()) not in EXCLUDED_FILES
        and not set(path.relative_to(base).parts) & EXCLUDED_PARTS
        and path.suffix not in EXCLUDED_SUFFIXES
    }
    unlisted = sorted(shipped - present)
    if unlisted:
        raise ValueError(f"文件未登记进 PLUGIN_FILES: {unlisted}")
    return files


def build(root: Path | None = None, output_dir: Path | None = None) -> Path:
    base = root or plugin_root()
    files = verify_manifest(base)
    destination = output_dir or base / "dist"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{PLUGIN_NAME}-{EXPECTED_VERSION}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            arcname = Path(PLUGIN_NAME) / path.relative_to(base)
            bundle.write(path, arcname.as_posix())
    return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验白名单，不构建 zip")
    args = parser.parse_args()
    files = verify_manifest()
    if args.check:
        print(f"发布清单校验通过：{len(files)} 个文件")
        return 0
    print(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
