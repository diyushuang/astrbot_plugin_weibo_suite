# 管理面板

管理面板是这个插件的 WebUI 视图：入口在 AstrBot WebUI 的「插件」页里找到「微博全链路助手」，打开它的详情页，再进入页面上的「管理面板」。它把微博监控、推送队列、归档回填、图床管线、文案库、溯源与签到、错误留存和多平台订阅的日常操作集中在一个页面，不必每条操作都回聊天窗口敲指令。

面板由两部分组成：

- 前端是纯静态资源，位于 `views/dashboard/`：`index.html`、`app.js`、`settings.js`、`style.css`、`logo.svg` 与 `icon-*.svg`。没有打包步骤、没有 npm 依赖，原生 JavaScript 直接运行在 AstrBot 插件视图的 iframe 里。
- 后端是 `views_backend/` 里用 `register_web_api()` 注册的路由：`dashboard.py` 28 条（微博域）、`sns_api.py` 7 条（订阅域）、`settings_api.py` 2 条（设置页），合计 37 条。原 35 条路由路径与行为保持不变。

页面不直接请求这些路由，而是通过 AstrBot 的插件页面桥接对象（新版 `window.AstrBotPluginView`、旧版 `window.AstrBotPluginPage`）的 `apiGet()` / `apiPost()`，由宿主转发到 `/api/v1/plugins/extensions/<插件名>/<endpoint>`。所以页面文件不能在浏览器地址栏里直接打开，必须经 AstrBot WebUI 进入。

面板里所有破坏性操作都走页内确认框：AstrBot 插件页 iframe 没有开放 `allow-modals`，原生 `window.confirm()` 会被浏览器静默拦截并当成「取消」，插件因此自己实现了 `confirmDialog()`。操作结果统一显示在页面顶部状态条，出错时带非 2xx 的原因。

页面每 20 秒自动拉取一轮只读接口；标签页切到后台时暂停轮询，切回前台立即刷新一次。右上角「刷新」可以手动触发。

## 界面

导航栏有八个页签，页面加载时按卡片归属自动分页；设置页是唯一不按卡片归属、而是按 schema 目录动态渲染的页签。所有数据来自下列只读接口，写操作走同一组 POST 接口。

### 总览

- **运行状态**：轮询是否在跑、上次检查时间、下次检查时间、轮询间隔（最小 30 秒）、待推送条数与其中重试中的条数、风控冷却与等级、身份模式（自定义 Cookie 或游客身份）、出网代理（已打码）、ffmpeg 是否可用、群相册是否启用、后台任务数（下载视频、准备视频、相册上传）、服务器内存与磁盘余量。
- **运行统计**：累计推送成功/放弃、今日推送数、相册已上传/去重跳过/失败、近 7 天柱状图、博主推送排行 Top5。
- **最近动态**：推送、相册、风控、异常等事件的流水。
- **功能与指令说明**：折叠的速查表，列出功能特性与聊天指令。
- **操作**：右上「刷新」、顶部「立即检查」。立即检查会真的抓取一轮并按现有规则推送新微博；只有已经监控账号、没有风控冷却、当前也没有检查任务在跑时才会真正开始，否则接口返回 409 并在状态条说明原因。
- **后台任务**：「立即检查」在后台起一轮检查，接口立即返回；按钮置忙 3 秒后自动刷新，进度看运行状态里的 `last_check_ts` 和最近动态。

### 微博监控

- **监控博主**：按 uid 或主页链接添加；每行有独立的「@全体」开关、移除按钮，以及「待建基线」「失败 N 次」等徽标。
- **推送目标**：按 `平台:消息类型:会话号` 形式的 umo 增删会话。
- **相册绑定规则**：博主 + 群号 + 相册名（或相册 ID）；群号必须是纯数字。
- **关键词路由规则**：可设全局规则（uid 留空）或按博主规则；关键词不能包含冒号，博主规则优先于全局规则。
- **推送 @全体成员**：总开关、博主名单、关键词；卡片同时显示 QQ 的 24 小时 @次数配额与最近一次投递结果，避免「配了却没 @ 到」。
- **推送预览**：按当前配置渲染一条虚构样例，不发消息、不进队列。
- **待推送队列**：逐条取消（30 分钟内可在下方「已取消（可恢复）」里恢复），以及「停止本轮推送」。
- **危险操作**（均有二次确认）：移除监控博主、移除推送目标、移除相册绑定、移除关键词路由、取消单条推送、停止本轮推送。添加类操作与「@全体」开关没有确认框，按下即生效。
- **后台任务**：本页签的写接口都是同步完成的；批量抓取在总览的「立即检查」里。

