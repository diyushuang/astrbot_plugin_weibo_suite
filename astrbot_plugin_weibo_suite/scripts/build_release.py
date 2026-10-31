"""校验并构建 AstrBot 发布包。

白名单是发布面的唯一事实源：运行期新增文件未登记时 `--check` 必须失败，
避免本地测试通过、发布包却因 ImportError 无法加载。
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_weibo_suite"
EXPECTED_VERSION = "v0.5.0"

# 目录项以 / 结尾，表示递归收录；文件项必须精确匹配。开发/测试/缓存文件不在白名单。
PLUGIN_FILES = (
    "__init__.py",
    "main.py",
    "composite.py",
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
    "views/",
    "views_backend/",
    "data/",
)

EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", "tests", "scripts"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".part"}


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


def verify_manifest(root: Path | None = None) -> list[Path]:
    base = root or plugin_root()
    metadata = (base / "metadata.yaml").read_text(encoding="utf-8")
    if f"version: {EXPECTED_VERSION}" not in metadata:
        raise ValueError(f"metadata.yaml 版本与 {EXPECTED_VERSION} 不一致")
    constants = (base / "constants.py").read_text(encoding="utf-8")
    if f'PLUGIN_VERSION = "{EXPECTED_VERSION}"' not in constants:
        raise ValueError(f"constants.py 版本与 {EXPECTED_VERSION} 不一致")
    # 微博域历史上自己又定义了一份 PLUGIN_VERSION（日志里显示用）；两处不同步
    # 会让"版本号到底是多少"取决于谁读的，这里一并校验。
    domain_constants = base / "weibo" / "constants.py"
    if domain_constants.is_file():
        text = domain_constants.read_text(encoding="utf-8")
        if 'PLUGIN_VERSION = "' in text and f'PLUGIN_VERSION = "{EXPECTED_VERSION}"' not in text:
            raise ValueError(f"weibo/constants.py 版本与 {EXPECTED_VERSION} 不一致")

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

    all_runtime_py = {
        path.relative_to(base).as_posix()
        for path in base.rglob("*.py")
        if not set(path.relative_to(base).parts) & EXCLUDED_PARTS
    }
    unlisted = sorted(all_runtime_py - present)
    if unlisted:
        raise ValueError(f"运行期 Python 文件未登记进 PLUGIN_FILES: {unlisted}")
    return files


def build(root: Path | None = None, output_dir: Path | None = None) -> Path:
    base = root or plugin_root()
    files = verify_manifest(base)
    destination = output_dir or base.parent / "dist"
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
