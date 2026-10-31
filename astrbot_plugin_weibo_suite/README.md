# 微博全链路助手

当前已完成 P1/P2/P3/P4：图片溯源域、微博监控域、整条微博归档域与图床向量
管线均已可用，统一入口可加载。

- 溯源与交付：`/溯源`、`/登记原图`、`/原图`、`/溯源重扫`、`/溯源删除`
- 媒体：`/随机图`、`/随机视频`、`sendRandomMedia`
- 签到：`/签到`、`/签到文案导入`
- 共享底座：SSRF 防护、IP 钉扎、受限流式下载、错误留存、NapCat 错误分类

微博监控：`微博` 25 条指令、免 Cookie 游客身份、推送队列、视频直发与群相册。

微博归档（P3）：`微博回填 <uid> [--since] [--full]`、`微博回填状态`、
`微博回填停止`、`微博归档导入 <文件>`、`微博归档查询 [uid] [页码]`、`微博对账 [uid]`。
帖子与媒体账本落在 `data_dir/archive.db`（SQLite WAL），原图落在
`data_dir/archive/<uid>/<mid>/`；监控命中即增量归档，原图旁路下载，回填支持
断点续跑与风控自动暂停。

图床/缩略图/向量管线（P4）：归档完成后自动入队（受 `pipeline.enabled`、
`pipeline.auto_queue`、`pipeline.enabled_uids` 约束），单帖一次走完原图上传 →
720px JPEG q72 缩略图 → pHash/向量入库。命名默认
`{category}/【微博图集@{account}】/` + `【微博@{account}】{date}-{index}：{title}.jpg`，
与现网同构，`/原图` 可按名直查；缩略图写入同一 Qdrant 点的 `thumb_url`。
六态进度（downloaded→album→bed→thumb→vector）落 `archive.db` 的 `media` 表，
断点续跑、单 worker 串行、失败退避 5→60s。对应指令：`微博管线状态`、
`微博管线补跑`（管理员）。CFB 图床模式只补原图落盘，其余状态交由图床侧钩子。

签到文案库（P5）与统一面板（P6）将按 `开发计划.md` 继续接入。