### 归档与图床

- **归档总览**：帖子数、媒体计数、文案库计数，以及最近 8 条归档记录。
- **回填**：填 uid（或主页链接）、起始日期，可选「全量补详情」，点「开始回填」；「停止」只是请求停止，任务在下一个断点退出。
- **对账**：uid 可留空表示全部账号，运行后把 JSON 报告显示在下方。
- **导入 JSON 种子**：选择本地 `.json` 文件上传。
- **图床管线**：管线状态与队列文本、按上限（1–500，默认 200）补跑缺口、命名目录模板 / 文件名模板 / 博主分类 JSON 的保存。
- **危险操作**（均有二次确认）：开始回填、停止回填、运行归档对账、补跑管线缺口。
- **后台任务**：回填在后台运行，进度从归档总览的 `backfill.running` 与 `backfill.stats`（已翻页数、已取条数、已入库条数）读取，随 20 秒轮询更新。

### 文案库

- **清洗统计**：已清洗条数等计数。
- **检索**：按 uid 与正文关键词搜索，结果表格显示时间、博主、文案、状态。
- **导入**：上传 CSV 或 Markdown（`.csv` / `.md` / `.markdown`），可附带 uid 过滤。
- **危险操作**：无。导入按 mid upsert（幂等），重复导入不会产生重复记录。

### 溯源与签到

- **状态**：图库条目数、向量库是否启用与模型/集合/条目数、签到模式、重扫状态。
- **操作**：「启动图库重扫」（可勾选「强制重建扫描来源」）、「修复原图历史键」（只改本地键，不动图床）。
- **危险操作**（均有二次确认）：启动图库重扫、修复原图历史键。
- **后台任务**：重扫在后台运行，进度从 `trace/status` 的 `rescan.running` / `rescan.messages` / `rescan.error` 读取。

### 多平台订阅

- **登录态与取数方式**：每个平台的订阅数、基础间隔、登录态；「打开登录」会在本机弹出持久化浏览器窗口，是否真的登录成功以下一轮检查为准；「标记已登录」只记录一次登录时间，不保存 Cookie。取数方式可选 `browser` / `http` / `rss`。
- **订阅名单**：按平台 + 主页链接或账号 ID 添加；逐行「停用 / 启用」「移除」，两个按钮提交 `{action, platform, account_id}`，接口同时接受 `key`、`account_id` 与 `target` 三种写法；「预览推送文案」按当前模板渲染样例。
- **风控守卫**：冷却基数与上限、健康暂停 / 降速阈值、保号期、夜间（0–8 点）翻倍、激进档位，保存后即写入配置。
- **延迟估算**：各平台实际间隔与 P50 / P95 延迟估算。
- **Cookie 与凭据**：只显示脱敏摘要（是否已配置、字段数量、前缀与总长度），不显示 Cookie 原文；缺字段时提示「缺少 web_session」这类键名，浏览器 profile 目录只显示「已建」而回传布尔值。
- **订阅事件**：订阅域的事件流水。
- **危险操作**（均有二次确认）：取消订阅（移除）。「立即检查一轮」会同步等检查跑完再返回，页面在等待期间置忙。
- **后台任务**：本页签没有后台任务；「立即检查一轮」是同步调用，最长会等一整轮取数。

### 设置

- **页面归并**：`_conf_schema.json` 的 15 个可见分组按业务线归入 5 个主题页（监控与推送 / 归档与管线 / 图床与存储 / 溯源与签到 / 多平台订阅），第 6 页「关于与安全」只放版本、覆盖统计与安全边界，不含配置键。15 组不跨页、不重复，180 个键每个只出现一次。
- **每项一行**：中文短标签、一句说明、默认值、范围与单位、原始键名；危险项带红色标记。长分组把高级项收进「高级选项」折叠区，关键项默认展开。
- **控件类型**：bool 用开关；schema 的 `options` 用下拉；int / float 用带范围和单位的数字框；`list` 用「每行一项」的文本域（保存为字符串数组）；`object` / `dict` 用 JSON 编辑器（保存前校验必须是对象）；`secret` 用密码框，只显示「已配置 / 未配置」。
- **secret 约定**：响应里永远没有原值。输入框留空表示保持不变，输入新值表示覆盖，点「清除」才会写空串。批量保存时，未改动的 secret 不会进入请求体。
- **未保存保护**：改动只留在页面内存里，20 秒轮询不会覆盖；切页签、重新读取、放弃改动都会二次确认，`beforeunload` 也会拦截。底部保存条常驻显示未保存数量。
- **搜索**：跨全部 180 项匹配短标签、键名、说明、分组名与枚举标签；搜索结果按页面 / 分组重新组装卡片。
- **批量保存**：点「保存全部」一次性 POST 全部改动；服务端先全量校验类型、范围与枚举，再统一写入 schema 分组，最后调用 `save_config()` 落盘。任何一项失败都不会产生半截写入；落盘失败会把内存分组与平铺镜像一起回滚。
- **生效时机**：多数配置下一轮检查或下一次操作即读取；面板路由、ffmpeg 探测、已缓存的并发与浏览器目录等启动期初始化的项需要重载插件。
- **危险操作**（均有二次确认）：保存危险项（如关闭归档、切换存储模式、开启激进档位）、恢复整组默认值、放弃未保存改动、带着未保存改动离开设置页。
- **后台任务**：设置页没有后台任务；保存是同步的，通常在一秒内返回。

