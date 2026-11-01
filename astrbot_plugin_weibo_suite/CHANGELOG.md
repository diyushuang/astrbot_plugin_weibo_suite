# 更新日志

## Unreleased

本轮为「按 GitHub 通行规范整理文档与仓库结构」，外加两处面板前端与后端字段
不一致导致的真实故障。

### 修掉的真 bug

- **面板「停用 / 启用」订阅按钮永远失败**（`views/dashboard/app.js` 与
  `views_backend/sns_api.py` 的字段名不一致）：面板提交
  `{action, platform, account_id}`，而接口只读 `key`（`platform:account_id`
  全量键）→ 点「停用」恒返回 404「没有这个订阅」，点「移除」恒返回 400
  「缺少平台或账号」。接口现在同时接受 `key`、`account_id` 与 `target` 三种
  写法，并抽出 `_locate_account()` 统一按 `(platform, account_id)` 定位。
  回归用例：`tests/test_sns_api.py::test_accounts_panel_payload_shape_uses_platform_and_account_id`。
- **面板「Cookie 与凭据」列表恒为空**（`app.js::loadSnsCredentials`）：前端读
  `data.credentials`，接口返回的字段是 `rows`。改为读 `rows` 并保留旧字段兼容。
- **凭据接口把本机绝对路径回传给浏览器**：`/sns/credentials` 的
  `profile_dir` 字段返回浏览器 profile 的完整路径，而面板只拿它当「目录建了没」
  的布尔用。改为 `profile_ready: bool`，返回值不再包含任何本机路径。

### 文档

- 新增 `docs/COMMANDS.md`（53 条指令：顶层 12 + 微博 30 + 订阅 11，含别名、
  权限、参数与示例）、`docs/CONFIGURATION.md`（15 个配置分组、179 个配置键、
  敏感字段说明与旧布局迁移键）、`docs/WEBUI.md`（七个页签 + 35 条面板路由 +
  鉴权边界）、`docs/ARCHITECTURE.md`（分层、依赖方向、一条微博的完整路径、
  生命周期、存储布局、横向能力、测试策略、已知取舍）。
- 重写根 `README.md`（特性总览、安装、配置、文档索引、隐私边界、许可）与
  `CONTRIBUTING.md`（环境、四条门禁、代码组织、注释与文档规范、
  Conventional Commits、分支与发布、既有约束）。
- 重写 `astrbot_plugin_weibo_suite/SECURITY.md`：补报告渠道、支持版本、
  设计上的安全边界（凭据/出网/日志脱敏/面板/文件读写）与不在范围内的项。
- 重写 `astrbot_plugin_weibo_suite/README.md`：按域重新组织，删掉阶段编号式
  叙述，补「什么时候需要哪条通道」的说明。
- 新增 `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.yml` 与
  `.github/PULL_REQUEST_TEMPLATE.md`：模板里带脱敏检查项。
- 新增仓库根 `LICENSE`（MIT）。

### 注释

- 补齐 11 个测试模块缺失的模块级 docstring（`tests/__init__.py`、
  `test_common.py`、`test_features.py`、`test_http_client.py`、`test_image_bed.py`、
  `test_image_delivery.py`、`test_library.py`、`test_media_history.py`、
  `test_random_media.py`、`test_url_guard.py`、`test_vector_search.py`）。
- 把 10 处英文模块 docstring 统一改为中文（`shared/errorlog.py`、
  `shared/napcat.py`、`trace/common.py`、`trace/http_client.py`、`weibo/runtime.py`
  及 5 个测试模块）。现在 116 个模块的模块级 docstring 全部为中文且齐全。
- 校验方式：AST 剥离 docstring 后与改前逐字节比对，20 个文件
  `BODY_DRIFT 0`，纯注释改动零逻辑变化。

## v0.8.1 - 2026-10-31

本轮为「按 AstrBot 开发规范与官方接口文档的全量复审」，重点在可恢复性、
并发正确性与隐私边界。

### 修掉的真 bug

- **平台熔断把平台永久踢出轮询**（`sns/scheduler.plan_round`）：`platform_paused`
  的值是熔断**截止时间戳**，旧实现按真值判断，而落盘的时间戳永远非零——一次
  风控熔断之后该平台再也不会被调度，且重启后依旧。改为与当前时刻比较，并在
  `check_all` 里及时清理过期的熔断记录；`_next_sleep` 同步修掉「负延时导致 5 秒
  空转」。
