# 微博全链路助手

[![CI](https://github.com/diyushuang/astrbot_plugin_weibo_suite/actions/workflows/ci.yml/badge.svg)](https://github.com/diyushuang/astrbot_plugin_weibo_suite/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.28.0%2C%3C4.30-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)

一个 AstrBot 插件，把四条业务线装在一起：**微博监控转发**、**整条微博归档与图床上传**、
**图片溯源与签到**、**多平台订阅（抖音 / 小红书 / Instagram）**，并提供统一的管理面板。

仓库根目录就是插件目录（AstrBot 要求 `metadata.yaml` 与 `main.py` 位于仓库根），
克隆到 `data/plugins/` 后目录名即插件标识 `astrbot_plugin_weibo_suite`。

| 你想做的事 | 从这里开始 |
| --- | --- |
| 装插件、配第一组参数 | 本文「安装」「快速上手」 |
| 查某个指令怎么用 | [指令手册](docs/COMMANDS.md) |
| 查某个配置项的含义与默认值 | [配置手册](docs/CONFIGURATION.md) |
| 用面板操作 | [面板说明](docs/WEBUI.md) |
| 改代码 / 提交 PR | [架构说明](docs/ARCHITECTURE.md)、[贡献指南](CONTRIBUTING.md) |

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
- **统一面板**：八个页签（含全量设置页），危险操作二次确认，后台任务可轮询进度。

## 安装

要求 AstrBot `>=4.28.0,<4.30`（下限对齐插件实际用到的 `register_web_api`、
`astrbot.api.web` 的 `request`/`json_response`/`error_response`、
`StarTools.get_data_dir`，这些在 4.28.0 中已存在且签名一致）、Python 3.10+、
平台适配器 `aiocqhttp`。

```bash
git clone https://github.com/diyushuang/astrbot_plugin_weibo_suite.git \
  <AstrBot>/data/plugins/astrbot_plugin_weibo_suite
pip install -r requirements.txt
```

也可以直接把仓库打成 zip 解压到 `data/plugins/astrbot_plugin_weibo_suite/`。重载插件后
在 WebUI 的「插件 → 微博全链路助手 → 详情页」里配置；想用 `/随机图`、`/溯源` 这类
图床能力，还需要在面板里做一次图床配置，只用微博监控可以跳过。

## 快速上手

第一次通常只需要三件事：

1. `monitor.monitored_uids` 填要监控的微博 uid（也可以之后用 `微博 添加 <uid>`）；
2. 在目标群里执行 `微博 绑定`，把这个群设为推送目标；
3. 确认 `monitor.poll_interval`（默认 120 秒）没有低到会被微博限流。

归档、图床管线、多平台订阅默认是关闭或保守的，按需打开。全部配置项见
[配置手册](docs/CONFIGURATION.md)。

## 四个域能做什么

### 微博监控转发

免 Cookie 的游客身份轮询、推送队列（含取消与重试）、@全体闸门、视频直发、
群相册（含实况图转 GIF 与 pid 级去重）。指令入口是 `微博` 分组，共 30 条，
全部列在[指令手册](docs/COMMANDS.md)。

### 整条微博归档

监控命中即增量归档，也可以 `微博 回填 <uid>` 做全量回填（支持 `--since` 与
`--full`、断点续跑、风控自动暂停）。帖子与媒体账本落在 `data_dir/archive.db`
（SQLite WAL），原图落在 `data_dir/archive/<uid>/<mid>/`。

**归档是旁路**：它永远不会反压监控与推送主链；队列满时丢弃并在面板计数，
原图下载也可以随时暂停/续跑。磁盘余量低于下限时会自动暂停并在面板提示。

### 图床与向量管线

归档完成后单帖一次走完：原图上传 → 720px JPEG q72 缩略图 → pHash/向量入库。
命名默认 `{category}/【微博图集@{account}】/` + `【微博@{account}】{date}-{index}：{title}.jpg`，
可按名直查。六态进度（downloaded → album → bed → thumb → vector）落在
`archive.db` 的 `media` 表，支持断点续跑、单 worker 串行、失败退避 5→60 秒。

图床支持 CloudFlare-ImgBed、通用 HTTP 图床、R2/OCI 兼容对象存储；图床上传的
鉴权码默认走 `X-Auth-Code` 请求头（不进 URL），老站点只认 `?authCode=` 时会
自动回落一次并告警。

### 图片溯源与签到

`/溯源` 用 pHash 与向量双引擎在本地图库/图床索引里找原图，`/登记原图` 建索引，
`/随机图`、`/随机视频` 出图，`/签到` 生成签到卡片。签到文案直接读归档库的
`cleaned_text`（与 Node 版清洗逐字符一致，90 条黄金样本对拍零差异），
归档不可用时回退 CSV/MD。

### 多平台订阅

`订阅 抖音 <主页链接>`、`订阅 小红书 <主页链接>`、`订阅 IG <用户名>` 即可订阅，
有更新自动推到通知群。抖音/小红书/IG 的时间线接口都要求登录态，所以取数有三条
通道（面板「取数通道」切换）：

- `browser`（默认，最稳）：playwright 起一个持久化浏览器，你在窗口里登录一次，
  之后每次检查都在同一会话里打开博主主页。Cookie 从不离开浏览器、不落盘、
  不进日志，TLS/指纹/出口 IP 与真人一致，风控压力最小。需要本机已装
  Microsoft Edge（或 `playwright install chromium`）。首次用
  `订阅 登录 抖音` / `小红书` / `IG` 弹窗登录，`订阅 状态` 看到「已登录」即可。
- `http`：粘一份导出 Cookie 走纯 HTTP，快，但 TLS 指纹与浏览器不同，风控概率
  明显更高；抖音这条通道还必须能从 Cookie 里取到 `UIFID`（列表接口要签名）。
- `rss`：RSSHub 一类自建源，只解析，不碰平台风控。`订阅 RSS <地址>` 是
  「平台实在取不动」时的备选。

**保号是第一目标**，默认参数都很保守：抖音/小红书基础间隔 300 秒、IG 900 秒，
抖动只往上加（绝不提前），0–8 点自动降速一倍，账号越多间隔线性拉长，新账号
30 分钟热身、24 小时保号期，单轮最多推 3 条，只取首页不翻页、不查评论不查粉丝。
命中风控按信号加权指数冷却，健康度低于 0.4 自动降速、低于 0.2 自动暂停；
同一平台 10 分钟内 3 次风控则整平台熔断 30 分钟；需要重新登录的账号只告警，
绝不反复撞验证码。

延迟是这套保守参数的代价，如实写在这里：抖音/小红书 P50 约 150 秒、P95 约
300 秒，IG P50 约 450 秒、P95 约 900 秒。面板里有「激进档」（120/180/300 秒）
可以自己打开，但提示会标红——那是在拿账号换速度。

## 文档

| 文档 | 内容 |
| --- | --- |
| [指令手册](docs/COMMANDS.md) | 53 条指令的别名、权限、参数与示例 |
| [配置手册](docs/CONFIGURATION.md) | 15 个配置分组、179 个配置键、敏感字段与旧布局迁移键 |
| [面板说明](docs/WEBUI.md) | 八个页签、37 条面板路由、全量设置页、鉴权边界、常见问题 |
| [架构说明](docs/ARCHITECTURE.md) | 分层与依赖方向、一条微博的完整路径、生命周期、存储布局、已知取舍 |
| [安全策略](SECURITY.md) | 漏洞报告渠道、支持版本、设计上的安全边界 |
| [更新日志](CHANGELOG.md) | 逐版本记录，按「症状 → 根因 → 修复」写 |
| [贡献指南](CONTRIBUTING.md) | 环境、四条门禁、代码组织、注释与文档规范、提交规范 |
| [整合计划](docs/ROADMAP.md) | 阶段目标、推进顺序与设计取舍（历史记录） |

## 目录结构

```text
.
├── main.py                 # 唯一注册指令的入口（@filter 只出现在这里）
├── constants.py            # 跨域共享常量
├── metadata.yaml           # AstrBot 插件元数据
├── _conf_schema.json       # 面板配置 schema（15 个分组）
├── logo.png                # 插件市场图标（1:1）
├── requirements.txt
├── weibo/                  # 微博监控、归档、管线、文案清洗
├── trace/                  # 图片溯源、图床、向量、签到（门面 + 9 个 mixin）
├── sns/                    # 多平台订阅（抖音 / 小红书 / IG / RSS）
├── shared/                 # HTTP 底座、SSRF 钉扎、错误留存等通用件
├── views/dashboard/        # 面板静态资源（无构建步骤）
├── views_backend/          # 面板 API 路由
├── data/                   # 随包分发的静态资源（emoji-props.json）
├── scripts/                # 发布包构建与黄金样本校验
├── tests/                  # 1200+ 用例
└── docs/                   # 指令 / 配置 / 面板 / 架构 / 计划
```

## 本地校验

```bash
ruff check .                                      # 静态检查（CI 门禁）
ruff format --check .                             # 格式（CI 门禁）
pytest -q                                         # 全量测试
python scripts/build_release.py --check           # 发布清单
```

CI 在 Python 3.10 与 3.12 上跑同样的门禁，另有一个装真 AstrBot 的 job 验证宿主语义，
见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

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

面板端点复用 AstrBot 的会话体系，插件侧没有额外鉴权，**不要直接暴露到公网**。
完整威胁模型见 [SECURITY.md](SECURITY.md)。

## 排查

面板「错误日志」页签保留最近 2000 条告警/错误，写入前已脱敏。常见问题（面板打不开、
改了配置没生效、后台任务卡住、凭据显示为掩码）见[面板说明的常见问题一节](docs/WEBUI.md)。

如需核对文案清洗的黄金用例，请自备参考数据并显式指定路径，脚本不会回落到任何
内置本机路径：

```bash
python scripts/verify_cleaner_golden.py --reference-root <目录>
# 或 export WEIBO_CLEANER_REFERENCE_ROOT=<目录>
```

## 致谢与边界

本插件复用了 AstrBot 的插件机制与 WebUI 基础设施，不包含 AstrBot 本体源码。
微博、抖音、小红书、Instagram 的接口与风控策略均由其各自平台决定，本插件只降低
触发风控的概率，不保证账号不受平台处罚；使用前请自行评估账号风险。

## 许可

[MIT](LICENSE)
