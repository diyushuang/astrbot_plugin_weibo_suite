# 配置手册

本手册逐项说明插件的配置键，内容以仓库根的 `_conf_schema.json` 为唯一事实来源：面板里的分组、字段顺序、类型与默认值都由这份 schema 渲染，因此本手册的表格顺序与面板顺序一致。

配置在 AstrBot 管理面板中打开：**插件 → 本插件 → 配置**，保存后写入 AstrBot 的插件配置文件。也可以直接编辑该 JSON 文件，但不推荐：AstrBot 加载时会按 schema 做一次完整性检查，schema 里没有声明的键会被丢弃，类型不符的值会被纠正，手改很容易被静默覆盖。

配置分为 15 个一级分组，另有若干用于旧版本兼容的顶层键，合计 179 个分组键（以 `_conf_schema.json` 当前内容为准，改 schema 时这份计数要一起改）。这 15 组在管理面板的「设置」页里被归入 5 个主题页（监控与推送 / 归档与管线 / 图床与存储 / 溯源与签到 / 多平台订阅），逐键映射见 [设置页键映射](./SETTINGS_MAP.md)：

| 分组 | 键数 | 作用 |
| --- | --- | --- |
| `monitor` | 8 | 微博轮询、监控账号、过滤词、出网代理与身份 |
| `push` | 26 | 推送模板、推送目标、@全体、视频与发送超时 |
| `album` | 11 | NapCat 群相册上传 |
| `archive` | 5 | 增量归档与全量回填 |
| `pipeline` | 9 | 图床与向量管线（默认关闭） |
| `copylib` | 1 | 签到文案库自动清洗 |
| `basic` | 8 | 溯源引擎、本地图库、哈希参数 |
| `delivery` | 14 | NapCat 与 QQ 官方机器人的图片/视频通道 |
| `image_bed` | 15 | 图床上传（登记原图） |
| `storage_bucket` | 15 | 对象存储桶（R2 / OCI，优先于图床） |
| `random_media` | 10 | 随机图/随机视频接口 |
| `checkin` | 12 | 每日签到 |
| `vector_search` | 13 | Qdrant 向量检索与多模态 embedding |
| `limits` | 4 | 下载体积上限与图床哈希缓存 |
| `sns` | 28 | 多平台订阅（抖音 / 小红书 / Instagram） |

## 敏感字段与脱敏

带 `secret: true` 的键在面板里按密码框渲染，不回显明文；schema 里的字段说明本身也写明了这一点。请特别注意：**脱敏只发生在面板显示层**，配置文件里仍是明文，备份、截图、发 issue 之前务必先剔除。

| 凭据 | 所在键 | 内容 |
| --- | --- | --- |
| 出网代理 | `monitor.proxy` | 代理地址，可能含 `user:pass@` |
| 微博 Cookie | `monitor.custom_cookie` | 自定义 Cookie，填写后跳过游客身份 |
| 图床 Token | `image_bed.token` | generic_http 模式的鉴权 Token |
| 图床 Token | `image_bed.cfi_token` | CloudFlare-ImgBed 的 API Token |
| 图床鉴权码 | `image_bed.cfi_auth_code` | CloudFlare-ImgBed 的 authCode |
| R2 密钥 | `storage_bucket.r2_secret_access_key` | Cloudflare R2 Secret Access Key |
| OCI 密钥 | `storage_bucket.oci_secret_access_key` | Oracle OCI Secret Key |
| 随机图 Token | `random_media.api_token` | 随机图接口 Token |
| Qdrant 密钥 | `vector_search.qdrant_api_key` | Qdrant API Key，无鉴权可留空 |
| 向量密钥 | `vector_search.embed_api_key` | 多模态 embedding 服务的 API Key |
| 抖音 Cookie | `sns.douyin_cookie` | 含 UIFID、ttwid，`http` 模式必填 |
| 小红书 Cookie | `sns.xhs_cookie` | 含 a1、web_session，`http` 模式必填 |
| IG Cookie | `sns.instagram_cookie` | 含 sessionid，`http` 模式必填 |

其余凭据类字段（如 `storage_bucket.r2_access_key_id`、`storage_bucket.oci_access_key_id`、`image_bed.cfi_base_url`）虽然未标 `secret`，但同样是账号凭据，对外分享配置时一并删除。旧布局的 `proxy` 与 `custom_cookie` 两个顶层键同样是 `secret`。

`image_bed.auth_header` 在面板里也是密码框（`secret: true`），但它只该填**请求头的名字**（如 `Authorization`、`X-API-Key`）。把整串 `Authorization: Bearer sk-xxx` 填进去并不合规——那样 token 会既出现在 `token` 里、又明文躺在 `auth_header` 里，面板的回显保护也就失效了。

`secret` 只对 `string` 与字符串 `list` 生效（AstrBot 文档口径）。上表里没有 `image_bed.extra_fields` 与 `checkin.quote_api_headers`，因为它们是自由键值对（`type: dict`），面板用 JSON 编辑器**明文**渲染、不做遮罩：字段上标 `secret` 也没有任何效果，反而会让人误以为已遮罩。这两处只要填了 `api_secret`、`Authorization: Bearer ...` 之类的值，截图、贴日志、分享配置文件都会原样带出去。

`微博 状态` 在展示出网方式时会调用 `redact_proxy` 对代理地址脱敏，所以可以直接把状态截图给群友；但配置文件本身没有这层保护。

## monitor