- **最新的帖子被永久丢弃**（`sns/service`）：拉取结果升序排序后取 `[:limit]`
  拿到的是**最旧**的一批，而把真正最新的 `[limit:]` 直接标记成已读——一旦单轮
  超出上限，新内容就再也推不出来。改为取末尾并只对未入选的旧帖标记已读。
- **推送失败也标记已读**：`_deliver` 在「没有推送目标」或全部会话失败时返回
  `sent=0`，旧实现仍把所有帖子标记已读，消息永久丢失。现在只对确实投递成功的
  `post_id` 标记已读。
- **健康度/成功率被重复计入**（`sns/guard` + `sns/service`）：`guard` 已经追加过
  一条历史，`service` 又追加一条，60 条上限实际只覆盖 30 次检查。改为就地补充
  信号与详情，不再二次 append。
- **抖音/小红书风控误判**：抖音把整页 HTML 里的 `verify` 子串当作验证码（页面里
  的 `verifyStatus` 常量就足够触发），小红书把任意 `300031` 六位数字当作风控码。
  分别改为 `<title>` 关键词 + 验证页正文正则、以及 `error_code=300031` 结构匹配。
- **暂停的账号再也救不回来**：`paused=True` 的账号被 `due()` 与
  `plan_round(force=True)` 双重排除，而唯一解除暂停的分支是死代码。现在暂停账号
  在冷却窗口过去后会放行一次探活，成功即自动解除。
- **`订阅 RSS` 直接抛 TypeError**：`sns/commands._sns_add_feed` 少了一个 `await`。
- **归档首次下载必然崩**：磁盘余量保护对**尚未创建**的归档目录调
  `shutil.disk_usage`，首次归档即 `FileNotFoundError`。改为向上回溯到最近的已
  存在祖先取余量，量不出来则放行并告警一次（不把"未知"当成"写满"）。
- **对账在并发写入下漏帖**（`weibo/archive`）：OFFSET 分页遇到监控进程持续插入
  会跳过记录。新增 `ArchiveStore.iter_posts_keyset` 做键集分页 `(created_at, mid)`，
  无该方法时自动回落旧分页。
- **`posts_need_text` 口径错误**：沿用了 `TRIM(text)` 判据，纯链接/纯表情帖清洗后
  正文合法为空，会被永久算成「缺正文」并反复重试。改用 `cleaned_at` 判据。
- **归档长文接口恒被拒**：`weibo.com/ajax/statuses/longtext` 不在 `WEIBO_ALLOWED_HOSTS`
  里，永远走不到。补上 `weibo.com`（SSRF 校验仍在），并把「首选接口不可用」
  从 debug 提到 warning（只告警一次）。
- **签到账号页报错**：`sns/fetch_m1._fetch_instagram` 把 `profile_from_html()` 返回的
  dict 当 list 迭代。改为先取快照再 `parse_profile`。
- **`state.json` 丢更新**：`_save_state` 在写盘前就清掉 dirty 标记，写失败后本次
  变更再也不会被重试。改为写成功后才清标记，失败置回 dirty。
- **`归档导入 D:/a b.json` 被解析成命令名当路径**：带唤醒前缀时 `raw[len(token):]`
  取错位置。改为按命令词最后一次出现位置切分。
- **图床鉴权码进 URL**：`cfi_auth_code` 原本拼在 `?authCode=` 查询串里，会随 URL
  进访问日志、异常栈与面板。改为 `X-Auth-Code` 请求头；老版本只认查询参数时
  **被 401/403 拒一次后自动回落一次**并告警（不牺牲老站点可用性）。
- **事件循环被同步 IO 阻塞**：`count_posts` / `media_stats` / `find_by_phash` /
  `copylib_list` / 缩略图写入 / 归档导入元信息读取 / 溯源重扫全表扫描等一律移入
  `asyncio.to_thread`。
- **相册上传无超时**：`NapCatAlbum.call` 没有单次超时，最坏情况 3×180 秒死等并
  一直握着 `_group_lock`。新增 120 秒超时；非幂等调用超时**不重试**，并提示
  「可能其实已经传上去了」。
- **媒体下载未限速**：`_stream_to_file` 完全没走限速（违反 CONTRIBUTING「所有微博
  请求需经 `_pace`」）。新增独立的 `_media_pace`（0.2s 抖动）：媒体 CDN 与
  `m.weibo.cn` API 是两条风险线，套用 2–5 秒的 API 间隔会把一批图拖成几分钟。
- **媒体重定向不过 SSRF 校验**：改为手动跟随，每一跳都重新 `_validate_media_url`。
- **归档队列「满了就静默丢弃」**：改为捕获 `QueueFull` → 记入有界重投队列
  （`ARCHIVE_PENDING_RETRY_MAX`）→ worker 每轮 drain，并累计丢弃计数进状态。
