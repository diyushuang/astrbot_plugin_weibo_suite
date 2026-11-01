# 微博全链路辅助插件

AstrBot 插件源码仓库，整合微博监控转发、图片溯源/签到，以及归档、图床管线和文案清洗能力。

插件包目录为 `astrbot_plugin_weibo_suite/`，发布产物输出到 `dist/`。开发和发布约束见
`CONTRIBUTING.md` 与 `开发计划.md`。

## 环境要求

- AstrBot `>=4.28.0,<5`（下限对齐本插件实际用到的 API：`register_web_api`、
  `astrbot.api.web` 的 `request`/`json_response`/`error_response`、
  `StarTools.get_data_dir`，这些在 4.28.0 中已存在且签名一致）。
- Python 3.10+（CI 覆盖 3.12）。
- 平台适配器：`aiocqhttp`。

## 快速开始

```bash
pip install -r astrbot_plugin_weibo_suite/requirements.txt -r requirements-dev.txt
```

把 `astrbot_plugin_weibo_suite/` 整个目录放进 AstrBot 的 `data/plugins/`，
重载插件后在 WebUI 里配置。插件自带七页签管理面板（总览 / 微博监控 / 归档与图床 /
文案库 / 溯源与签到 / 错误日志 / 多平台订阅）。

## 本地校验

```bash
ruff check .                                        # 静态检查（CI 门禁）
pytest -q                                           # 全量测试
python -m astrbot_plugin_weibo_suite.scripts.build_release --check   # 发布清单
```

## 隐私与数据边界

本仓库**不包含**任何运行时数据。以下内容一律不入库，也不会出现在发布包里
（见 `.gitignore`）：

- 凭据与会话：`.env*`、`cookie*`/`*_cookie*`/`credentials*`/`sessions*`、
  `sns_state.json`、`state.json`、`**/sns_data/`。
- 归档与文案原文：`*.db`（含 WAL/SHM）、`copylib/`（文案导出，正文是真实原创内容）、
  `data/plugin_data/`。
- 日志：`*.log`（含 `redact_url()` 脱敏后的直链仍然只保留 scheme/host/path）。

登录态（Cookie）只保存在浏览器 profile 或系统凭据存储里，不写日志、不进配置文件回显。
图床直链在写日志前统一经 `redact_url()` 去掉查询串；面板的「凭据」接口只返回脱敏值。

如需核对文案清洗的黄金用例，请自备参考数据并显式指定路径，脚本不会回落到任何
内置本机路径：

```bash
python astrbot_plugin_weibo_suite/scripts/verify_cleaner_golden.py --reference-root <目录>
# 或 export WEIBO_CLEANER_REFERENCE_ROOT=<目录>
```