微博轮询与身份配置。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `poll_interval` | int | `120` | 轮询间隔（秒） |
| `monitored_uids` | list | `[]` | 监控 UID 列表，一般由 `微博 添加` 维护 |
| `include_retweets` | bool | `true` | 是否包含转发帖 |
| `filter_keywords` | list | `[]` | 正文命中这些关键词的微博直接跳过 |
| `whitelist_keywords` | list | `[]` | 白名单关键词（命中则强制推送） |
| `max_post_age_minutes` | int | `0` | 只推送最近 N 分钟内的微博（0 = 不限制） |
| `proxy` | string | `""` | 出网代理，`http://` 或 `https://` 开头（敏感） |
| `custom_cookie` | string | `""` | 自定义 Cookie，填写后跳过游客身份（敏感） |

调参建议与常见坑：

- `poll_interval` 在运行期会被夹到不小于 30 秒（`weibo/monitor.py:127` 的 `max(MIN_INTERVAL_SECONDS, ...)`），填 10 也只会按 30 跑。间隔越短风控概率越高，且每轮最多翻 3 页时间线、页间请求有 2–5 秒随机间隔，所以把它调到 60 以下几乎没有收益。
- 触发风控的典型组合是「短间隔 + 游客身份 + 代理出口 IP 被多人共用」。风控命中后插件会冷却并自动恢复，`微博 状态` 能看到剩余时间。
- `whitelist_keywords` 的优先级高于 `filter_keywords`：同一个词同时出现在两个列表里时，帖子会被推送。只想屏蔽某个词、但又想放过带另一个词的帖子时，用这个组合比调大间隔更有效。
- `max_post_age_minutes` 为 `0` 表示不按时间过滤，首轮容易把时间线里较旧的帖子一起推出去；首次添加博主并希望「只推新的」时，保持默认即可（首轮会建立基线，历史微博不推）。
- `proxy` 支持带账号密码的 URL，`微博 状态` 会脱敏后再展示；`custom_cookie` 一旦填写就不再走免 Cookie 的游客身份，失效后需要整串更新。

## push

推送内容与目标。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `at_all` | bool | `false` | @全体总开关 |
| `message_format` | string | `📢 微博更新\n【{name}】{time}\n{weibo}\n🔗 {link}` | 推送模板 |
| `push_sessions` | list | `[]` | 推送目标会话（由 `微博 绑定` 写入） |
| `push_delay_seconds` | int | `2` | 每轮推送条目之间的间隔（秒） |
| `max_push_per_round` | int | `0` | 单轮最多推送条数（0 = 不限制） |
| `max_pending_retries` | int | `5` | 待推送条目的最大重试次数 |
| `message_with_images` | bool | `true` | 推送时附带图片 |
| `message_with_videos` | bool | `true` | 推送时附带视频 |
| `max_videos_per_post` | int | `2` | 单条微博最多处理的视频数 |
| `message_send_timeout` | int | `60` | 单次发送超时（秒） |
| `text_max_length` | int | `100` | 推送正文截断长度（字符） |
| `show_full_weibo_text` | bool | `false` | 长文是否推送全文 |
| `show_hashtags` | bool | `false` | 正文是否保留话题标签 |
| `weibo_emoji_translate` | bool | `false` | 是否把微博表情码翻译成 emoji |
| `at_all_uids` | list | `[]` | @全体生效的博主 uid 列表 |
| `at_all_keywords` | list | `[]` | @全体生效的关键词（空 = 不限定） |
| `at_all_daily_limit` | int | `10` | 24 小时内 @全体 次数上限（0 = 不限制） |
| `at_all_check_permission` | bool | `true` | @全体发送前检查机器人权限 |
| `at_all_verify` | bool | `true` | @全体发送后回读确认 |
| `callback_api_base` | string | `""` | AstrBot 回调 API 基址（留空自动获取） |
| `video_prefer_file` | bool | `true` | 优先以文件方式发送视频 |
| `video_quality` | string | `hd` | 视频清晰度（`hd` / `sd`） |
| `video_max_mb` | int | `95` | 单个视频体积上限（MB） |
| `video_inline_max_mb` | int | `30` | 内联发送视频的体积上限（MB） |
| `video_file_fallback` | bool | `true` | 视频发送失败时回退为文件 |
| `video_file_temp` | bool | `true` | 视频转文件发送后清理临时文件 |

调参建议与常见坑：

- `push_sessions` 不要手填。`微博 绑定` / `微博 解绑` 会重写这个列表，手改的内容会在下次绑定时被覆盖。
- `at_all` 只是一级开关，实际是否 @ 还要过三层：必须命中 `at_all_uids` 或 `at_all_keywords`、未超过 `at_all_daily_limit`（默认 24 小时内 10 次）、且机器人有 @全体 权限（`at_all_check_permission` 为真时前置检查，`at_all_verify` 为真时发送后回读确认）。只把 `at_all` 设为 `true` 通常什么都不会发生。
- `text_max_length` 默认只有 100 字符，长微博会被截断；`show_full_weibo_text` 只影响长文（正文超过截断长度时是否改发全文），不改变普通帖的截断。
- `message_send_timeout` 是单条消息的总超时，同时开图片与视频、或目标通道是 QQ 官方机器人时建议加到 120 以上。
- 推送顺序由 `push_delay_seconds` 控制，`max_push_per_round` 用于压突发：一次检查命中很多帖子时，先推 N 条，其余留在待推送队列按重试次数继续。
- 多群推送**部分失败只补发失败的会话**：某条微博只在部分 `push_sessions` 里失败时，条目留在队列并记住 `retry_sessions`（`weibo/pusher.py:1114-1136`），下一轮只发这些会话，已送达的群不会出现同一条微博两遍（`weibo/pusher.py:891-903`）。发送结果「未知」（超时但可能已经发出）按已送达处理、不重发——宁可漏一条也不在群里刷重复；这个重试名单与已下载的媒体一起落盘，重启不丢。之后新绑定的群不会参与老条目的补发。
- `callback_api_base` 只在反向代理或容器化部署里需要手填，留空会按当前请求自动推断。