- **面板后台任务不随插件停止**：`DashboardAPI.shutdown()` 早已存在却无人调用，
  重扫任务会活过 `terminate()`。已在 `weibo/plugin.terminate()` 中闭环。
- **资源泄露与并发竞争**：playwright 在所有通道失败时不关闭（每次重试漏一个
  node 进程）、`profile_root/_shared` 与按平台目录不一致、`_starting` 竞争会
  交出空 context、`check_all` 的 `locked()`+`acquire()` TOCTOU、RSS 取数无超时、
  `stagger_offsets` 是死代码（批量新增订阅同一秒全部触发）、
  `views_backend/sns_api` 自己 new 第二个 `SnsService` 写同一份状态文件。
- **面板接口以 200 掩盖失败**：SNS 接口失败时返回 HTTP 200，前端按 2xx 当成功。
  改为 409/500/503，并区分「服务未就绪」。
- **数据损坏导致整个域起不来**：`sns/state` 损坏 JSON 静默返回空（下一次保存就
  覆盖掉原文件），`sns/types.from_dict` 的数字转换无保护。改为备份
  `.corrupt-<epoch>` + `_safe_float`/`_safe_int` 归一化。
- **溯源缓存代际竞争**：`_reload_cache` 只在 SELECT 时持锁、却在锁外赋值，慢的
  旧快照能覆盖新结果。改为锁内赋值 + 代际计数，旧代丢弃。
- **临时下载非原子**：改为 `.part` + `flush`/`os.fsync` + `os.replace`，且
  `BaseException`（含 `CancelledError`）都会清理残留。
- **日志泄露带签名的直链**：全仓新增 `redact_url()`，7 处会写入完整图床直链的
  日志改为脱敏；`vector_search` 的错误 detail 只保留长度/头部/解析位置。

### 隐私与仓库卫生

- 移除全部真实账号信息：微博 UID、博主昵称、抖音 SEC_UID 与昵称、内网/真实图床
  地址、本地绝对路径。测试与 `_conf_schema.json` 提示、`CHANGELOG` 一律改用
  `1000000001` / `示例博主A` / `MS4wLjABAAAAdemoSecUidPlaceholder…` /
  `192.0.2.10`（TEST-NET-1）/ `bed.example.com` / `reference/weibo-copy`。
  `copy_golden.json` 90 条用例按 token 同步替换（`raw`/`long_text`/`expected` 三方
  一致，`clean(raw,long_text)==expected` 仍逐条成立，97 个 emoji 编码覆盖不变）。
- `scripts/verify_cleaner_golden.py` 不再硬编码本机路径，改读 `--reference-root` /
  `WEIBO_CLEANER_REFERENCE_ROOT`，未配置时明确跳过并 exit 0。
- `.gitignore` 重写：新增 `.env*`、`*.log`、`cookie*`/`credentials*`/`sessions*`、
  `sns_state.json`、`state.json`、`copylib/`（含真实文案原文）、`_selftest/`、
  `_tools/`、`.pytest-tmp/` 等；运行时数据与凭据一律不入库。
- 删除已入库的隐私目录与产物：`copylib/`（69 份真实文案导出）、`_scratch/`、
  `.screenshots/`、`.trae-html-share-packages/`、`_tools/`，以及 `dist/` 下
  v0.2.0–v0.7.0 旧构建包（其中仍含脱敏前的源码）。
- `metadata.yaml` 的 `astrbot_version` 下限由 `>=4.28.2` 下调为 `>=4.28.0`：
  `register_web_api(route, handler, methods, desc)`、`astrbot.api.web` 的
  `request`/`json_response`/`error_response`、`StarTools.get_data_dir` 在 4.28.0
  源码中均已存在且签名一致，没有理由把 4.28.0/4.28.1 用户挡在门外。

### 测试

全仓 **1097 项通过**（新增 6 项：鉴权码走请求头 / 老站点回落 / 5xx 不回落 /
熔断时间戳语义 / `posts_need_text` 改用 `cleaned_at` 判据）。

## v0.8.0 - 2026-10-31

- P8：多平台订阅推送上线——抖音、小红书、Instagram 博主有更新时实时推到 Q 群，
  复用微博域已验证的文案渲染、图片段、@全体与超时语义（`deliver_sns_payload`），
  但**不复用** weibo 的 pending 队列：订阅域自带去重与守卫，再套一层待推送队列
  会让「风控冷却中却还在重试推送」成为可能。