### 错误日志

- 列出留存的 warning / error 记录：时间、级别、来源（函数名与行号）、消息；带「上次运行」标记的是重启前读回的记录。
- 「清空」会同时清空内存缓冲与 `errors.log`，确认框里写明了这一点，属于危险操作。
- 日志在产生的瞬间落盘，512 KB 自动轮转，缓冲上限 2000 条。

## 接口

所有路由在注册时以插件名 `astrbot_plugin_weibo_suite`（`constants.py` 的 `PLUGIN_NAME`）作为前缀，下表省略该前缀，例如 `/overview` 的完整路径是 `/api/v1/plugins/extensions/astrbot_plugin_weibo_suite/overview`。在面板内调用时 endpoint 只写相对名（`overview`）。

入参一列均指 JSON 请求体字段，由各 handler 通过 `request.json()` 读取；GET 路由即使没有请求体也走同一套读取逻辑，因此表里标注了「无」的才是真正不接受参数。业务失败统一返回非 2xx（`error_response`，默认 400，另有 404 / 409 / 500 / 503）。

| 路由 | 方法 | 入参 | 返回要点 | 说明 |
| --- | --- | --- | --- | --- |
| `/overview` | GET | 无 | `version`；`status`（轮询、检查、风控、身份、代理、后台任务、内存、磁盘）；`accounts`；`at_all_settings`；`sessions`；`album_rules`；`keyword_rules`；`pending`；`cancelled`；`stats`；`activity` | 面板首屏与自动轮询的主数据源 |
| `/errors` | GET | 无 | `errors[]`，每条 `{ts, level, src, msg}` | 错误留存快照，`msg` 已在出口脱敏 |
| `/preview-push` | GET | 无 | `text`；`at_all`；`at_all_on`；`at_all_keywords`；`at_all_uids`；`with_images`；`with_videos` | 虚构样例渲染，不发消息、不进队列 |
| `/check-now` | POST | 无 | `{started: true}` | 后台起一轮检查；409：已有检查、无监控账号、风控冷却中、手动检查进行中 |
| `/cancel-pending` | POST | `{post_id}` | `{ok, message}` | 从待推送队列撤销一条，进入可恢复区；409 表示无法取消 |
| `/restore-pending` | POST | `{post_id}` | `{ok, message}` | 在撤销窗口内把条目放回队列；409 表示窗口已过 |
| `/stop-push-round` | POST | 无 | `{ok, message}` | 停止本轮推送，剩余条目留在队列；409 表示没有进行中的轮次 |
| `/clear-errors` | POST | 无 | `{cleared}` | 清空缓冲与 `errors.log`；404 表示错误留存未启用 |
| `/accounts` | POST | `{action, uid}` | add 返回 `{uid, name, nickname_ok, message?}`；remove 返回 `{removed, name}` | `action` 为 `add` / `remove`；uid 可传纯数字或主页链接 |
| `/at-all-uids` | POST | `{action, uid}` | `{uid, name, at_all}` | `@全体` 博主名单增删 |
| `/at-all-settings` | POST | `{action, value}` 或 `{action, keyword}` | `{at_all}` 或 `{keywords}` | `set_switch` 改总开关；`add_keyword` / `remove_keyword` 改关键词（每个 ≤30 字，最多 20 个） |
| `/sessions` | POST | `{action, umo}` | `{added}` 或 `{removed}` | 推送目标增删；umo 必须是 `平台:消息类型:会话号` 三段 |
| `/album-rules` | POST | `{action, uid, gid, album}` | `{uid, gid, album}` 或 `{removed}` | 博主 → 群相册绑定；群号必须是纯数字，uid 需已在监控列表 |
| `/keyword-rules` | POST | `{action, uid, keyword, gid, album}` | `{uid, keyword, gid, album}` 或 `{removed}` | uid 留空表示全局规则；关键词不能含冒号 |
| `/archive/overview` | GET | 无 | `enabled`；`posts`；`media`；`copylib`；`backfill{running, stats}`；`recent[]` | 归档总览；归档存储未就绪时返回 `enabled: false` 的空壳 |
| `/archive/list` | GET | JSON body：`{uid, limit, offset}` | `{uid, limit, offset, items[]}` | 分页查询归档；limit 1–50（默认 10），正文截到 280 字 |
| `/archive/backfill` | GET / POST | GET 空 body；POST `{}`、`{action: "stop"}` 或 `{uid, since, full}` | 查进度 `{running, stats}`；停止 `{stopped}`；启动 `{started, uid, since, full}` | 回填在后台任务里跑；409 表示已有回填任务 |
| `/archive/reconcile` | GET / POST | `{uid}`（留空表示全部） | 对账报告对象，字段由归档存储返回 | 只读校验，不修改数据；报告带 `error` 时返回 409 |
| `/archive/import` | POST | 上传体 `{filename, content_base64}` 或 multipart 文件 | 导入结果对象 | 只接受 `.json`；超过 64 MB 拒绝；临时文件落在数据目录的 `webui_uploads/` |
| `/pipeline/status` | GET | 无 | `{status, queue_worker}` | 图床管线统计与队列线程是否存活 |
| `/pipeline/retry` | POST | `{limit}` | 补跑结果对象（含 `queued`） | limit 1–500（默认 200）；409 表示管线不可用 |
| `/pipeline/naming` | GET / POST | GET 空 body；POST `{naming_directory, naming_file, categories}` | 三个字段回显（无配置时给默认模板） | `categories` 必须是 JSON 对象；有字段为空则 400，只读请求返回当前值 |
| `/copylib/overview` | GET | 无 | 文案库统计对象 | `ok: false` 时返回 409 |
| `/copylib/list` | GET / POST | `{uid, keyword, limit}` | `{uid, keyword, items[]}` | 关键词截到 60 字；limit 1–50（默认 10） |
| `/copylib/import` | POST | 上传体 + `{uid}` | 导入结果对象 | 只接受 `.csv` / `.md` / `.markdown`；导入失败返回 400 |
| `/trace/status` | GET | 无 | `library`；`vector{enabled, model, collection, count, error?}`；`checkin_mode`；`rescan{running, messages, error}` | 溯源域未初始化时返回 409 |
| `/trace/rescan` | POST | `{force}` | `{started, force}` | 图库重扫在后台跑；已在进行中返回 409，进度看 `/trace/status` |
| `/trace/repair-history` | POST | 无 | `{cleaned}` | 清理会话历史里带装饰前缀的原图键，只改本地键 |
| `/settings` | GET | 无 | `version`；`counts{pages,groups,keys,writable,secrets}`；`pages[]`（主题页/分组/字段目录）；`values{}`；`secrets{}`（仅 `configured`） | 设置页目录与当前值；secret 原值从不出现 |
| `/settings/save` | POST | `{values: {键: 值}}` | `{saved, ...快照}` | 只接受目录中登记的键；先全量校验再写入；未知键、类型/范围/枚举错误返回 400；落盘失败返回 500 并回滚 |
| `/sns/overview` | GET | 无 | `status: "ok"`，另有 `enabled`、`fetch_mode`、`aggressive`、`accounts`、`platforms`、`sessions`、`stats`、`last_round`、`browser`、`guard`、`latency`、`events`、`browser_error` | 订阅域总览；SNS 服务未就绪返回 503 |
| `/sns/credentials` | GET | 无 | `status`；`fetch_mode`；`rows[]`（`platform`、`label`、`logged_in`、`last_login_ts`、`note`、`cookie_configured`、`cookie_keys`、`cookie_preview`、`missing_keys`、`profile_ready`）；`modes[]` | 只返回脱敏摘要，Cookie 原文不会出现在任何响应里 |
| `/sns/session` | POST | `{action, platform, logged_in, note}` | open 返回 `{status, message}`；mark 返回 `{status, platform, logged_in}` | `open` 只打开登录窗口，登录结果以下一轮检查为准；打开失败返回 409 |
| `/sns/accounts` | POST | `{action, platform, target, key, account_id}` | add / remove 返回 `{status, message}`；toggle 返回 `{status, key, enabled}` | add 用 `target`；remove 与 toggle 接受 `key`（`平台:账号`）、`account_id` 或 `target`，面板提交的是 `{platform, account_id}` |
| `/sns/guard` | GET / POST | GET 空 body；POST 白名单键 | GET 返回 `{status, guard, latency}`；POST 返回 `{status, changed[]}` | 可写键：`enabled`、`fetch_mode`、`aggressive`、`night_slowdown`、`browser_headed`、`cooldown_*`、`health_*`、`base_interval_*`、`message_*`、`max_images`、`text_max_length`、`at_all` 等；无有效字段返回 400，`fetch_mode` 只允许 `browser` / `http` / `rss` |
| `/sns/check-now` | POST | `{platform}`（留空表示全部到点） | `{status: "ok", ...result}` | 同步等一轮检查跑完，不是后台任务 |
| `/sns/preview` | POST | `{platform}`（默认 `douyin`） | `{status, text, sessions, at_all}` | 按当前模板渲染样例；模板占位符非法时返回 2xx 且 `status: "error"` |