## album

NapCat 群相册上传。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `album_enabled` | bool | `true` | 启用群相册 |
| `album_rules` | list | `[]` | 群相册绑定规则（由 `微博 相册绑定` 写入） |
| `album_keyword_rules` | list | `[]` | 关键词相册路由规则 |
| `album_max_images` | int | `18` | 单条微博最多上传的图片数 |
| `album_live_gif` | bool | `true` | 实况图转 GIF 上传 |
| `album_same_host` | bool | `false` | 相册文件走与协议端相同的 host |
| `album_notify` | bool | `false` | 相册上传完成后在群里提示 |
| `album_download_concurrency` | int | `5` | 相册图片下载并发 |
| `album_upload_concurrency` | int | `3` | 相册上传并发 |
| `album_upload_interval` | float | `0.5` | 相册上传间隔（秒） |
| `album_upload_payload_mb` | float | `32.0` | 单次上传的 base64 载荷预算（MB） |

调参建议与常见坑：

- `album_max_images` 的默认值 18 就是微博单条帖子的图片上限（`weibo/constants.py:98` 的 `DEFAULT_MAX_IMAGES`），监控取数时按这个值做 `[:max_imgs]` 截断，所以调小会让多图帖「识别 18 张、只传 N 张」。老版本的默认值是 9，升级后插件会做一次性迁移把仍等于 9 的存量值上调到 18 并打标记 `album_max_images_v18`，之后再改回 9 不会被再次自动上调。
- `album_rules` 与 `album_keyword_rules` 由指令维护，不要手改；要加规则用 `微博 相册绑定`、`微博 关键词相册`、`微博 博主关键词相册`。
- `album_same_host` 在 NapCat 与图床不在同一台机器、或图床有独立访问域名时需要打开，否则协议端可能拉不到图片。
- `album_download_concurrency` / `album_upload_concurrency` 越高越容易打满带宽或触发相册接口限流；`album_upload_interval` 是上传之间的强制间隔，批量传 18 图时建议保持 0.5 秒以上。
- `album_upload_payload_mb` 是单次 base64 请求体的预算，超过会拆批。base64 会把体积撑大约三分之一，所以 32 MB 的预算对应约 24 MB 的原图总量。

## archive

增量归档与全量回填。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 启用增量归档 |
| `download_originals` | bool | `true` | 异步下载原图 |
| `auto_archive` | bool | `true` | 监控命中后自动写入归档账本 |
| `page_delay` | float | `3.0` | 全量回填页间最小间隔（秒） |
| `since` | string | `""` | 回填默认起始日期（`YYYY-MM-DD`，空为到底） |

调参建议与常见坑：

- `page_delay` 是回填时两页之间的最小间隔，默认 3 秒是保守值。调到 1 秒以内、又同时开着 `download_originals`，是最容易触发风控的组合。
- `since` 只提供**默认值**：`微博 回填` 没带 `--since` 时才用它，且必须写成 `YYYY-MM-DD`。写成 `2024` 这类弱格式会在指令里直接报错，不会静默变成全量回填。
- `since` 留空表示「回到底」，即尽量翻完整个时间线；单次回填最多 1000 页，页间隔由 `page_delay` 决定，所以一次全量可能跑很久，中途可以用 `微博 回填停止` 停下，游标保留。

## pipeline

图床与向量管线，默认关闭。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 启用微博图床管线 |
| `naming_directory` | string | `{category}/【微博图集@{account}】/` | 图床目录模板 |
| `naming_file` | string | `【微博@{account}】{date}-{index}：{title}.jpg` | 文件名模板 |
| `enabled_uids` | list | `[]` | 只对这些博主跑管线（留空 = 全部归档博主） |
| `auto_queue` | bool | `true` | 归档完成后自动入队（关掉后只能手动补跑） |
| `max_attempts` | int | `3` | 单张图最大尝试次数（超过后不再自动重试） |
| `thumb_size` | int | `720` | 缩略图长边像素（与现网 720 一致） |
| `thumb_quality` | int | `72` | 缩略图 JPEG 质量（与现网 72 一致） |
| `categories` | dict | `{}` | 博主到分类目录的映射，键可用 uid 或昵称，例如 `{"示例博主": "12、日常分享"}` |

调参建议与常见坑：

- `enabled` 默认是 `false`，因为管线依赖图床或对象存储可用。开启前先把 `storage_bucket.mode` 或 `image_bed.mode` 配好，否则帖子会一直卡在失败态并消耗 `max_attempts`。
- `categories` 决定 `{category}` 的取值：键既可以是 uid 也可以是昵称，命中不到时会用默认分类，所以新增博主后要记得补映射。
- `auto_queue` 关掉后归档不会自动入队，需要手动执行 `微博 管线补跑`；历史上传场景建议关了它再批量补，避免和在线监控抢带宽。
- 两个模板里的占位符要写全，尤其 `{index}`：去掉它会让同一条帖子的多张图生成同名文件，后上传的覆盖先上传的。