- 三通道取数，按平台可分别配置：`browser`（playwright 持久化会话，默认）/
  `http`（导出 Cookie + 纯 HTTP）/ `rss`（自建源）。browser 通道里 Cookie 从不
  离开浏览器、不落盘、不进日志，TLS 指纹与出口 IP 与真人一致。
- 保号优先：基础间隔 抖音/小红书 300s、IG 900s，抖动只向上；夜间（0–8 点）
  降速一倍；账号数线性拉长间隔；新账号 30 分钟热身、24 小时保号期；单轮最多
  3 条；只取首页。风控命中按信号加权指数冷却（验证码 1.8、静默空响应 1.2），
  健康度 <0.4 降速、<0.2 暂停，同平台 10 分钟 3 次风控则整平台熔断 30 分钟。
  `network_error` 不计入风控计数；空响应/结构不符一律按错误处理。
- 新增 `sns/` 域（constants / types / guard / scheduler / state / identity /
  browser / fetch_m1 / fetch_m2 / adapters{douyin,xhs,instagram,rss} / service /
  commands），`@filter` 仍只出现在 `main.py`；新增 `订阅` 指令组 11 条
  （帮助/抖音/小红书/IG/RSS/列表/删除/检测/登录/状态/演练），写操作限管理员。
- 面板新增第七页签「多平台订阅」：会话与登录态、账号增删与单条预览、守卫参数、
  延迟预估、凭据（仅脱敏）、风控事件流；后端新增 7 条接口
  （`sns/overview|credentials|session|accounts|guard|check-now|preview`），
  面板路由总数 28 → 35。
- `_conf_schema.json` 新增 `sns` 分组 27 项，与 `sns/constants.CFG_KEYS`
  逐项对齐；版本号同步 v0.8.0（metadata / constants / weibo.constants /
  build_release），发布清单纳入 `sns/`。
- 测试新增 141 项（守卫与调度、三个平台与 RSS 的解析、状态与身份、服务编排、
  指令解析、投递契约、七页签静态门禁、`订阅` 指令组入口契约、schema 与运行期
  键一致性），全仓 1095 项通过。过程中修掉的真 bug：
  `SnsState.get/remove` 复合键签名不符（首个 `订阅` 就崩）、`IdentityStore`
  缺 `profile_root`、抖音图集帖解析出 0 张图、抖音视频帖丢封面、
  小红书时间线多套一层导致判成"没有作品"、IG `challenge_required` 被当成
  正常空列表、Atom 源丢链接、指令参数错位一位（`订阅 删除 …` 被当成平台名）、
  `check_all` 空结果返回字符串导致指令层 AttributeError、手动检测永远查不到
  到点账号、`订阅 登录` 把 `BrowserUnavailable` 直接抛给用户、
  单群发送异常会中断整轮推送。

## v0.7.0 - 2026-10-31

- P6：统一 WebUI 上线，按 `views/dashboard/` 规范提供总览、微博监控、
  归档与图床、文案库、溯源与签到、错误日志六页签；hash 路由、深浅主题与
  `AstrBotPluginView`/旧 `AstrBotPluginPage` 双桥接兼容。
- 平移已验证的监控面板能力：账号、推送目标、相册绑定、关键词路由、@全体、
  推送预览、待推送/取消队列、运行统计与错误留存全部保留。
- 后端新增归档、管线、文案库、溯源四组 API：归档总览/列表/回填/导入/对账，
  管线状态/补跑/命名，文案库总览/检索/导入，溯源状态/重扫/历史键修复。
  原有 14 条面板 API 与新增 14 条接口共 28 条统一注册；面板不可用或注册异常
  只降级 WebUI，不阻断监控、归档和推送。
- 回填、重扫在后台任务中执行，进度可轮询；停止、补跑、清空等操作使用页内
  确认框，不依赖 iframe 中被禁用的原生 `confirm()`。所有操作写入最近动态。
- JSON/CSV/Markdown 上传支持 multipart 与 `content_base64` 两种桥接形态，
  限制扩展名与 64MB 体积；错误文本隐藏 URL、内网 IP 与签名查询串，
  代理和密钥不进入响应。
- 修复 WebUI 代理展示引用未定义 `_redact_proxy` 的潜在 NameError，统一使用
  `shared.common.redact_proxy`。
- 测试新增 10 项（路由清单、六页签资产、归档/回填/管线/文案/溯源适配、
  上传拒绝、脱敏），全仓 945 项通过；Ruff、JS 语法与发布清单校验通过。
