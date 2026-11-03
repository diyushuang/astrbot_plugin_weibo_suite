"""生成「schema 键 → 页面/分组 → 控件类型 → 是否可写」映射表。

用法：

    python scripts/gen_settings_map.py            # 写入 docs/SETTINGS_MAP.md
    python scripts/gen_settings_map.py --stdout    # 只打印，不写文件

内容全部来自 `views_backend/settings_schema.build_catalog()`，与设置页运行时
使用同一份目录，避免文档与页面漂移。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 本地检出的目录名不一定是 astrbot_plugin_weibo_suite（AstrBot 安装时才一致）。
# 把仓库根放进 sys.path 后，views_backend 作为顶层包导入即可，不依赖包别名。
sys.path.insert(0, str(ROOT))

from views_backend.settings_schema import build_catalog, iter_fields  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "SETTINGS_MAP.md"

WIDGET_LABEL = {
    "switch": "开关",
    "select": "下拉",
    "number": "数字",
    "password": "密码（脱敏）",
    "text": "单行文本",
    "textarea": "多行文本",
    "list": "列表（每行一项）",
    "json": "JSON 编辑器",
}


def _cell(value: object) -> str:
    text = str(value).replace("|", "\\|").replace("\n", "\\n")
    return text


def _default_text(field: dict) -> str:
    if field["secret"]:
        return "（空）"
    value = field["default"]
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _constraint(field: dict) -> str:
    if field["options"]:
        return " / ".join(f"{item['value']}={item['label']}" for item in field["options"])
    if field["min"] is not None and field["max"] is not None:
        unit = f" {field['unit']}" if field["unit"] else ""
        return f"{field['min']}–{field['max']}{unit}"
    if field["type"] == "list":
        return "每行一项"
    if field["type"] in ("object", "dict"):
        return "JSON 对象"
    return ""


def _note(field: dict) -> str:
    notes = []
    if field["secret"]:
        notes.append("脱敏")
    if field["danger"]:
        notes.append("危险")
    if field["managed"]:
        notes.append("指令维护")
    if field["plaintext"]:
        notes.append("明文 JSON")
    if field["advanced"]:
        notes.append("高级")
    if field["condition"]:
        notes.append("；".join(item["label"] for item in field["condition"]))
    return " · ".join(notes)


def render() -> str:
    catalog = build_catalog()
    counts = catalog["counts"]
    lines = [
        "# 设置页键映射",
        "",
        "由 `scripts/gen_settings_map.py` 从 `_conf_schema.json` 与",
        "`views_backend/settings_schema.py` 生成，请勿手改。",
        "",
        f"- 主题页：{counts['pages']} 个（另有 1 个「关于与安全」页，不含配置键）",
        f"- schema 可见分组：{counts['groups']} 个",
        f"- 配置键：{counts['keys']} 个；可写：{counts['writable']} 个",
        f"- 脱敏字段：{counts['secrets']} 个",
        "",
        "| 页面 | 分组 | 配置键 | 短标签 | 控件 | 可写 | 默认值 | 范围 / 选项 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for page, group, field in iter_fields(catalog):
        lines.append(
            "| "
            + " | ".join(
                _cell(item)
                for item in (
                    page["title"],
                    group["title"],
                    f"`{field['key']}`",
                    field["label"],
                    WIDGET_LABEL.get(field["widget"], field["widget"]),
                    "是" if field["writable"] else "否",
                    _default_text(field),
                    _constraint(field),
                    _note(field),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出路径")
    args = parser.parse_args()
    text = render()
    if args.stdout:
        print(text)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"已写入 {args.out}（{len(text.splitlines()) - 8} 行配置项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