## copylib

签到文案库。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `auto_clean` | bool | `true` | 归档后自动清洗 |

`auto_clean` 打开时，归档入库的正文会自动进入文案清洗流程，`微博 文案库` 里能看到进度。关掉后只能用 `微博 文案清洗` 手动补，适合先攒素材再统一清洗的用法。

## basic

溯源引擎与本地图库。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `search_engine` | string | `auto` | 检索引擎（`auto` / `vector` / `hash`） |
| `scan_dirs` | list | `[]` | 本地图库目录 |
| `hash_similarity_threshold` | float | `0.85` | 哈希命中阈值（0–1） |
| `ai_verify` | bool | `false` | AI 视觉复核 |
| `register_admin_only` | bool | `true` | 仅管理员可登记原图 |
| `max_images_per_query` | int | `3` | 单次溯源最多处理图片数 |
| `hash_size` | int | `16` | 感知哈希精度（`4` / `8` / `12` / `16` / `20` / `24` / `28` / `32`） |
| `top_n` | int | `3` | 未命中时提示的候选数 |

调参建议与常见坑：

- `search_engine` 的 `auto` 会先试向量、失败回退哈希；选了 `vector` 但 `vector_search` 没配全时溯源会明确报错而不是降级，排障时先看这里。
- `scan_dirs` 为空时本地检索没有图源，`溯源状态` 会显示图库为空。路径写宿主机绝对路径，容器部署时要注意挂载后的路径。
- `hash_size` 改变后旧精度的缓存会自动失效，但本地图库需要跑一次 `溯源重扫` 才会按新精度重建。哈希精度越高越费内存，`16` 是精度与成本的平衡点。
- `hash_similarity_threshold` 偏高会漏召回（同图不同裁剪也可能判不中），偏低会误召回；`top_n` 只是「没命中时」给出候选，不影响命中判定。
- `register_admin_only` 设为 `false` 后普通群友也能用 `登记原图`，这是放开图库贡献的唯一方式，注意配合 `溯源删除` 的管理员权限做内容治理。
- `max_images_per_query` 限制一次消息里处理几张图，防止有人一次丢 9 张图把检索排队塞满。

## delivery

图片与视频的发送通道。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `napcat_mode` | string | `auto` | NapCat 通道（`auto` / `native` / `bytes` / `url` / `url-scaled`） |
| `napcat_max_side` | int | `1920` | 缩放/压缩最长边（像素） |
| `napcat_quality` | int | `85` | JPEG 压缩质量 |
| `napcat_verify_scaled` | bool | `true` | 实测校验缩放是否生效 |
| `napcat_scaled_url_style` | string | `query` | 缩放 URL 风格（`query` / `cf-path`） |
| `napcat_target_kb` | int | `0` | 压缩目标体积（KB，0 = 不压缩） |
| `napcat_webp` | bool | `false` | 压缩输出用 WebP |
| `napcat_timeout` | int | `60` | OneBot 图片段超时（秒） |
| `qq_mode` | string | `auto` | QQ 官方机器人通道（`auto` / `native` / `bytes` / `url` / `url-scaled`） |
| `qq_native_max_mb` | int | `3` | native 体积上限（MB） |
| `qq_auto_prefer` | string | `url` | `auto` 在 native 之外的偏好（`url` / `bytes`） |
| `qq_url_max_mb` | int | `20` | 直链通道体积上限（MB） |
| `qq_max_upload_mb` | int | `100` | 分片通道下载上限（MB） |
| `qq_on_failure` | string | `fallback` | 通道失败后的处理（`fallback` / `notify`） |

调参建议与常见坑：

- `napcat_scaled_url_style` 必须和图床/对象存储的缩放能力对齐：`query` 走查询参数缩放，`cf-path` 走 Cloudflare 的路径式缩放。风格写错时图片仍能发，但尺寸不缩，流量白白翻倍。
- `napcat_verify_scaled` 会在首次使用缩放通道时实测校验，代价是多一次请求，好处是能立刻发现「URL 改对了但服务端不认」。
- `napcat_target_kb` 非 0 时会按目标体积压缩，与 `napcat_quality` 共同生效，最终取更小的那份；`napcat_webp` 打开后输出 WebP，部分协议端对 WebP 的兼容性不如 JPEG。
- QQ 官方机器人对 native 通道有 3 MB 的硬上限，超了会走 `qq_auto_prefer` 指定的通道；`qq_on_failure` 设为 `notify` 时失败会在群里提示，设为 `fallback` 则静默换通道重试。
- `napcat_timeout` 覆盖图片段的上传等待，图床慢或多图帖建议加到 120。

## image_bed

图床上传，供登记原图与管线使用。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | `local` | 图床模式（`local` / `generic_http` / `cloudflare_imgbed`） |
| `api_url` | string | `""` | generic_http 的上传接口地址 |
| `token` | string | `""` | generic_http 的鉴权 Token（敏感） |
| `auth_header` | string | `Authorization` | 鉴权请求头名称，**只填头名，不要填整串 `Authorization: Bearer xxx`**（面板按密码框显示） |
| `auth_prefix` | string | `Bearer ` | 鉴权头值前缀 |
| `file_field` | string | `file` | 上传文件字段名 |
| `extra_fields` | dict | `{}` | 附加表单字段 |
| `url_path` | string | `data.url` | 响应中图片直链的 JSON 路径 |
| `timeout` | int | `30` | 上传超时（秒） |
| `cfi_base_url` | string | `""` | CloudFlare-ImgBed 站点地址 |
| `cfi_token` | string | `""` | CloudFlare-ImgBed API Token（敏感） |
| `cfi_auth_code` | string | `""` | 上传鉴权码 authCode（敏感） |
| `cfi_upload_channel` | string | `""` | 存储渠道 uploadChannel |
| `cfi_channel_name` | string | `""` | 渠道名称 channelName |
| `cfi_upload_folder` | string | `""` | 上传目录 uploadFolder |