- 按 AstrBot 插件规范重构入口：`WeiboSuitePlugin` 与全部 `@filter` 指令集中在
  `main.py`（加载器按 handler 的模块路径绑定实例，域模块里的装饰器在线上不会
  生效），删除 `composite.py`；新增入口契约与全局名解析门禁测试。
- 修复游客续期 `_jsonp_data`、媒体下载 `_header_int`、关键词相册
  `_keyword_rules_upsert`、文件名兜底 `hashlib` 四处 NameError；这些路径在真实
  运行中 100% 触发，旧测试因通配导入看不到未定义名。
- 配置 schema 与代码对齐：补全 monitor/push/album 分组与全部平铺镜像键，
  启动时把分组值镜像到业务代码读取的平铺键，运行期写入两边同步；指令/面板写入的
  监控账号、推送目标、相册与 @全体规则不再在重启后被 AstrBot 裁剪。
- 修复归档：暂停/失败游标可续跑；未知图数用 -1 哨兵，对账不再把带图帖误报缺图；
  增量归档补全长文；清洗判据改用 `cleaned_at`，纯链接帖不再被反复重洗。
- 修复管线：staging 每图独立子目录，避免自定义命名模板/同帖同名时缩略图或
  `copy2` 截断归档原图；缩略图上传失败不再覆盖原图。
- 修复溯源：OneBot 直发加 30s 回执超时；Qdrant `with_vector` 响应上限按向量规模
  放宽；分层下载超时按体积估算，未知长度统一落盘；远程哈希刷新失败进入退避；
  批量 embed 失败取消其余在飞任务；QQ `msg_seq` 自增；归档时间戳越界防护。
- 修复监控：已见 ID 入队即提交，重载/取消不再重复推送；队列满时保留剩余微博；
  风控冷却期间不再续期/重试/下载；续期失败不污染 UA 与 cookie 组合。
- 面板上传：base64 先按长度预检、multipart 先读 `size`，避免超大请求整块解进内存；
  写盘移入线程，并清理 6 小时前或超出 50 份的 `webui_uploads` 残留。
- 面板交互：命名规则有未保存改动时暂停自动重渲染；文案检索期间不覆盖结果列表。
- 管线补跑按 `state_rank` 分步幂等：已到 bed/thumb 的图不再重复上传图床。
- 归档/文案导入的读盘、JSON 解析与逐条写库移入线程，64MB 种子不再阻塞轮询。
- 对账报告新增 `scanned`/`partial`：超过 5000 帖扫描上限时不再误报全绿；
  修正格式化里图床/缩略图/向量字段名与报告键不一致的问题。

## v0.6.0 - 2026-10-31

- P5：新增文案清洗核心 `weibo/cleaner.py`，逐条移植 Node 版话题、链接、分享样板、
  行尾引导语、微博表情码与 VS16 规则；HTTP 正文语料经同一纯函数处理，
  U+200C/U+200D（尤其是 ZWJ emoji 连字）保持不动。
- `archive.db.posts.cleaned_text` 成为签到文案的唯一事实源：监控归档和回填落账时
  自动清洗，失败只记 warning，不影响推送或归档；旧库启动时就地补列，
  `/微博文案清洗` 可按账号、限量幂等重建。
- `/签到` weibo 模式通过 composite 注入的 `(指纹, 行列表)` 窄接口直接读归档库；
  只有已清洗且有正文的帖子进入签到池，首页图优先取有 URL 的媒体。
  归档不可用时继续回退原 `weibo/` 下的 CSV/MD，迁移期行为不中断。
- 新增 5 条 `微博` 子指令（共 30 条）：`文案库`、`文案列表`、`文案清洗`、
  `文案导出`、`文案导入`；CSV/MD 降级为交换格式，按 mid upsert，重复导入不增条。
- 纳入 `emoji-props.json`（与参考项目 SHA-256 相同）及 97 个微博表情映射，
  未知品牌/活动贴纸保留原码；同义词仅允许 Node 已声明的四组。
- 清洗回归新增 291 项：90 条 Node CSV 黄金基准逐字符比对，B1/B2/B3/B5/B6/B9
  文本门禁，C1/C2/C4/C5/C7/C9/C11 Emoji 门禁，长文补回、Markdown 行首、
  性能护栏及文案库导入/导出/签到快照测试。
- 本地参考语料 2114 条全量对拍：逐字符一致 2114、不一致 0；ZWJ 原始/成品均为 26。

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
- JSON 种子导入 `微博归档导入 <文件>`：兼容示例汇总两账号现成数据
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
