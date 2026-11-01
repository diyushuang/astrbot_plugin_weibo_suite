# 微博全链路辅助插件

[![CI](https://github.com/diyushuang/astrbot_plugin_weibo_suite/actions/workflows/ci.yml/badge.svg)](https://github.com/diyushuang/astrbot_plugin_weibo_suite/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.28.0%2C%3C5-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)

一个 AstrBot 插件，把四条业务线装在一起：**微博监控转发**、**整条微博归档与图床上传**、
**图片溯源与签到**、**多平台订阅（抖音 / 小红书 / Instagram）**，并提供统一的管理面板。

插件包目录是 `astrbot_plugin_weibo_suite/`，发布产物输出到 `dist/`。

## 特性

- **微博监控转发**：免 Cookie 游客身份轮询、推送队列与重试、@全体闸门、视频直发、
  群相册（实况图转 GIF、pid 级去重）。
- **整条微博归档**：监控命中即增量归档，支持全量回填、断点续跑、风控自动暂停；
  SQLite 账本记录帖子与媒体六态，原图按需下载。
- **图床与向量管线**：原图上传 → 缩略图 → pHash/向量入库一条链路，支持
  CloudFlare-ImgBed、通用 HTTP 图床与 R2/OCI 兼容对象存储。
- **图片溯源与签到**：pHash + 向量双引擎找原图，签到卡片文案直接读归档正文。
- **多平台订阅**：三条取数通道（浏览器 / Cookie HTTP / RSS），风控友好调度，
  保号优先。
- **统一面板**：七个页签，危险操作二次确认，后台任务可轮询进度。

## 安装

要求 AstrBot `>=4.28.0,<5`（下限对齐插件实际用到的 `register_web_api`、
`astrbot.api.web` 的 `request`/`json_response`/`error_response`、
`StarTools.get_data_dir`，这些在 4.28.0 中已存在且签名一致）、Python 3.10+、
平台适配器 `aiocqhttp`。

```bash
pip install -r astrbot_plugin_weibo_suite/requirements.txt -r requirements-dev.txt
```

把 `astrbot_plugin_weibo_suite/` 整个目录放进 AstrBot 的 `data/plugins/`，重载插件，
在 WebUI 的插件详情页里配置。面板入口在「插件 → 微博全链路助手 → 详情页」。

## 文档

| 文档 | 内容 |
| --- | --- |
| [插件说明](astrbot_plugin_weibo_suite/README.md) | 安装、首次配置、四个域能做什么、多平台订阅的三条通道与保号参数 |
| [指令手册](docs/COMMANDS.md) | 53 条指令的别名、权限、参数与示例 |
| [配置手册](docs/CONFIGURATION.md) | 15 个配置分组、179 个配置键、敏感字段与旧布局迁移键 |
| [面板说明](docs/WEBUI.md) | 七个页签、35 条面板路由、鉴权边界、常见问题 |
| [架构说明](docs/ARCHITECTURE.md) | 分层与依赖方向、一条微博的完整路径、生命周期、存储布局、已知取舍 |
| [安全策略](astrbot_plugin_weibo_suite/SECURITY.md) | 漏洞报告渠道、支持版本、设计上的安全边界 |
| [更新日志](astrbot_plugin_weibo_suite/CHANGELOG.md) | 逐版本记录，按「症状 → 根因 → 修复」写 |
| [参与开发](CONTRIBUTING.md) | 环境、四条门禁、代码组织、注释与文档规范、提交规范 |
| [开发计划](开发计划.md) | 阶段目标与推进顺序 |

## 本地校验

```bash
ruff check .                                                        # 静态检查（CI 门禁）
ruff format --check .                                               # 格式（CI 门禁）
pytest -q                                                           # 全量测试
python -m astrbot_plugin_weibo_suite.scripts.build_release --check   # 发布清单
```

CI 在 Python 3.10 与 3.12 上跑同样的门禁，见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## 隐私与数据边界

本仓库**不包含**任何运行时数据。以下内容一律不入库，也不会出现在发布包里
（见 [`.gitignore`](.gitignore)）：

- 凭据与会话：`.env*`、`cookie*`/`*_cookie*`/`credentials*`/`sessions*`、
  `sns_state.json`、`state.json`、`**/sns_data/`。
- 归档与文案原文：`*.db`（含 WAL/SHM）、`copylib/`（文案导出，正文是真实原创内容）、
  `data/plugin_data/`。
- 日志：`*.log`（写盘前经 `redact_url()` 脱敏，直链只保留 scheme/host/path）。

登录态（Cookie）只保存在浏览器 profile 或系统凭据存储里，不写日志、不进配置文件回显。
图床直链在写日志前统一经 `redact_url()` 去掉查询串；面板的「凭据」接口只返回脱敏值。
测试夹具、注释与 `CHANGELOG.md` 里的示例一律使用 `example.com`、`1000000001`、
`示例博主` 这类占位值。

如需核对文案清洗的黄金用例，请自备参考数据并显式指定路径，脚本不会回落到任何
内置本机路径：

```bash
python astrbot_plugin_weibo_suite/scripts/verify_cleaner_golden.py --reference-root <目录>
# 或 export WEIBO_CLEANER_REFERENCE_ROOT=<目录>
```

## 致谢与边界

本插件复用了 AstrBot 的插件机制与 WebUI 基础设施，不包含 AstrBot 本体源码。
微博、抖音、小红书、Instagram 的接口与风控策略均由其各自平台决定，本插件只降低
触发风控的概率，不保证账号不受平台处罚；使用前请自行评估账号风险。

## 许可

[MIT](LICENSE)