调参建议与常见坑：

- `mode` 默认是 `local`，此时下面这些字段全部不生效。改成 `generic_http` 后至少要配 `api_url` 与 `url_path`，两者必须是同一套响应格式。
- `url_path` 是点号路径，`data.url` 表示取响应 JSON 的 `data.url`；图片直链藏在多层里的服务要写全，比如 `data.0.url` 这种带数组下标的路径。
- `auth_header` 与 `auth_prefix` 拼成完整请求头，默认是 `Authorization: Bearer <token>`；接口只认 `X-API-Key` 时改成对应名字并把前缀设成空字符串。这里只写头名：头值永远由 `auth_prefix + token` 生成，自己把整串塞进 `auth_header` 会拼出两层前缀，鉴权必然失败。
- `extra_fields` 用于接口要求的固定表单参数（如 `bucket`、`folder`），值是字符串，不要写成嵌套结构。
- CloudFlare-ImgBed 模式下 `cfi_base_url` 必填，`cfi_token` 与 `cfi_auth_code` 按站点是否开了鉴权二选一填写；`cfi_upload_channel` 与 `cfi_channel_name` 对应站点里的存储渠道，填错会返回「渠道不存在」。
- 图床与对象存储同时配置时，`storage_bucket` 优先，见下节。
- **上传失败不会让登记失败**：只对瞬时故障做指数退避重试（`UPLOAD_RETRY_ATTEMPTS`），重试耗尽后图会存成本地副本，同时把图床标记为「降级」（`trace/image_bed.py:238-258`、`:301-319`）。降级只影响直链形态——直链变成本地路径、日志里只有一条 warning——并且下一次上传成功会自动解除，不用重启插件；当前是否降级看 `溯源状态`（见 [指令手册](COMMANDS.md#溯源状态)）。

## storage_bucket

对象存储桶，可选，优先于图床。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | `none` | 储存桶模式（`none` / `cloudflare_r2` / `oracle_oci`） |
| `r2_account_id` | string | `""` | R2 账户 ID |
| `r2_access_key_id` | string | `""` | R2 Access Key ID |
| `r2_secret_access_key` | string | `""` | R2 Secret Access Key（敏感） |
| `r2_bucket` | string | `""` | R2 存储桶名称 |
| `r2_endpoint` | string | `""` | R2 S3 endpoint |
| `r2_public_base_url` | string | `""` | R2 公开访问域名 |
| `oci_namespace` | string | `""` | OCI 对象存储命名空间 |
| `oci_region` | string | `""` | OCI 区域标识 |
| `oci_access_key_id` | string | `""` | OCI Access Key |
| `oci_secret_access_key` | string | `""` | OCI Secret Key（敏感） |
| `oci_bucket` | string | `""` | OCI 存储桶名称 |
| `oci_endpoint` | string | `""` | OCI S3 endpoint |
| `oci_public_bucket` | bool | `false` | 存储桶已设为公共可读 |
| `oci_public_base_url` | string | `""` | 自定义公开地址 |

调参建议与常见坑：

- `mode` 保持 `none` 时整组配置不生效；配置了 `cloudflare_r2` 或 `oracle_oci` 后，它与 `image_bed` 同时可用时以本组为准。
- R2 只需要 Account ID、Access Key、Secret、Bucket 与公开域名，`r2_endpoint` 留空时按账户 ID 自动拼；`r2_public_base_url` 必填，否则拿不到能直接访问的图片链接。
- OCI 的公开访问是二选一：桶本身公共可读就打开 `oci_public_bucket`，用自定义域名就填 `oci_public_base_url`，两个都空会导致回传的链接不可访问。
- 从 v1.2.x 升到 v1.3.0 时，旧版写在 `image_bed.mode` 里的 `cloudflare_r2` / `oracle_oci` 会被自动搬迁到本组，且在本组已经有 `mode` 时不覆盖，所以升级后不要手动再迁一次。

## random_media

随机图与随机视频。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `""` | 图床站点地址（必填） |
| `lan_base_url` | string | `""` | 图床内网直连地址 |
| `api_endpoint` | string | `/random` | 随机图接口路径 |
| `api_token` | string | `""` | 随机图接口 Token（敏感） |
| `default_dir` | string | `""` | 默认目录 |
| `timeout` | float | `10` | 请求超时（秒） |
| `retry_count` | int | `3` | 失败重试次数 |
| `show_file_info` | bool | `true` | 回传时附带文件名 |
| `enable_llm` | bool | `true` | 允许 LLM 工具调用 |
| `send_thumbnail` | bool | `true` | 随机图发送缩略图 |

调参建议与常见坑：

- `base_url` 为空时 `随机图` / `随机视频` 没有图源，会直接报未配置。
- `lan_base_url` 用于「插件与被调用的图床在同一内网」的部署：配了之后优先走内网直连，省掉外网回环流量。跨机部署不要填内网地址，否则请求会超时。
- `enable_llm` 控制 `sendRandomMedia` 这个 LLM 工具是否可用（模型可以自己决定发一张随机图）。不想让模型主动发图就关掉它，聊天指令不受影响。
- `send_thumbnail` 打开时发缩略图，体积小、刷屏轻；关掉后发原图，适合需要原画质的场景。
- `retry_count` 与 `timeout` 一起决定最坏等待时间，超时 × 重试次数超过 30 秒时用户会明显感到卡顿。

## checkin

每日签到。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 启用每日签到 |
| `mode` | string | `quote` | 签到内容模式（`quote` / `weibo`） |
| `render_mode` | string | `card` | 图文呈现方式（`card` / `simple`） |
| `border_px` | int | `0` | 卡片边框宽度（像素） |
| `utc_offset_hours` | int | `8` | 签到时区（UTC 偏移小时） |
| `quote_source` | string | `builtin` | 语句来源（`builtin` / `api` / `mixed`） |
| `quote_local_text` | text | `""` | 本地语句，每行一句 |
| `quote_api_url` | string | `https://v1.hitokoto.cn/?encode=json&c=d&c=i&c=k` | 语句接口地址 |
| `quote_json_path` | string | `hitokoto` | 语句 JSON 字段路径 |
| `quote_api_headers` | dict | `{}` | 语句接口请求头 |
| `quote_timeout` | int | `5` | 语句接口超时（秒） |
| `font_path` | string | `""` | 中文字体文件路径 |

调参建议与常见坑：

- `mode` 设为 `weibo` 时走微博签到分支，依赖微博身份（游客或 `monitor.custom_cookie`）；设为 `quote` 则只发本地语句卡片。
- `quote_source` 为 `builtin` 只用 `quote_local_text`，为 `api` 只请求 `quote_api_url`，为 `mixed` 时两者都可用（接口失败回落本地）。
- `render_mode` 为 `card` 会渲染图片卡片，依赖可用的中文字体；`font_path` 留空时用系统默认字体，容器或精简系统里常见中文变成方块，此时指向一个自备的 `.ttf` 即可。`simple` 则是纯文本，不依赖字体。
- `utc_offset_hours` 决定「今天」的边界，默认 8 对应东八区；部署在其他时区时如果不改，会出现「下午签到算明天」的错位。
- 语句接口的响应路径用 `quote_json_path` 指定，换接口后这个字段几乎一定要改；需要鉴权的接口把请求头写进 `quote_api_headers`。

## vector_search

Qdrant 向量检索与多模态 embedding。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `qdrant_url` | string | `""` | Qdrant 服务地址（必填） |
| `qdrant_api_key` | string | `""` | Qdrant API Key（敏感） |
| `collection_name` | string | `imgbed_images` | Qdrant 集合名 |
| `embed_base_url` | string | `""` | 多模态向量 AI 地址（必填） |
| `embed_api_key` | string | `""` | 向量 AI API Key（敏感） |
| `embed_model` | string | `""` | 多模态图片向量模型（必填） |
| `similarity_threshold` | float | `0.8` | 向量相似度阈值（0–1） |
| `duplicate_vector_threshold` | float | `0.995` | 重复图片向量阈值（0–1） |
| `vector_index_on_register` | bool | `true` | 登记原图时同步写入向量库 |
| `embed_image_input` | string | `nemotron-vl` | 图片输入序列化格式（`nemotron-vl` / `qwen-vl` / `dataurl` / `jina-image`） |
| `embed_input_type` | string | `passage` | NVIDIA 非对称模型的 input_type（`passage` / `query`） |
| `top_k` | int | `5` | 向量检索返回候选数 |
| `request_timeout` | int | `30` | 请求超时（秒） |

调参建议与常见坑：

- `qdrant_url`、`embed_base_url`、`embed_model` 三个是必填项，缺一个向量引擎就起不来；此时把 `basic.search_engine` 改成 `hash` 或 `auto` 至少能保住哈希检索。
- `embed_image_input` 必须和目标模型的输入约定一致：NVIDIA 系模型用 `nemotron-vl`，Qwen 系用 `qwen-vl`，纯 OpenAI 兼容接口用 `dataurl`。写错不会报错，只会退化成「永远检索不到」。
- `embed_input_type` 只对非对称模型有意义，检索侧与建库侧的取值不同（建库 `passage`、检索 `query` 这类约定），配错会明显拉低相似度。
- `similarity_threshold` 是「算不算命中」的门槛，`duplicate_vector_threshold` 是「算不算重复图」的门槛，后者更严格（默认 0.995），用于登记时去重。`top_k` 只影响候选数量，不影响命中判定。
- `vector_index_on_register` 关闭后登记原图不写向量库，只能靠 `溯源重扫` 补，适合 embedding 服务不稳定时先保证登记不失败。

## limits

下载体积与图床哈希缓存。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `max_download_mb` | int | `20` | 单张图片下载上限（MB） |
| `remote_hash_enabled` | bool | `true` | 图床哈希索引 |
| `remote_hash_ttl` | int | `21600` | 哈希缓存有效期（秒） |
| `remote_hash_max_points` | int | `0` | 哈希拉取上限（0 = 不限） |

调参建议与常见坑：

- `remote_hash_enabled` 打开后启动时会从图床拉取预置 pHash，让哈希引擎也能反查图床里的图片；它只需要配置 Qdrant 地址，不依赖 embedding，所以没配向量模型也能用。
- `remote_hash_ttl` 默认 6 小时，超期或缓存为空时重建；切换 `basic.hash_size` 后旧精度的缓存会自动失效，无需手动清。
- `remote_hash_max_points` 为 `0` 表示不限量，图床很大时首次拉取会明显拖慢启动；调试阶段建议设成 50 先验证链路。
- `max_download_mb` 同时约束归档原图与相册下载，超过上限的图会被跳过并计一次失败，宁可放宽也不要让大图帖整体失败。

## sns

多平台订阅（抖音 / 小红书 / Instagram）。

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 启用多平台订阅（关闭后不轮询、不推送） |
| `fetch_mode` | string | `browser` | 取数方式（`browser` / `http` / `rss`） |
| `browser_headed` | bool | `true` | 浏览器显示窗口（关闭则无头运行） |
| `browser_profile_dir` | string | `""` | 浏览器持久化目录（留空 = 插件数据目录下 `sns/browser`） |
| `aggressive` | bool | `false` | 激进档位：间隔降到 180/180/600 秒 |
| `base_interval_douyin` | int | `300` | 抖音基础轮询间隔（秒，最小 300） |
| `base_interval_xhs` | int | `300` | 小红书基础轮询间隔（秒，最小 300） |
| `base_interval_instagram` | int | `900` | Instagram 基础轮询间隔（秒，最小 900） |
| `jitter_ratio` | float | `0.0` | 额外抖动比例（0 = 用平台内置比例，只向长的一侧抖） |
| `platform_concurrency` | int | `0` | 平台并发上限（0 = 用内置值：抖音 2 / 小红书 1 / IG 1） |
| `cooldown_base_seconds` | int | `600` | 风控冷却基数（秒） |
| `cooldown_cap_seconds` | int | `3600` | 风控冷却上限（秒） |
| `health_pause_threshold` | float | `0.2` | 健康度低于该值自动暂停账号 |
| `health_degrade_threshold` | float | `0.4` | 健康度低于该值间隔翻 1.5 倍 |
| `warmup_minutes` | int | `30` | 新订阅热身期（分钟，间隔翻倍） |
| `protect_mode_hours` | int | `24` | 保号期（小时，间隔翻倍） |
| `night_slowdown` | bool | `true` | 夜间（0–8 点）间隔翻倍 |
| `push_sessions` | list | `[]` | 推送目标会话（留空则沿用微博的推送目标） |
| `message_format` | string | `📢 {platform}更新\n【{name}】{time}\n{text}\n🔗 {link}` | 推送文案模板 |
| `message_with_images` | bool | `true` | 推送时附带图片 |
| `max_images` | int | `9` | 每条推送最多带几张图 |
| `text_max_length` | int | `300` | 正文截断长度 |
| `at_all` | bool | `false` | 推送时 @全体成员（需机器人在群里是管理员） |
| `at_all_keywords` | list | `[]` | @全体关键词（留空 = 不过滤） |
| `at_all_uids` | list | `[]` | @全体生效的账号白名单（留空 = 不限；填的是订阅账号的 ID / 名称 / 平台标识） |
| `douyin_cookie` | string | `""` | 抖音 Cookie（`http` 模式必填，敏感） |
| `xhs_cookie` | string | `""` | 小红书 Cookie（`http` 模式必填，敏感） |
| `instagram_cookie` | string | `""` | Instagram Cookie（`http` 模式必填，敏感） |

调参建议与常见坑：

- 三通道的取舍：`browser`（默认）复用本机持久化浏览器，Cookie 不出浏览器，是最稳的保号方式；`http` 需要自备对应平台的 Cookie 并承担失效风险；`rss` 只对提供了 RSS 的源有效，作为备选。
- 间隔下限是硬约束：`base_interval_douyin` / `base_interval_xhs` 低于 300、`base_interval_instagram` 低于 900 都会被抬回下限（`sns/service.py:112-118` 的 `max(floor, value)`）。
- `aggressive` 打开后取 `min(配置值, 180/180/600)` 且不低于 60 秒，风控概率显著上升。它不会覆盖你的配置值往上调，只会往下压，所以关掉后恢复成原来的间隔。
- `health_pause_threshold` 会被夹到不超过 `health_degrade_threshold`（`sns/service.py:131-134` 取 `min`），把暂停线设得比降速线高不会生效，也不会报错。
- 保号参数叠加生效：新订阅前 `warmup_minutes` 分钟间隔翻倍，订阅后 `protect_mode_hours` 小时内间隔翻倍，`night_slowdown` 打开的 0–8 点间隔再翻倍。三个都默认开启，所以新号在夜间实际轮询间隔可能是基础值的数倍，这是「加了订阅半天没推送」的常见原因。
- `jitter_ratio` 为 `0` 时用平台内置抖动比例且只向长的一侧抖；手动填正数会在内置基础上再加一层，一般不需要动。
- `platform_concurrency` 为 `0` 时用内置并发（抖音 2 / 小红书 1 / IG 1）。调高能加快多号检查，但同一平台的请求会挤在一起，比调低间隔更容易触发风控。
- `push_sessions` 留空时沿用微博的推送目标（`sns/service.py:137-139` 的回落逻辑），只想给订阅单独建群时再填。
- `sns.at_all_uids` 与 `push.at_all_uids` 是**两个不同的键**，各管各的域：前者匹配订阅账号的账号 ID、名称或平台标识（`weibo/pusher.py:1853-1872` 的 `_sns_at_all_hit` 拿 `at_all_keys` 去比），后者匹配微博博主 uid。填 `douyin` 等于给抖音平台的所有账号开 @全体；留空表示白名单不设限，此时能不能 @ 只取决于 `at_all` 开关、关键词与群管理员权限这三关。
- `message_format` 支持 `{platform}`、`{name}`、`{time}`、`{text}`、`{link}` 五个占位符，模板里写错占位符不会报错，只会原样输出。
- `cooldown_base_seconds` 与 `cooldown_cap_seconds` 共同决定风控冷却的指数退避区间，默认 10 分钟起、最多 1 小时。

## 旧布局迁移键

`_conf_schema.json` 里有一批顶层键，描述统一以「（旧布局迁移用，请勿修改）」开头，共 59 条；另有 1 条内部键 `config_layout`。它们存在的唯一原因是**让老部署的旧值能活到迁移代码执行的那一刻**。

### 为什么不能直接删掉旧键

老版本把所有配置平铺在顶层（`poll_interval`、`monitored_uids`、`message_format`、`album_max_images` 等）。新版本把它们搬进了 `monitor` / `push` / `album` / `basic` / `delivery` / `limits` 等分组。问题在于 AstrBot 加载配置时会按 schema 做完整性检查（`AstrBotConfig.check_config_integrity`），schema 里没声明的键会被丢掉。

于是如果新版直接在 schema 里删掉旧键，用户升级后：旧键的值在**插件初始化之前**就被 schema 的默认值顶掉了，迁移代码再去读旧键只能读到默认值，用户配置等于被清空。所以旧键必须继续声明在 schema 里，作为「隐藏镜像」留着，由迁移代码把值搬进新分组后再打上版本标记。

### 迁移怎么发生

主迁移在 `trace/main.py` 的 `_migrate_config_layout`：

1. 先看 `config_layout` 是否等于当前布局版本 `_LAYOUT_VERSION`，相等就直接返回，所以迁移只跑一次。
2. 遍历 `_SCALAR_PATHS`，把旧平铺键里**非空**的值搬进 `basic` / `delivery` / `limits`；`image_delivery` 与 `qq_upload` 两个 dict 分别按 `_NAPCAT_KEY_MAP`、`_QQ_KEY_MAP` 映射搬进 `delivery`。
3. 结尾写 `config_layout = _LAYOUT_VERSION` 并 `save_config()`。写盘失败只记 warning（「配置布局迁移写入失败（重启后会自动重试）」），下次启动会重试；真正搬过值时会记一条 info（「配置已迁移到新布局：basic / delivery / limits（旧键保留为隐藏镜像）」）。

旧值优先于新键，因为新键在首次加载时已经被 schema 默认值填满，此刻它还不代表用户的意图。迁移只做搬运并打标记，之后用户在新分组里改的值不会被旧键反向覆盖。

### 两种镜像的区别

- **隐藏镜像**：14 条早期键（`search_engine`、`scan_dirs`、`similarity_threshold`、`hash_size`、`top_n`、`max_images_per_query`、`ai_verify`、`register_admin_only`、`max_download_mb`、`remote_hash_enabled`、`remote_hash_ttl`、`remote_hash_max_points`、`image_delivery`、`qq_upload`）连同 `config_layout`，都带 `"invisible": true` 与 `"condition": {"advanced_settings": "__legacy__"}`。这个条件指向一个 schema 里不存在的字段，永远不成立，所以面板里根本不显示它们。
- **运行时镜像**：微博域的 45 条旧键（`poll_interval`、`monitored_uids`、`push_sessions`、`message_format`、`album_*` 等）没有隐藏标记，面板顶层仍会出现。它们不是历史残留，而是微博域代码仍在读的镜像：`weibo/plugin.py:24` 的 `WEIBO_CFG_PATHS` 把 45 个平铺键映射到「分组 + 字段」，`_sync_config_groups`（`weibo/plugin.py:463`）在启动时以分组值为准覆盖平铺镜像，`_set_cfg`（`weibo/plugin.py:475`）在写配置时两边同时写。所以改分组键会同步到镜像，改镜像下一轮就被覆盖。

### 用户该怎么做

- 只改分组里的键。旧顶层键（无论是否显示）都不需要也不应该手动编辑。
- 不用为了「清掉旧键」去删配置：隐藏镜像不参与运行，删了反而会让某些尚未迁移完的部署丢值。
- 看到 `config_layout` 是空字符串不用管，第一次启动时迁移会把它写成当前版本号。
- 如果怀疑迁移没跑，看日志里有没有「配置已迁移到新布局」；迁移是幂等的，重启一次会重试。

### 另外两处一次性迁移

- `_migrate_bucket_config`（`trace/state_mixin.py:288`）：v1.2.x → v1.3.0 时把 `image_bed.mode` 里的 `cloudflare_r2` / `oracle_oci` 搬到 `storage_bucket`，若 `storage_bucket.mode` 已有值则不覆盖。
- `_migrate_album_max_images`（`weibo/plugin.py:485`）：v1.11.x 及以前的 `album_max_images` 默认值是 9，AstrBot 会把默认值物化进部署配置，导致升级后新默认值 18 不生效。迁移只在「当前值恰好等于旧默认值 9」时上调到 18，并在 `state.json` 记一个 `album_max_images_v18` 标记，之后不再自动改动，所以迁移后手动改回 9 是允许的。

## 相关文档

- [指令手册](./COMMANDS.md)
- [WebUI 控制面板](./WEBUI.md)
- [贡献指南](../CONTRIBUTING.md)
