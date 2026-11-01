# `copy_golden.json` 来源与重生成口径

该黄金基准用于锁定 Python `weibo/cleaner.py` 与「示例博主A微博汇总」参考实现
Node 版 `src/build_weibo_copy.js` 的逐字符一致性。`expected` 必须直接取自参考
项目产出的 CSV「文案」列（样例正文已整体替换为脱敏占位文案），不能先运行
Python 再回抄结果。

来源（占位路径，真实数据目录不入库）：

- `reference/weibo-copy/output/微博原创文案_1000000001.csv`
- `reference/weibo-copy/output/微博原创文案_1000000002.csv`
- `reference/weibo-copy/data/weibo/<uid>_original_full.json`

对齐规则：

1. CSV 的「微博链接」末段与 JSON 的 `mb` 字段对齐。
2. 未截断记录：`raw = t`，`long_text = ""`。
3. 截断记录：`raw = t[:trunc.from]`，`long_text = t`；`expected` 仍取 CSV「文案」。
4. `note` 只记录覆盖标签，便于审阅样本，不参与清洗。
5. 样本总量控制在 60~90 条，并覆盖全部 `WB_EMOJI` 映射、同义码、品牌贴纸、
   成对/文末话题、链接与引导语、多行 Markdown 行首、长文补回、ZWJ、肤色。

Node 版 `WB_EMOJI` 实际为 93 个基础码加 4 个同义码，共 97 个键；P5 合同里
“至少 100 个键”的阈值与上游对象不符，实现和回归以 Node 对象本身为准。

本地全量核对（不进 CI，依赖仓库外参考数据）：

```powershell
.\.venv\Scripts\python.exe astrbot_plugin_weibo_suite\scripts\verify_cleaner_golden.py
```
