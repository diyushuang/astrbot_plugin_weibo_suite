# 更新日志

## v0.5.0 - 2026-10-31

- P4：新增图床 / 缩略图 / 向量入库管线（F2）。归档完成后按 `pipeline.enabled` +
  `pipeline.auto_queue` + `pipeline.enabled_uids` 自动入队，单帖一次性完成原图
  上传、720px JPEG q72 缩略图、pHash 与向量入库。
- 命名规则独立成纯函数模块 `weibo/pipeline_naming.py`：默认目录
  `{category}/【微博图集@{account}】/`、默认文件名
  `【微博@{account}】{date}-{index}：{title}.jpg`，与现网 2.4 万点同构，
  `/原图` 可按名直查；非法字符清洗、超长标题截断、非 jpg 扩展名替换而非叠加。
- 溯源域新增三件套接口：`ImageBedClient.store(..., folder=)`（LOCAL / CFB / R2 /
  OCI / generic_http 统一目录语义）、`compute_features_with_thumbnail(...)`
  （一次解码同时产出 pHash 与缩略图，不放大、去 EXIF/ICC）、
  `VectorEngine.upsert_file_batch(...)`（整帖一次 PUT，embed 并发上限 4，
  `thumb_url` 写同一 Qdrant 点，空 `thumb_url` 时载荷与旧版逐键一致）。
- 六态进度落 `media` 表（downloaded→album→bed→thumb→vector）断点续跑；
  单图失败 `bump_media_attempt` 记次并原地写错误，达 `pipeline.max_attempts`
  （默认 3）归 `exhausted`，不阻塞后续帖。
- 队列语义：`asyncio.Queue` 单 worker 串行，失败退避 5→10→20→40→60s 封顶、
  成功即重置；`pending` 集合去重；停机幂等排空；`PIPELINE_QUEUE_MAX=500`。
- CFB 模式（`bed_mode` 为空或 `cloudflare_imgbed`）由图床侧钩子自动入库，
  插件侧只补原图落盘，绝不推进 bed/thumb/vector 状态。
- 原图一律读归档本地库，缺失时最多补调一次 `archive_download_post`，不重复下载。
- 新增 2 条指令（共 25 条 `微博` 子指令）：`微博管线状态`、`微博管线补跑`
  （管理员，按归档库扫描未完成帖重新入队）。
- 归档侧 `_archive_save_post` 在下载入队后通知管线（`pipeline_notify_archived`），
  通知或入队失败只记 warning，不影响推送与落账链路。
- 测试：新增 95 个用例（命名 49 / 服务接口 22 / 管线编排 24），全仓 612 项通过。

## v0.4.0 - 2026-10-31

- P3：新增整条微博归档域（F1）。`archive.db`（SQLite WAL）成为帖子、媒体、
  回填游标的单一事实源，落盘目录 `archive/<uid>/<mid>/<pid>.<ext>`。
- 增量归档：监控命中后同步落账（元数据 + 图集），原图交给旁路队列异步下载；
  归档任何异常只记 warning，不影响推送；同一 mid 重复归档幂等、状态不重置。
- 全量回填 `微博回填 <uid> [--since] [--full]`：分页翻到底、断点游标续跑、
  页间节流、风控自动暂停；`微博回填状态`/`微博回填停止` 可观测可中止。
- JSON 种子导入 `微博归档导入 <文件>`：兼容示例博主A汇总两账号现成数据
  （758 + 1376 条），uid 三级判定，缺图信息按 `pic_total=-1`（未知）入账。
- 对账 `微博对账 [uid]`：帖/图/落盘三方核对，输出"正文缺失、图数不符、
  原图未落盘"三类缺口与结论行。
- 媒体状态机（pending→downloaded→album→bed→thumb→vector）rank 单调不回退，
  为 P4 图床/缩略图/向量化管线预留状态位。
- 相册发送改为归档本地优先：命中已落盘原图直接复用，省一次下载。
- 新增 6 条指令（共 23 条 `微博` 子指令），`微博状态` 增加归档摘要行。

## v0.3.0 - 2026-10-31

- P2：将微博转发域从 5703 行入口拆为 visitor/api/parser/monitor/pusher/album/
  commands，17 条 `微博` 指令全部保留。
- 建立溯源域与微博域的统一复合入口，13 条 `/` 指令与 `sendRandomMedia` 保持可达。
- 共享件收敛：代理/UMO/Cookie、相册规则序列化、关键词 upsert、风控剩余分钟、
  NapCat base64 载荷单一实现。
- 修复：视频完成路径接入 10 秒状态防抖；`@全体` 通知表按 TTL 清理；同机相册
  文件名扫描改为台账 miss 时的 10 分钟缓存兜底；移除接口重试循环不可达死代码。

## v0.2.0 - 2026-10-31

- P1：平移图片溯源域 15 个能力模块与入口，保留 `/溯源`、`/登记原图`、`/原图`、
  `/随机图`、`/随机视频`、`/签到` 等全部指令与 `sendRandomMedia` LLM 工具。
- 将 SSRF 钉扎、受限流式下载、共享会话、通用配置工具与错误留存下沉到 `shared/`。
- NapCat 错误分类与载荷工具箱下沉，供相册与溯源投递共用。
- 完整平移旧项目 22 个测试文件，并新增共享 HTTP、NapCat、错误留存和迁移结构门禁。

## v0.1.0 - 2026-10-31

- P0：建立按域拆分的插件包、统一元数据、CI、测试入口和发布白名单。
- 采用插件标识 `astrbot_plugin_weibo_suite`，最终版本号留待 P7 发布 v1.0.0。