## 鉴权与安全边界

这些接口复用 AstrBot 自身的会话与权限体系：路由通过 `register_web_api()` 挂在插件扩展命名空间下，能否访问由 AstrBot WebUI 的登录会话决定。插件侧**没有**额外的 token、API Key 或 CSRF 校验，也没有按请求来源做二次鉴权。由此有两点必须注意：

- 不要把面板端点直接暴露到公网。如果 AstrBot 需要公网访问，请在反向代理或网关上再加一层鉴权，或者只允许本机 / 内网访问 WebUI 端口。
- 面板的写接口等同于聊天指令里的管理操作，能改监控名单、推送目标、相册绑定、文案库与订阅配置；拿到一个已登录会话就等于拿到全部管理能力。
- `/settings/save` 只接受设置目录里登记的 180 个分组键，未知键一律 400；它不是通用的配置写入口，也不能写入旧布局的顶层镜像键。secret 字段的读取只回 `configured` 布尔，原值不会进入任何响应。

其余边界：

- 业务失败返回非 2xx。`error_response()` 默认 400，参数缺失/非法为 400，找不到对象为 404，状态冲突（已有任务、风控冷却、未初始化）为 409，读取异常为 500，SNS 服务未就绪为 503。唯一例外是 `/sns/preview` 的模板占位符错误：它返回 2xx 并在 JSON 里给 `status: "error"`。
- 凭据类接口只返回脱敏值。`/sns/credentials` 的 `cookie_preview` 由 `redact_cookie()` 生成，形如 `abc123…（共 120 字符）`，只保留前 6 个字符和总长度；`cookie_keys` 只是字段数量；`missing_keys` 只回缺失的键名（用于提示还差哪个字段），不回任何取值；浏览器 profile 目录只回 `profile_ready` 布尔值，不回本机绝对路径。Cookie 原文不会进入任何响应。
- 日志类接口返回的是脱敏后的记录。`/errors` 的每条 `msg` 在 `shared/errorlog.py` 的 `snapshot()` 出口经过 `_redact_error_msg()`：URL 与裸 IP 变成 `<地址已隐藏>`，`?token=` / `?sign=` 这类查询串凭据与 `user:pass@host` 代理账号变成 `<凭据已隐藏>`，裸 `/file/...` 路径同样变成 `<地址已隐藏>`。图床直链相关的日志另由 `trace/image_bed.py` 的 `redact_url()` 处理：只保留 `scheme://host/path`，查询串（`?sign=`、`?token=` 等一次性凭据）一律收成 `?***`。
- `/overview` 里的出网代理地址经 `redact_proxy()` 打码后才下发，不返回代理账号与密码。
- 上传接口有后缀白名单（归档只收 `.json`，文案只收 `.csv` / `.md` / `.markdown`）、64 MB 大小上限，文件只写到插件数据目录下的 `webui_uploads/`，6 小时后由下一次上传顺带清理，最多保留最近 50 个；数据目录未就绪时直接拒绝，不落到 AstrBot 安装目录。
- 上传是**流式分块**写入而不是整个读进内存：multipart 走的 `_store_stream()`（`views_backend/dashboard.py:938`）按 1 MB 一段写到临时 `.part` 文件，累计超过 64 MB 立即中断、返回 413 并删掉半成品；只有确认没超限才 `os.replace()` 成正式文件名。所以「选了一个 2 GB 的文件」不会先把进程撑爆再报错。
- 错误响应不带服务端绝对路径：所有接口的异常文本都过 `_safe_error()`（`views_backend/dashboard.py:823-826`），其中的绝对路径被替换成 `<路径已隐藏>`，URL 与裸 IP 同样遮蔽。接口只回「为什么失败」，不回目录结构，截图贴群里也不会泄露部署路径。
- 面板启动的后台任务（回填、图库重扫、手动检查）都会登记在插件的任务集合里，插件卸载时统一取消。

