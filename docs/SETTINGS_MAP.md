# 设置页键映射

由 `scripts/gen_settings_map.py` 从 `_conf_schema.json` 与
`views_backend/settings_schema.py` 生成，请勿手改。

- 主题页：5 个（另有 1 个「关于与安全」页，不含配置键）
- schema 可见分组：15 个
- 配置键：180 个；可写：180 个
- 脱敏字段：16 个

| 页面 | 分组 | 配置键 | 短标签 | 控件 | 可写 | 默认值 | 范围 / 选项 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 监控与推送 | 微博监控 | `monitor.poll_interval` | 轮询间隔 | 数字 | 是 | 120 | 30–3600 秒 |  |
| 监控与推送 | 微博监控 | `monitor.monitored_uids` | 监控 UID 列表 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 微博监控 | `monitor.include_retweets` | 包含转发帖 | 开关 | 是 | true |  |  |
| 监控与推送 | 微博监控 | `monitor.filter_keywords` | 屏蔽关键词 | 列表（每行一项） | 是 | [] | 每行一项 |  |
| 监控与推送 | 微博监控 | `monitor.whitelist_keywords` | 白名单关键词 | 列表（每行一项） | 是 | [] | 每行一项 |  |
| 监控与推送 | 微博监控 | `monitor.max_post_age_minutes` | 最大帖子时效 | 数字 | 是 | 0 | 0–10080 分钟 |  |
| 监控与推送 | 微博监控 | `monitor.proxy` | 出网代理 | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 监控与推送 | 微博监控 | `monitor.custom_cookie` | 自定义 Cookie | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 监控与推送 | 推送与 @全体 | `push.at_all` | @全体总开关 | 开关 | 是 | false |  | 危险 |
| 监控与推送 | 推送与 @全体 | `push.message_format` | 推送模板 | 多行文本 | 是 | 📢 微博更新\n【{name}】{time}\n{weibo}\n🔗 {link} |  |  |
| 监控与推送 | 推送与 @全体 | `push.push_sessions` | 推送目标会话 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 推送与 @全体 | `push.push_delay_seconds` | 推送条目间隔 | 数字 | 是 | 2 | 0–60 秒 |  |
| 监控与推送 | 推送与 @全体 | `push.max_push_per_round` | 单轮最多推送 | 数字 | 是 | 0 | 0–200 条 |  |
| 监控与推送 | 推送与 @全体 | `push.max_pending_retries` | 最大重试次数 | 数字 | 是 | 5 | 0–20 次 |  |
| 监控与推送 | 推送与 @全体 | `push.message_with_images` | 推送附带图片 | 开关 | 是 | true |  |  |
| 监控与推送 | 推送与 @全体 | `push.message_with_videos` | 推送附带视频 | 开关 | 是 | true |  |  |
| 监控与推送 | 推送与 @全体 | `push.max_videos_per_post` | 单帖最多视频 | 数字 | 是 | 2 | 0–10 个 |  |
| 监控与推送 | 推送与 @全体 | `push.message_send_timeout` | 单次发送超时 | 数字 | 是 | 60 | 5–300 秒 |  |
| 监控与推送 | 推送与 @全体 | `push.text_max_length` | 正文截断长度 | 数字 | 是 | 100 | 1–2000 字符 |  |
| 监控与推送 | 推送与 @全体 | `push.show_full_weibo_text` | 长文推送全文 | 开关 | 是 | false |  |  |
| 监控与推送 | 推送与 @全体 | `push.show_hashtags` | 保留话题标签 | 开关 | 是 | false |  |  |
| 监控与推送 | 推送与 @全体 | `push.weibo_emoji_translate` | 微博表情转 emoji | 开关 | 是 | false |  |  |
| 监控与推送 | 推送与 @全体 | `push.at_all_uids` | @全体博主名单 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 推送与 @全体 | `push.at_all_keywords` | @全体关键词 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 推送与 @全体 | `push.at_all_daily_limit` | 24 小时 @全体上限 | 数字 | 是 | 10 | 0–50 次 | 高级 |
| 监控与推送 | 推送与 @全体 | `push.at_all_check_permission` | @全体发送前检查权限 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.at_all_verify` | @全体发送后回读确认 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.callback_api_base` | 回调 API 基址 | 单行文本 | 是 |  |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_prefer_file` | 视频优先按文件发送 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_quality` | 视频清晰度 | 单行文本 | 是 | hd |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_max_mb` | 单个视频上限 | 数字 | 是 | 95 | 1–500 MB | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_inline_max_mb` | 内联视频上限 | 数字 | 是 | 30 | 1–200 MB | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_file_fallback` | 视频失败回退文件 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 推送与 @全体 | `push.video_file_temp` | 视频转文件后清理临时文件 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 群相册 | `album.album_enabled` | 启用群相册 | 开关 | 是 | true |  |  |
| 监控与推送 | 群相册 | `album.album_rules` | 相册绑定规则 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 群相册 | `album.album_keyword_rules` | 关键词相册路由 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 |
| 监控与推送 | 群相册 | `album.album_max_images` | 单帖最多上传图片 | 数字 | 是 | 18 | 1–18 张 |  |
| 监控与推送 | 群相册 | `album.album_live_gif` | 实况图转 GIF | 开关 | 是 | true |  |  |
| 监控与推送 | 群相册 | `album.album_same_host` | 相册文件走协议端 host | 开关 | 是 | false |  | 高级 |
| 监控与推送 | 群相册 | `album.album_notify` | 上传完成群内提示 | 开关 | 是 | false |  | 高级 |
| 监控与推送 | 群相册 | `album.album_download_concurrency` | 图片下载并发 | 数字 | 是 | 5 | 1–20 并发 | 高级 |
| 监控与推送 | 群相册 | `album.album_upload_concurrency` | 相册上传并发 | 数字 | 是 | 3 | 1–10 并发 | 高级 |
| 监控与推送 | 群相册 | `album.album_upload_interval` | 相册上传间隔 | 数字 | 是 | 0.5 | 0–10 秒 | 高级 |
| 监控与推送 | 群相册 | `album.album_upload_payload_mb` | 单次上传载荷预算 | 数字 | 是 | 32.0 | 1–100 MB | 高级 |
| 监控与推送 | 图片投递通道 | `delivery.napcat_mode` | NapCat 通道 | 下拉 | 是 | auto | auto=自动（推荐） / native=原生直传 / bytes=本机处理后上传 / url=直链原图 / url-scaled=直链缩放版 |  |
| 监控与推送 | 图片投递通道 | `delivery.napcat_max_side` | 缩放最长边 | 数字 | 是 | 1920 | 1–4096 像素 |  |
| 监控与推送 | 图片投递通道 | `delivery.napcat_quality` | JPEG 压缩质量 | 数字 | 是 | 85 | 1–100 % |  |
| 监控与推送 | 图片投递通道 | `delivery.napcat_verify_scaled` | 实测校验缩放 | 开关 | 是 | true |  | 高级 |
| 监控与推送 | 图片投递通道 | `delivery.napcat_scaled_url_style` | 缩放 URL 风格 | 下拉 | 是 | query | query=查询参数式 / cf-path=Cloudflare 路径式 | 高级 · NapCat 通道 = 直链缩放版 |
| 监控与推送 | 图片投递通道 | `delivery.napcat_target_kb` | 压缩目标体积 | 数字 | 是 | 0 | 0–10000 KB | 高级 |
| 监控与推送 | 图片投递通道 | `delivery.napcat_webp` | 压缩输出 WebP | 开关 | 是 | false |  | 高级 |
| 监控与推送 | 图片投递通道 | `delivery.napcat_timeout` | 图片段超时 | 数字 | 是 | 60 | 10–300 秒 | 高级 |
| 监控与推送 | 图片投递通道 | `delivery.qq_mode` | QQ 官方通道 | 下拉 | 是 | auto | auto=自动（推荐） / native=原生直传 / bytes=本机处理后上传 / url=直链原图 / url-scaled=直链缩放版 |  |
| 监控与推送 | 图片投递通道 | `delivery.qq_native_max_mb` | native 体积上限 | 数字 | 是 | 3 | 0–5 MB |  |
| 监控与推送 | 图片投递通道 | `delivery.qq_auto_prefer` | auto 通道偏好 | 下拉 | 是 | url | url=直链优先 / bytes=本地分片优先 |  |
| 监控与推送 | 图片投递通道 | `delivery.qq_url_max_mb` | 直链体积上限 | 数字 | 是 | 20 | 0–100 MB |  |
| 监控与推送 | 图片投递通道 | `delivery.qq_max_upload_mb` | 分片下载上限 | 数字 | 是 | 100 | 1–500 MB |  |
| 监控与推送 | 图片投递通道 | `delivery.qq_on_failure` | 通道失败处理 | 下拉 | 是 | fallback | fallback=回退原生通道 / notify=只提示文字 |  |
| 归档与管线 | 归档账本 | `archive.enabled` | 启用增量归档 | 开关 | 是 | true |  | 危险 |
| 归档与管线 | 归档账本 | `archive.download_originals` | 异步下载原图 | 开关 | 是 | true |  |  |
| 归档与管线 | 归档账本 | `archive.auto_archive` | 监控命中后自动归档 | 开关 | 是 | true |  |  |
| 归档与管线 | 归档账本 | `archive.page_delay` | 回填页间间隔 | 数字 | 是 | 3.0 | 0–60 秒 | 高级 |
| 归档与管线 | 归档账本 | `archive.since` | 回填默认起始日期 | 单行文本 | 是 |  |  |  |
| 归档与管线 | 归档账本 | `archive.download_concurrency` | 原图下载并发 | 数字 | 是 | 1 | 1–10 并发 | 高级 |
| 归档与管线 | 图床与向量管线 | `pipeline.enabled` | 启用图床管线 | 开关 | 是 | false |  | 危险 |
| 归档与管线 | 图床与向量管线 | `pipeline.naming_directory` | 目录模板 | 单行文本 | 是 | {category}/【微博图集@{account}】/ |  |  |
| 归档与管线 | 图床与向量管线 | `pipeline.naming_file` | 文件名模板 | 单行文本 | 是 | 【微博@{account}】{date}-{index}：{title}.jpg |  |  |
| 归档与管线 | 图床与向量管线 | `pipeline.categories` | 博主分类映射 | JSON 编辑器 | 是 | {} | JSON 对象 |  |
| 归档与管线 | 图床与向量管线 | `pipeline.enabled_uids` | 只跑这些博主 | 列表（每行一项） | 是 | [] | 每行一项 |  |
| 归档与管线 | 图床与向量管线 | `pipeline.auto_queue` | 归档后自动入队 | 开关 | 是 | true |  |  |
| 归档与管线 | 图床与向量管线 | `pipeline.max_attempts` | 单张最大尝试 | 数字 | 是 | 3 | 1–10 次 |  |
| 归档与管线 | 图床与向量管线 | `pipeline.thumb_size` | 缩略图长边 | 数字 | 是 | 720 | 64–2048 像素 |  |
| 归档与管线 | 图床与向量管线 | `pipeline.thumb_quality` | 缩略图质量 | 数字 | 是 | 72 | 1–100 % |  |
| 归档与管线 | 文案库 | `copylib.auto_clean` | 归档后自动清洗 | 开关 | 是 | true |  |  |
| 图床与存储 | 图床上传 | `image_bed.mode` | 图床模式 | 下拉 | 是 | local | local=仅本地副本 / generic_http=通用 HTTP 图床 / cloudflare_imgbed=CloudFlare-ImgBed | 危险 |
| 图床与存储 | 图床上传 | `image_bed.api_url` | 上传接口地址 | 单行文本 | 是 |  |  | 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.token` | 图床鉴权 Token | 密码（脱敏） | 是 | （空） |  | 脱敏 · 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.auth_header` | 鉴权请求头名称 | 密码（脱敏） | 是 | （空） |  | 脱敏 · 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.auth_prefix` | 鉴权头值前缀 | 单行文本 | 是 | Bearer  |  | 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.file_field` | 上传文件字段名 | 单行文本 | 是 | file |  | 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.extra_fields` | 附加表单字段 | JSON 编辑器 | 是 | {} | JSON 对象 | 明文 JSON · 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.url_path` | 响应直链路径 | 单行文本 | 是 | data.url |  | 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.timeout` | 上传超时 | 数字 | 是 | 30 | 5–300 秒 | 高级 · 图床模式 = 通用 HTTP 图床 |
| 图床与存储 | 图床上传 | `image_bed.cfi_base_url` | ImgBed 站点地址 | 单行文本 | 是 |  |  | 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 图床上传 | `image_bed.cfi_token` | ImgBed API Token | 密码（脱敏） | 是 | （空） |  | 脱敏 · 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 图床上传 | `image_bed.cfi_auth_code` | 上传鉴权码 authCode | 密码（脱敏） | 是 | （空） |  | 脱敏 · 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 图床上传 | `image_bed.cfi_upload_channel` | 存储渠道 uploadChannel | 单行文本 | 是 |  |  | 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 图床上传 | `image_bed.cfi_channel_name` | 渠道名称 channelName | 单行文本 | 是 |  |  | 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 图床上传 | `image_bed.cfi_upload_folder` | 上传目录 uploadFolder | 单行文本 | 是 |  |  | 图床模式 = CloudFlare-ImgBed |
| 图床与存储 | 对象存储 | `storage_bucket.mode` | 储存桶模式 | 下拉 | 是 | none | none=不使用储存桶 / cloudflare_r2=Cloudflare R2 / oracle_oci=Oracle OCI | 危险 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_account_id` | R2 账户 ID | 单行文本 | 是 |  |  | 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_access_key_id` | R2 Access Key ID | 密码（脱敏） | 是 | （空） |  | 脱敏 · 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_secret_access_key` | R2 Secret Access Key | 密码（脱敏） | 是 | （空） |  | 脱敏 · 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_bucket` | R2 存储桶名称 | 单行文本 | 是 |  |  | 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_endpoint` | R2 S3 endpoint | 单行文本 | 是 |  |  | 高级 · 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.r2_public_base_url` | R2 公开访问域名 | 单行文本 | 是 |  |  | 储存桶模式 = Cloudflare R2 |
| 图床与存储 | 对象存储 | `storage_bucket.oci_namespace` | OCI 命名空间 | 单行文本 | 是 |  |  | 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_region` | OCI 区域标识 | 单行文本 | 是 |  |  | 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_access_key_id` | OCI Access Key | 密码（脱敏） | 是 | （空） |  | 脱敏 · 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_secret_access_key` | OCI Secret Key | 密码（脱敏） | 是 | （空） |  | 脱敏 · 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_bucket` | OCI 存储桶名称 | 单行文本 | 是 |  |  | 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_endpoint` | OCI S3 endpoint | 单行文本 | 是 |  |  | 高级 · 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_public_bucket` | 存储桶公共可读 | 开关 | 是 | false |  | 储存桶模式 = Oracle OCI |
| 图床与存储 | 对象存储 | `storage_bucket.oci_public_base_url` | 自定义公开地址 | 单行文本 | 是 |  |  | 储存桶模式 = Oracle OCI |
| 溯源与签到 | 溯源引擎 | `basic.search_engine` | 检索引擎 | 下拉 | 是 | auto | auto=自动（向量优先，失败回退哈希） / vector=仅向量库 / hash=仅本地哈希 |  |
| 溯源与签到 | 溯源引擎 | `basic.scan_dirs` | 本地图库目录 | 列表（每行一项） | 是 | [] | 每行一项 |  |
| 溯源与签到 | 溯源引擎 | `basic.hash_similarity_threshold` | 哈希命中阈值 | 数字 | 是 | 0.85 | 0–1 比例 |  |
| 溯源与签到 | 溯源引擎 | `basic.ai_verify` | AI 视觉复核 | 开关 | 是 | false |  | 高级 |
| 溯源与签到 | 溯源引擎 | `basic.register_admin_only` | 仅管理员可登记原图 | 开关 | 是 | true |  | 危险 · 高级 |
| 溯源与签到 | 溯源引擎 | `basic.max_images_per_query` | 单次溯源最多图片 | 数字 | 是 | 3 | 1–10 张 | 高级 |
| 溯源与签到 | 溯源引擎 | `basic.hash_size` | 感知哈希精度 | 下拉 | 是 | 16 | 4=4 × 4（16 bit） / 8=8 × 8（64 bit） / 12=12 × 12（144 bit） / 16=16 × 16（256 bit，默认） / 20=20 × 20（400 bit） / 24=24 × 24（576 bit） / 28=28 × 28（784 bit） / 32=32 × 32（1024 bit） | 高级 |
| 溯源与签到 | 溯源引擎 | `basic.top_n` | 未命中候选数 | 数字 | 是 | 3 | 0–50 张 | 高级 |
| 溯源与签到 | 向量检索 | `vector_search.qdrant_url` | Qdrant 服务地址 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 向量检索 | `vector_search.qdrant_api_key` | Qdrant API Key | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 溯源与签到 | 向量检索 | `vector_search.collection_name` | Qdrant 集合名 | 单行文本 | 是 | imgbed_images |  |  |
| 溯源与签到 | 向量检索 | `vector_search.embed_base_url` | 向量 AI 地址 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 向量检索 | `vector_search.embed_api_key` | 向量 AI API Key | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 溯源与签到 | 向量检索 | `vector_search.embed_model` | 多模态向量模型 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 向量检索 | `vector_search.similarity_threshold` | 向量命中阈值 | 数字 | 是 | 0.8 | 0–1 比例 |  |
| 溯源与签到 | 向量检索 | `vector_search.duplicate_vector_threshold` | 重复图片阈值 | 数字 | 是 | 0.995 | 0–1 比例 | 高级 |
| 溯源与签到 | 向量检索 | `vector_search.vector_index_on_register` | 登记时写入向量库 | 开关 | 是 | true |  |  |
| 溯源与签到 | 向量检索 | `vector_search.embed_image_input` | 图片输入格式 | 下拉 | 是 | nemotron-vl | nemotron-vl=NVIDIA Nemotron VL / qwen-vl=Qwen VL / dataurl=Data URL / jina-image=Jina Image | 高级 |
| 溯源与签到 | 向量检索 | `vector_search.embed_input_type` | NVIDIA input_type | 下拉 | 是 | passage | passage=passage（图片侧） / query=query（文本侧） | 高级 · 图片输入格式 = NVIDIA Nemotron VL |
| 溯源与签到 | 向量检索 | `vector_search.top_k` | 检索候选数 | 数字 | 是 | 5 | 1–50 个 | 高级 |
| 溯源与签到 | 向量检索 | `vector_search.request_timeout` | 请求超时 | 数字 | 是 | 30 | 5–300 秒 | 高级 |
| 溯源与签到 | 限制与缓存 | `limits.max_download_mb` | 单图下载上限 | 数字 | 是 | 20 | 1–500 MB | 高级 |
| 溯源与签到 | 限制与缓存 | `limits.remote_hash_enabled` | 图床哈希索引 | 开关 | 是 | true |  | 高级 |
| 溯源与签到 | 限制与缓存 | `limits.remote_hash_ttl` | 哈希缓存有效期 | 数字 | 是 | 21600 | 60–604800 秒 | 高级 |
| 溯源与签到 | 限制与缓存 | `limits.remote_hash_max_points` | 哈希拉取上限 | 数字 | 是 | 0 | 0–1000000 点 | 高级 |
| 溯源与签到 | 随机图 | `random_media.base_url` | 图床站点地址 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 随机图 | `random_media.lan_base_url` | 内网直连地址 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 随机图 | `random_media.api_endpoint` | 随机图接口路径 | 单行文本 | 是 | /random |  |  |
| 溯源与签到 | 随机图 | `random_media.api_token` | 随机图接口 Token | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 溯源与签到 | 随机图 | `random_media.default_dir` | 默认目录 | 单行文本 | 是 |  |  |  |
| 溯源与签到 | 随机图 | `random_media.timeout` | 请求超时 | 数字 | 是 | 10 | 1–60 秒 |  |
| 溯源与签到 | 随机图 | `random_media.retry_count` | 失败重试次数 | 数字 | 是 | 3 | 0–10 次 |  |
| 溯源与签到 | 随机图 | `random_media.show_file_info` | 回传附带文件名 | 开关 | 是 | true |  |  |
| 溯源与签到 | 随机图 | `random_media.enable_llm` | 允许 LLM 工具调用 | 开关 | 是 | true |  |  |
| 溯源与签到 | 随机图 | `random_media.send_thumbnail` | 发送缩略图 | 开关 | 是 | true |  |  |
| 溯源与签到 | 每日签到 | `checkin.enabled` | 启用每日签到 | 开关 | 是 | true |  |  |
| 溯源与签到 | 每日签到 | `checkin.mode` | 签到内容模式 | 下拉 | 是 | quote | quote=随机语句 / weibo=微博归档文案 |  |
| 溯源与签到 | 每日签到 | `checkin.render_mode` | 图文呈现方式 | 下拉 | 是 | card | card=合成卡片 / simple=缩略图 + 文字 |  |
| 溯源与签到 | 每日签到 | `checkin.border_px` | 卡片边框宽度 | 数字 | 是 | 0 | 0–200 像素 |  |
| 溯源与签到 | 每日签到 | `checkin.utc_offset_hours` | 签到时区 | 数字 | 是 | 8 | -12–14 小时 |  |
| 溯源与签到 | 每日签到 | `checkin.quote_source` | 语句来源 | 下拉 | 是 | builtin | builtin=仅本地语句库 / api=仅在线接口 / mixed=本地 + 在线混合 |  |
| 溯源与签到 | 每日签到 | `checkin.quote_local_text` | 本地语句 | 多行文本 | 是 |  |  |  |
| 溯源与签到 | 每日签到 | `checkin.quote_api_url` | 语句接口地址 | 单行文本 | 是 | https://v1.hitokoto.cn/?encode=json&c=d&c=i&c=k |  |  |
| 溯源与签到 | 每日签到 | `checkin.quote_json_path` | 语句 JSON 字段路径 | 单行文本 | 是 | hitokoto |  |  |
| 溯源与签到 | 每日签到 | `checkin.quote_api_headers` | 语句接口请求头 | JSON 编辑器 | 是 | {} | JSON 对象 | 明文 JSON · 高级 |
| 溯源与签到 | 每日签到 | `checkin.quote_timeout` | 语句接口超时 | 数字 | 是 | 5 | 1–60 秒 | 高级 |
| 溯源与签到 | 每日签到 | `checkin.font_path` | 中文字体路径 | 单行文本 | 是 |  |  | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.enabled` | 启用多平台订阅 | 开关 | 是 | true |  | 危险 |
| 多平台订阅 | 多平台订阅 | `sns.fetch_mode` | 取数方式 | 下拉 | 是 | browser | browser=本机浏览器（推荐） / http=HTTP + Cookie / rss=RSS 兜底 | 危险 |
| 多平台订阅 | 多平台订阅 | `sns.browser_headed` | 浏览器显示窗口 | 开关 | 是 | true |  |  |
| 多平台订阅 | 多平台订阅 | `sns.browser_profile_dir` | 浏览器持久化目录 | 单行文本 | 是 |  |  | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.aggressive` | 激进档位 | 开关 | 是 | false |  | 危险 |
| 多平台订阅 | 多平台订阅 | `sns.base_interval_douyin` | 抖音轮询间隔 | 数字 | 是 | 300 | 300–86400 秒 |  |
| 多平台订阅 | 多平台订阅 | `sns.base_interval_xhs` | 小红书轮询间隔 | 数字 | 是 | 300 | 300–86400 秒 |  |
| 多平台订阅 | 多平台订阅 | `sns.base_interval_instagram` | Instagram 轮询间隔 | 数字 | 是 | 900 | 900–86400 秒 |  |
| 多平台订阅 | 多平台订阅 | `sns.jitter_ratio` | 额外抖动比例 | 数字 | 是 | 0.0 | 0–1 比例 | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.platform_concurrency` | 平台并发上限 | 数字 | 是 | 0 | 0–10 并发 | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.cooldown_base_seconds` | 风控冷却基数 | 数字 | 是 | 600 | 60–86400 秒 |  |
| 多平台订阅 | 多平台订阅 | `sns.cooldown_cap_seconds` | 风控冷却上限 | 数字 | 是 | 3600 | 60–86400 秒 |  |
| 多平台订阅 | 多平台订阅 | `sns.health_pause_threshold` | 健康暂停阈值 | 数字 | 是 | 0.2 | 0–1 比例 |  |
| 多平台订阅 | 多平台订阅 | `sns.health_degrade_threshold` | 健康降速阈值 | 数字 | 是 | 0.4 | 0–1 比例 |  |
| 多平台订阅 | 多平台订阅 | `sns.warmup_minutes` | 新订阅热身期 | 数字 | 是 | 30 | 0–1440 分钟 | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.protect_mode_hours` | 保号期 | 数字 | 是 | 24 | 0–720 小时 | 高级 |
| 多平台订阅 | 多平台订阅 | `sns.night_slowdown` | 夜间间隔翻倍 | 开关 | 是 | true |  |  |
| 多平台订阅 | 多平台订阅 | `sns.push_sessions` | 订阅推送目标 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 · 高级 |
| 多平台订阅 | 多平台订阅 | `sns.message_format` | 订阅推送模板 | 多行文本 | 是 | 📢 {platform}更新\n【{name}】{time}\n{text}\n🔗 {link} |  |  |
| 多平台订阅 | 多平台订阅 | `sns.message_with_images` | 推送附带图片 | 开关 | 是 | true |  |  |
| 多平台订阅 | 多平台订阅 | `sns.max_images` | 单条最多图片 | 数字 | 是 | 9 | 1–20 张 |  |
| 多平台订阅 | 多平台订阅 | `sns.text_max_length` | 正文截断长度 | 数字 | 是 | 300 | 1–2000 字符 |  |
| 多平台订阅 | 多平台订阅 | `sns.at_all` | 订阅推送 @全体 | 开关 | 是 | false |  | 危险 |
| 多平台订阅 | 多平台订阅 | `sns.at_all_keywords` | @全体关键词 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 · 高级 |
| 多平台订阅 | 多平台订阅 | `sns.at_all_uids` | @全体账号白名单 | 列表（每行一项） | 是 | [] | 每行一项 | 指令维护 · 高级 |
| 多平台订阅 | 多平台订阅 | `sns.douyin_cookie` | 抖音 Cookie | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 多平台订阅 | 多平台订阅 | `sns.xhs_cookie` | 小红书 Cookie | 密码（脱敏） | 是 | （空） |  | 脱敏 |
| 多平台订阅 | 多平台订阅 | `sns.instagram_cookie` | Instagram Cookie | 密码（脱敏） | 是 | （空） |  | 脱敏 |
