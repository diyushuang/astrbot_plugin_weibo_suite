"""一次性核对：cleaner.py 清洗结果 vs Node 版 build_weibo_copy.js 产出的 CSV 文案列。

不进 CI —— 依赖仓库外的参考数据，用 `--reference-root` 或环境变量
`WEIBO_CLEANER_REFERENCE_ROOT` 指定；未配置或目录不存在时直接跳过，只在本地回归时手动跑：

    .venv/Scripts/python.exe astrbot_plugin_weibo_suite/scripts/verify_cleaner_golden.py

对齐口径：CSV「微博链接」末段 == 原始 JSON 的 mb 字段；CSV「文案」列 == clean 的输出。
trunc 条目用 t[:from] 当列表接口的截断文本、t 当补回的全文。
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_ROOT_ENV = "WEIBO_CLEANER_REFERENCE_ROOT"
UIDS = ("1000000001", "1000000002")
ZWJ = "\u200d"
VS16 = "\ufe0f"


def _load_cleaner():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from astrbot_plugin_weibo_suite.weibo.cleaner import clean_mblog_text

    return clean_mblog_text


def _read_csv(path: Path) -> dict[str, str]:
    by_mid: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        i_text = header.index("文案")
        i_link = header.index("微博链接")
        for row in reader:
            if len(row) <= max(i_text, i_link):
                continue
            mid = row[i_link].rstrip("/").rsplit("/", 1)[-1]
            by_mid[mid] = row[i_text]
    return by_mid


def _case(record: dict) -> tuple[str, str, str]:
    text = record["t"]
    truncated_from = (record.get("trunc") or {}).get("from")
    if truncated_from:
        return text[: int(truncated_from)], text, record["mb"]
    return text, "", record["mb"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对 cleaner 与参考 CSV 的一致性")
    parser.add_argument("--reference-root", type=Path, default=None)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    env_root = os.environ.get(REFERENCE_ROOT_ENV, "").strip()
    root: Path | None = args.reference_root or (Path(env_root) if env_root else None)
    if root is None or not root.is_dir():
        print(f"未配置参考数据目录（--reference-root 或 {REFERENCE_ROOT_ENV}），跳过核对。")
        return 0
    clean = _load_cleaner()

    total = 0
    matched = 0
    zwj_raw = zwj_full = zwj_out = 0
    per_uid: dict[str, int] = {}
    mismatches: list[tuple[str, str, str, str]] = []

    for uid in UIDS:
        by_mid = _read_csv(root / "output" / f"微博原创文案_{uid}.csv")
        records = json.loads(
            (root / "data" / "weibo" / f"{uid}_original_full.json").read_text(encoding="utf-8")
        )
        seen = 0
        for record in records:
            raw, long_text, mid = _case(record)
            expected = by_mid.get(mid)
            if expected is None:
                continue
            got = clean(raw, long_text)
            seen += 1
            zwj_raw += raw.count(ZWJ)
            if long_text:
                zwj_full += long_text.count(ZWJ)
            zwj_out += got.count(ZWJ)
            if got == expected:
                matched += 1
            else:
                mismatches.append((uid, mid, expected, got))
        per_uid[uid] = seen
        total += seen

    print(f"参考数据根目录：{root}")
    print(f"可对齐条目：{total}（{per_uid}）")
    print(f"逐字符一致：{matched}；不一致：{len(mismatches)}")
    print(f"ZWJ 计数：raw 合计 {zwj_raw}，long_text 补回 {zwj_full}，成品合计 {zwj_out}")

    for uid, mid, expected, got in mismatches[:10]:
        print(f"\n--- 不一致 uid={uid} mid={mid}")
        print(f"expected: {expected!r}")
        print(f"got     : {got!r}")
        matcher = difflib.SequenceMatcher(None, expected, got)
        for opcode in matcher.get_opcodes()[:6]:
            print(f"  {opcode}")
        print(f"  similarity={matcher.ratio():.4f}")
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