## 常见问题

### 面板打不开，或页签是空白的

- 确认 AstrBot 版本不低于 4.28.0，且能导入 `astrbot.api.web`。导入失败时插件会退回 quart 实现；两者都不可用时 `register_dashboard()` 返回 `False`，日志里会有一条「统一 WebUI 注册失败（面板降级）」的 warning，此时监控、归档、推送仍然照常工作，只是没有面板。
- 页面文件不能直接用浏览器打开，请在 AstrBot WebUI 的「插件 → 微博全链路助手 → 详情页」里进入。桥接对象不存在时页面顶部会提示「未检测到 AstrBot 桥接环境」。
- 全部页签都空：先看浏览器控制台，多半是请求 404（插件名前缀不符或插件没加载）或 500。
- 只有「多平台订阅」页签空：`sns_api.py` 的 7 条路由是单独注册的，注册失败只影响订阅域，`/sns/overview` 会返回 503 或 404，重载插件即可重试。
- 只有「错误日志」页签空且提示未启用：错误留存器随插件初始化挂载，初始化没跑完时为 404。

### 改了配置没生效

- 面板的写接口会直接改插件配置并落盘，与聊天指令写的是同一份配置；多数参数在下一轮检查时读取。
- 少数在插件启动时初始化一次的项需要重载插件，例如轮询任务、面板路由本身、ffmpeg 探测结果、已缓存的下载/上传并发。
- 页面每 20 秒才自动刷新一次；操作后可以点右上角「刷新」，不必等轮询。
- 如果只是「推送预览」没变，点卡片上的「刷新预览」再确认一次。

### 后台任务卡住了怎么看

- 归档回填：看 `/archive/overview` 的 `backfill.running` 与 `backfill.stats`（`pages` / `fetched` / `inserted` 是否还在涨）。
- 图库重扫：看 `/trace/status` 的 `rescan.running`、`rescan.messages`、`rescan.error`。
- 手动检查：看 `/overview` 的 `status.checking`、`status.last_check_ts` 与最近动态。
- 图床管线补跑：看 `/pipeline/status` 的 `status` 与 `queue_worker`，入队数量在补跑接口的返回里。
- 长时间 `running: true` 且计数不动：回填可以先在面板点「停止」（停止只是置信号，任务在下一个断点退出）；其他任务没有停止入口，只能重载插件。卡住时的异常通常已经落在「错误日志」页签里，可以直接对照。

### 凭据显示为掩码

- 这是设计如此：`/sns/credentials` 只返回 `cookie_preview`（前 6 个字符 + 总长度）与字段数量，用来判断「配没配、配了多长」，不提供还原。
- 掩码为空且 `cookie_configured` 为 `false`，说明该平台还没有配置 Cookie。
- 要更新凭据，请在插件配置面板重新填写，或用 `browser` 取数方式在本机浏览器登录一次；面板不会回显，也不会回写 Cookie。
- 列表整块为空：先确认插件已初始化（`/sns/credentials` 返回 503 表示 SNS 服务未就绪）。前端读 `rows`，接口返回的就是 `rows`；v0.8.1 之前前端读的是 `credentials`，那个字段名不一致的问题已修复。

### 多平台订阅里点「停用」或「移除」提示 404 / 400

- 这两个按钮提交 `{action, platform, account_id}`；接口现在同时接受 `key`（`平台:账号` 全量键）、`account_id` 与 `target`，正常情况下不会再失败。
- 若仍返回 404「没有这个订阅」：说明列表是旧的，点右上角「刷新」后重试（订阅可能已在别处被移除）。
- 若返回 400「缺少平台或账号」：请求里既没有 `key` 也没有 `platform`，属于直接调用接口时漏填。

## 相关文档

- [指令说明](./COMMANDS.md)
- [配置项说明](./CONFIGURATION.md)
- [贡献指南](../CONTRIBUTING.md)
