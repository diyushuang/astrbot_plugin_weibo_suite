"""设置页目录：把 `_conf_schema.json` 的 15 个可见分组映射到主题页。

设计约束：

- **唯一事实源**是仓库根的 `_conf_schema.json`：类型、默认值、枚举、条件与
  secret 标记全部从 schema 读取，这里只补面板需要的展示信息（短标签、单位、
  建议范围、危险标记、折叠归类）。
- 每个可见键必须且只能出现在一个页面/分组里。`build_catalog()` 在缺标签、
  缺归类或出现未知键时直接抛错，测试再对 schema 做一次全量覆盖断言。
- 范围（`min` / `max` / `step`）是**面板输入约束**，不等于第三方平台的硬限制；
  真正会被运行期夹紧的值在文档与代码里另有说明。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_ROOT / "_conf_schema.json"

# 主题页：同组配置不跨页，跨页不重复。15 个可见分组全部落在前 5 页，第 6 页
# 只放版本/覆盖/安全说明，不含配置键。
PAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "monitor-push",
        "title": "监控与推送",
        "summary": "轮询与过滤、推送模板、@全体、群相册，以及 NapCat / QQ 图片通道。",
        "groups": ("monitor", "push", "album", "delivery"),
    },
    {
        "id": "archive-pipeline",
        "title": "归档与管线",
        "summary": "归档账本、全量回填、图床向量管线与文案库的自动清洗。",
        "groups": ("archive", "pipeline", "copylib"),
    },
    {
        "id": "storage",
        "title": "图床与存储",
        "summary": "登记原图走哪条上传链路：通用图床、CloudFlare-ImgBed、R2 或 OCI。",
        "groups": ("image_bed", "storage_bucket"),
    },
    {
        "id": "trace-checkin",
        "title": "溯源与签到",
        "summary": "哈希/向量检索引擎、本地图库、随机图接口与每日签到。",
        "groups": ("basic", "vector_search", "limits", "random_media", "checkin"),
    },
    {
        "id": "sns",
        "title": "多平台订阅",
        "summary": "抖音 / 小红书 / Instagram 的取数方式、风控守卫与推送文案。",
        "groups": ("sns",),
    },
    {
        "id": "about",
        "title": "关于与安全",
        "summary": "版本、配置覆盖情况与凭据安全边界。",
        "groups": (),
        "about": True,
    },
)

GROUP_META: dict[str, dict[str, str]] = {
    "monitor": {
        "title": "微博监控",
        "summary": "轮询节奏、监控名单、过滤规则、出网代理与身份。",
        "accent": "red",
    },
    "push": {
        "title": "推送与 @全体",
        "summary": "消息模板、推送目标、重试节奏、视频限制与 @全体闸门。",
        "accent": "red",
    },
    "album": {
        "title": "群相册",
        "summary": "博主的图片上传到哪个群相册，以及并发与载荷预算。",
        "accent": "amber",
    },
    "delivery": {
        "title": "图片投递通道",
        "summary": "NapCat / OneBot 与 QQ 官方机器人的图片发送策略。",
        "accent": "amber",
    },
    "archive": {
        "title": "归档账本",
        "summary": "增量归档、原图下载、回填页间隔与默认起始日期。",
        "accent": "amber",
    },
    "pipeline": {
        "title": "图床与向量管线",
        "summary": "归档后自动上传、缩略图规格、命名模板与失败重试。",
        "accent": "amber",
    },
    "copylib": {
        "title": "文案库",
        "summary": "归档正文是否自动进入文案清洗流程。",
        "accent": "amber",
    },
    "image_bed": {
        "title": "图床上传",
        "summary": "通用 HTTP 图床与 CloudFlare-ImgBed 的接口参数。",
        "accent": "teal",
    },
    "storage_bucket": {
        "title": "对象存储",
        "summary": "Cloudflare R2 与 Oracle OCI 的 S3 兼容配置；优先于图床。",
        "accent": "teal",
    },
    "basic": {
        "title": "溯源引擎",
        "summary": "检索引擎、本地图库目录、哈希精度与结果数量。",
        "accent": "teal",
    },
    "vector_search": {
        "title": "向量检索",
        "summary": "Qdrant 与多模态 embedding 服务的连接参数。",
        "accent": "teal",
    },
    "limits": {
        "title": "限制与缓存",
        "summary": "单图下载上限、图床哈希索引与缓存刷新。",
        "accent": "teal",
    },
    "random_media": {
        "title": "随机图",
        "summary": "图床 /random 接口、内网直连、超时与 LLM 工具开关。",
        "accent": "teal",
    },
    "checkin": {
        "title": "每日签到",
        "summary": "签到内容、卡片样式、语句来源与中文字体。",
        "accent": "teal",
    },
    "sns": {
        "title": "多平台订阅",
        "summary": "三平台取数、间隔、风控守卫、推送模板与 Cookie 凭据。",
        "accent": "blue",
    },
}


def _f(label: str, **extra: Any) -> dict[str, Any]:
    """构造一条字段展示元数据。"""
    meta: dict[str, Any] = {"label": label}
    meta.update(extra)
    return meta


# 短标签 + 单位/范围/危险/高级/维护方式。类型、默认值、枚举与 secret 来自 schema。
FIELD_META: dict[str, dict[str, Any]] = {
    # ---------------- monitor ----------------
    "monitor.poll_interval": _f("轮询间隔", unit="秒", min=30, max=3600, step=5),
    "monitor.monitored_uids": _f("监控 UID 列表", managed=True),
    "monitor.include_retweets": _f("包含转发帖"),
    "monitor.filter_keywords": _f("屏蔽关键词"),
    "monitor.whitelist_keywords": _f("白名单关键词"),
    "monitor.max_post_age_minutes": _f("最大帖子时效", unit="分钟", min=0, max=10080, step=1),
    "monitor.proxy": _f("出网代理", placeholder="http://127.0.0.1:7890"),
    "monitor.custom_cookie": _f("自定义 Cookie", placeholder="留空则使用免 Cookie 游客身份"),
    # ---------------- push ----------------
    "push.at_all": _f("@全体总开关", danger=True),
    "push.message_format": _f("推送模板", multiline=True),
    "push.push_sessions": _f("推送目标会话", managed=True),
    "push.push_delay_seconds": _f("推送条目间隔", unit="秒", min=0, max=60, step=1),
    "push.max_push_per_round": _f("单轮最多推送", unit="条", min=0, max=200, step=1),
    "push.max_pending_retries": _f("最大重试次数", unit="次", min=0, max=20, step=1),
    "push.message_with_images": _f("推送附带图片"),
    "push.message_with_videos": _f("推送附带视频"),
    "push.max_videos_per_post": _f("单帖最多视频", unit="个", min=0, max=10, step=1),
    "push.message_send_timeout": _f("单次发送超时", unit="秒", min=5, max=300, step=5),
    "push.text_max_length": _f("正文截断长度", unit="字符", min=1, max=2000, step=1),
    "push.show_full_weibo_text": _f("长文推送全文"),
    "push.show_hashtags": _f("保留话题标签"),
    "push.weibo_emoji_translate": _f("微博表情转 emoji"),
    "push.at_all_uids": _f("@全体博主名单", managed=True),
    "push.at_all_keywords": _f("@全体关键词", managed=True),
    "push.at_all_daily_limit": _f(
        "24 小时 @全体上限", unit="次", min=0, max=50, step=1, advanced=True
    ),
    "push.at_all_check_permission": _f("@全体发送前检查权限", advanced=True),
    "push.at_all_verify": _f("@全体发送后回读确认", advanced=True),
    "push.callback_api_base": _f(
        "回调 API 基址", advanced=True, placeholder="https://astrbot.example.com"
    ),
    "push.video_prefer_file": _f("视频优先按文件发送", advanced=True),
    "push.video_quality": _f("视频清晰度", advanced=True),
    "push.video_max_mb": _f("单个视频上限", unit="MB", min=1, max=500, step=1, advanced=True),
    "push.video_inline_max_mb": _f(
        "内联视频上限", unit="MB", min=1, max=200, step=1, advanced=True
    ),
    "push.video_file_fallback": _f("视频失败回退文件", advanced=True),
    "push.video_file_temp": _f("视频转文件后清理临时文件", advanced=True),
    # ---------------- album ----------------
    "album.album_enabled": _f("启用群相册"),
    "album.album_rules": _f("相册绑定规则", managed=True),
    "album.album_keyword_rules": _f("关键词相册路由", managed=True),
    "album.album_max_images": _f("单帖最多上传图片", unit="张", min=1, max=18, step=1),
    "album.album_live_gif": _f("实况图转 GIF"),
    "album.album_same_host": _f("相册文件走协议端 host", advanced=True),
    "album.album_notify": _f("上传完成群内提示", advanced=True),
    "album.album_download_concurrency": _f(
        "图片下载并发", unit="并发", min=1, max=20, step=1, advanced=True
    ),
    "album.album_upload_concurrency": _f(
        "相册上传并发", unit="并发", min=1, max=10, step=1, advanced=True
    ),
    "album.album_upload_interval": _f(
        "相册上传间隔", unit="秒", min=0, max=10, step=0.1, advanced=True
    ),
    "album.album_upload_payload_mb": _f(
        "单次上传载荷预算", unit="MB", min=1, max=100, step=1, advanced=True
    ),
    # ---------------- delivery ----------------
    "delivery.napcat_mode": _f("NapCat 通道"),
    "delivery.napcat_max_side": _f("缩放最长边", unit="像素", min=1, max=4096, step=1),
    "delivery.napcat_quality": _f("JPEG 压缩质量", unit="%", min=1, max=100, step=1),
    "delivery.napcat_verify_scaled": _f("实测校验缩放", advanced=True),
    "delivery.napcat_scaled_url_style": _f("缩放 URL 风格", advanced=True),
    "delivery.napcat_target_kb": _f("压缩目标体积", unit="KB", min=0, max=10000, step=10),
    "delivery.napcat_webp": _f("压缩输出 WebP"),
    "delivery.napcat_timeout": _f("图片段超时", unit="秒", min=10, max=300, step=5),
    "delivery.qq_mode": _f("QQ 官方通道"),
    "delivery.qq_native_max_mb": _f("native 体积上限", unit="MB", min=0, max=5, step=1),
    "delivery.qq_auto_prefer": _f("auto 通道偏好"),
    "delivery.qq_url_max_mb": _f("直链体积上限", unit="MB", min=0, max=100, step=1),
    "delivery.qq_max_upload_mb": _f("分片下载上限", unit="MB", min=1, max=500, step=1),
    "delivery.qq_on_failure": _f("通道失败处理"),
    # ---------------- archive ----------------
    "archive.enabled": _f("启用增量归档", danger=True),
    "archive.download_originals": _f("异步下载原图"),
    "archive.auto_archive": _f("监控命中后自动归档"),
    "archive.page_delay": _f("回填页间间隔", unit="秒", min=0, max=60, step=0.5, advanced=True),
    "archive.since": _f("回填默认起始日期", placeholder="YYYY-MM-DD"),
    # ---------------- pipeline ----------------
    "pipeline.enabled": _f("启用图床管线", danger=True),
    "pipeline.naming_directory": _f("目录模板"),
    "pipeline.naming_file": _f("文件名模板"),
    "pipeline.categories": _f("博主分类映射"),
    "pipeline.enabled_uids": _f("只跑这些博主"),
    "pipeline.auto_queue": _f("归档后自动入队"),
    "pipeline.max_attempts": _f("单张最大尝试", unit="次", min=1, max=10, step=1),
    "pipeline.thumb_size": _f("缩略图长边", unit="像素", min=64, max=2048, step=8),
    "pipeline.thumb_quality": _f("缩略图质量", unit="%", min=1, max=100, step=1),
    # ---------------- copylib ----------------
    "copylib.auto_clean": _f("归档后自动清洗"),
    # ---------------- basic ----------------
    "basic.search_engine": _f("检索引擎"),
    "basic.scan_dirs": _f("本地图库目录"),
    "basic.hash_similarity_threshold": _f("哈希命中阈值", unit="比例", min=0, max=1, step=0.01),
    "basic.ai_verify": _f("AI 视觉复核", advanced=True),
    "basic.register_admin_only": _f("仅管理员可登记原图", danger=True, advanced=True),
    "basic.max_images_per_query": _f(
        "单次溯源最多图片", unit="张", min=1, max=10, step=1, advanced=True
    ),
    "basic.hash_size": _f("感知哈希精度", unit="格", advanced=True),
    "basic.top_n": _f("未命中候选数", unit="张", min=0, max=50, step=1, advanced=True),
    # ---------------- image_bed ----------------
    "image_bed.mode": _f("图床模式", danger=True),
    "image_bed.api_url": _f("上传接口地址", placeholder="https://bed.example.com/api/v1/upload"),
    "image_bed.token": _f("图床鉴权 Token", placeholder="留空则不携带鉴权信息"),
    "image_bed.auth_header": _f("鉴权请求头名称", placeholder="Authorization"),
    "image_bed.auth_prefix": _f("鉴权头值前缀", placeholder="Bearer "),
    "image_bed.file_field": _f("上传文件字段名", placeholder="file"),
    "image_bed.extra_fields": _f("附加表单字段", plaintext=True),
    "image_bed.url_path": _f("响应直链路径", placeholder="data.url"),
    "image_bed.timeout": _f("上传超时", unit="秒", min=5, max=300, step=5, advanced=True),
    "image_bed.cfi_base_url": _f("ImgBed 站点地址", placeholder="https://img.example.com"),
    "image_bed.cfi_token": _f("ImgBed API Token", placeholder="留空则不携带 Authorization"),
    "image_bed.cfi_auth_code": _f("上传鉴权码 authCode", placeholder="留空则只用 API Token"),
    "image_bed.cfi_upload_channel": _f(
        "存储渠道 uploadChannel", placeholder="telegram / cfr2 / s3…"
    ),
    "image_bed.cfi_channel_name": _f("渠道名称 channelName", placeholder="多渠道站点才需要"),
    "image_bed.cfi_upload_folder": _f("上传目录 uploadFolder", placeholder="img/test"),
    # ---------------- storage_bucket ----------------
    "storage_bucket.mode": _f("储存桶模式", danger=True),
    "storage_bucket.r2_account_id": _f("R2 账户 ID"),
    "storage_bucket.r2_access_key_id": _f("R2 Access Key ID"),
    "storage_bucket.r2_secret_access_key": _f("R2 Secret Access Key"),
    "storage_bucket.r2_bucket": _f("R2 存储桶名称"),
    "storage_bucket.r2_endpoint": _f(
        "R2 S3 endpoint", advanced=True, placeholder="留空使用官方默认 endpoint"
    ),
    "storage_bucket.r2_public_base_url": _f(
        "R2 公开访问域名", placeholder="https://pub-xxxx.r2.dev"
    ),
    "storage_bucket.oci_namespace": _f("OCI 命名空间"),
    "storage_bucket.oci_region": _f("OCI 区域标识", placeholder="ap-osaka-1"),
    "storage_bucket.oci_access_key_id": _f("OCI Access Key"),
    "storage_bucket.oci_secret_access_key": _f("OCI Secret Key"),
    "storage_bucket.oci_bucket": _f("OCI 存储桶名称"),
    "storage_bucket.oci_endpoint": _f("OCI S3 endpoint", advanced=True),
    "storage_bucket.oci_public_bucket": _f("存储桶公共可读"),
    "storage_bucket.oci_public_base_url": _f("自定义公开地址"),
    # ---------------- random_media ----------------
    "random_media.base_url": _f("图床站点地址", placeholder="https://img.example.com"),
    "random_media.lan_base_url": _f("内网直连地址", placeholder="http://127.0.0.1:7658"),
    "random_media.api_endpoint": _f("随机图接口路径", placeholder="/random"),
    "random_media.api_token": _f("随机图接口 Token", placeholder="站点未开启鉴权时留空"),
    "random_media.default_dir": _f("默认目录", placeholder="风景/2026"),
    "random_media.timeout": _f("请求超时", unit="秒", min=1, max=60, step=0.5),
    "random_media.retry_count": _f("失败重试次数", unit="次", min=0, max=10, step=1),
    "random_media.show_file_info": _f("回传附带文件名"),
    "random_media.enable_llm": _f("允许 LLM 工具调用"),
    "random_media.send_thumbnail": _f("发送缩略图"),
    # ---------------- checkin ----------------
    "checkin.enabled": _f("启用每日签到"),
    "checkin.mode": _f("签到内容模式"),
    "checkin.render_mode": _f("图文呈现方式"),
    "checkin.border_px": _f("卡片边框宽度", unit="像素", min=0, max=200, step=1),
    "checkin.utc_offset_hours": _f("签到时区", unit="小时", min=-12, max=14, step=1),
    "checkin.quote_source": _f("语句来源"),
    "checkin.quote_local_text": _f("本地语句", multiline=True),
    "checkin.quote_api_url": _f("语句接口地址", placeholder="https://v1.hitokoto.cn/?encode=json"),
    "checkin.quote_json_path": _f("语句 JSON 字段路径", placeholder="hitokoto"),
    "checkin.quote_api_headers": _f("语句接口请求头", advanced=True, plaintext=True),
    "checkin.quote_timeout": _f("语句接口超时", unit="秒", min=1, max=60, step=1, advanced=True),
    "checkin.font_path": _f("中文字体路径", advanced=True, placeholder="留空自动查找系统字体"),
    # ---------------- vector_search ----------------
    "vector_search.qdrant_url": _f("Qdrant 服务地址", placeholder="http://127.0.0.1:6333"),
    "vector_search.qdrant_api_key": _f("Qdrant API Key", placeholder="未开启鉴权时留空"),
    "vector_search.collection_name": _f("Qdrant 集合名", placeholder="imgbed_images"),
    "vector_search.embed_base_url": _f("向量 AI 地址", placeholder="https://api.example.com/v1"),
    "vector_search.embed_api_key": _f("向量 AI API Key", placeholder="网关未鉴权时留空"),
    "vector_search.embed_model": _f("多模态向量模型", placeholder="Qwen/Qwen3-VL-Embedding-8B"),
    "vector_search.similarity_threshold": _f("向量命中阈值", unit="比例", min=0, max=1, step=0.01),
    "vector_search.duplicate_vector_threshold": _f(
        "重复图片阈值", unit="比例", min=0, max=1, step=0.001, advanced=True
    ),
    "vector_search.vector_index_on_register": _f("登记时写入向量库"),
    "vector_search.embed_image_input": _f("图片输入格式", advanced=True),
    "vector_search.embed_input_type": _f("NVIDIA input_type", advanced=True),
    "vector_search.top_k": _f("检索候选数", unit="个", min=1, max=50, step=1, advanced=True),
    "vector_search.request_timeout": _f(
        "请求超时", unit="秒", min=5, max=300, step=5, advanced=True
    ),
    # ---------------- limits ----------------
    "limits.max_download_mb": _f("单图下载上限", unit="MB", min=1, max=500, step=1, advanced=True),
    "limits.remote_hash_enabled": _f("图床哈希索引", advanced=True),
    "limits.remote_hash_ttl": _f(
        "哈希缓存有效期", unit="秒", min=60, max=604800, step=60, advanced=True
    ),
    "limits.remote_hash_max_points": _f(
        "哈希拉取上限", unit="点", min=0, max=1000000, step=100, advanced=True
    ),
    # ---------------- sns ----------------
    "sns.enabled": _f("启用多平台订阅", danger=True),
    "sns.fetch_mode": _f("取数方式", danger=True),
    "sns.browser_headed": _f("浏览器显示窗口"),
    "sns.browser_profile_dir": _f(
        "浏览器持久化目录", advanced=True, placeholder="留空 = 数据目录/sns/browser"
    ),
    "sns.aggressive": _f("激进档位", danger=True),
    "sns.base_interval_douyin": _f("抖音轮询间隔", unit="秒", min=300, max=86400, step=60),
    "sns.base_interval_xhs": _f("小红书轮询间隔", unit="秒", min=300, max=86400, step=60),
    "sns.base_interval_instagram": _f("Instagram 轮询间隔", unit="秒", min=900, max=86400, step=60),
    "sns.jitter_ratio": _f("额外抖动比例", unit="比例", min=0, max=1, step=0.01, advanced=True),
    "sns.platform_concurrency": _f(
        "平台并发上限", unit="并发", min=0, max=10, step=1, advanced=True
    ),
    "sns.cooldown_base_seconds": _f("风控冷却基数", unit="秒", min=60, max=86400, step=60),
    "sns.cooldown_cap_seconds": _f("风控冷却上限", unit="秒", min=60, max=86400, step=60),
    "sns.health_pause_threshold": _f("健康暂停阈值", unit="比例", min=0, max=1, step=0.01),
    "sns.health_degrade_threshold": _f("健康降速阈值", unit="比例", min=0, max=1, step=0.01),
    "sns.warmup_minutes": _f("新订阅热身期", unit="分钟", min=0, max=1440, step=1, advanced=True),
    "sns.protect_mode_hours": _f("保号期", unit="小时", min=0, max=720, step=1, advanced=True),
    "sns.night_slowdown": _f("夜间间隔翻倍"),
    "sns.push_sessions": _f("订阅推送目标", managed=True, advanced=True),
    "sns.message_format": _f("订阅推送模板", multiline=True),
    "sns.message_with_images": _f("推送附带图片"),
    "sns.max_images": _f("单条最多图片", unit="张", min=1, max=20, step=1),
    "sns.text_max_length": _f("正文截断长度", unit="字符", min=1, max=2000, step=1),
    "sns.at_all": _f("订阅推送 @全体", danger=True),
    "sns.at_all_keywords": _f("@全体关键词", managed=True, advanced=True),
    "sns.at_all_uids": _f("@全体账号白名单", managed=True, advanced=True),
    "sns.douyin_cookie": _f("抖音 Cookie", placeholder="http 模式必填"),
    "sns.xhs_cookie": _f("小红书 Cookie", placeholder="http 模式必填"),
    "sns.instagram_cookie": _f("Instagram Cookie", placeholder="http 模式必填"),
}

# 枚举值的中文标签；没列出的值原样显示。值按字符串匹配，写回时仍按 schema 类型。
OPTION_LABELS: dict[str, dict[str, str]] = {
    "basic.search_engine": {
        "auto": "自动（向量优先，失败回退哈希）",
        "vector": "仅向量库",
        "hash": "仅本地哈希",
    },
    "push.video_quality": {"hd": "高清（hd）", "sd": "标清（sd）"},
    "delivery.napcat_mode": {
        "auto": "自动（推荐）",
        "native": "原生直传",
        "bytes": "本机处理后上传",
        "url": "直链原图",
        "url-scaled": "直链缩放版",
    },
    "delivery.qq_mode": {
        "auto": "自动（推荐）",
        "native": "原生直传",
        "bytes": "本机处理后上传",
        "url": "直链原图",
        "url-scaled": "直链缩放版",
    },
    "delivery.napcat_scaled_url_style": {
        "query": "查询参数式",
        "cf-path": "Cloudflare 路径式",
    },
    "delivery.qq_auto_prefer": {"url": "直链优先", "bytes": "本地分片优先"},
    "delivery.qq_on_failure": {"fallback": "回退原生通道", "notify": "只提示文字"},
    "image_bed.mode": {
        "local": "仅本地副本",
        "generic_http": "通用 HTTP 图床",
        "cloudflare_imgbed": "CloudFlare-ImgBed",
    },
    "storage_bucket.mode": {
        "none": "不使用储存桶",
        "cloudflare_r2": "Cloudflare R2",
        "oracle_oci": "Oracle OCI",
    },
    "checkin.mode": {"quote": "随机语句", "weibo": "微博归档文案"},
    "checkin.render_mode": {"card": "合成卡片", "simple": "缩略图 + 文字"},
    "checkin.quote_source": {
        "builtin": "仅本地语句库",
        "api": "仅在线接口",
        "mixed": "本地 + 在线混合",
    },
    "vector_search.embed_image_input": {
        "nemotron-vl": "NVIDIA Nemotron VL",
        "qwen-vl": "Qwen VL",
        "dataurl": "Data URL",
        "jina-image": "Jina Image",
    },
    "vector_search.embed_input_type": {
        "passage": "passage（图片侧）",
        "query": "query（文本侧）",
    },
    "sns.fetch_mode": {
        "browser": "本机浏览器（推荐）",
        "http": "HTTP + Cookie",
        "rss": "RSS 兜底",
    },
}

# schema 里没有 options、但语义上必须限定枚举的字段（面板侧补下拉）。
EXTRA_OPTIONS: dict[str, tuple[Any, ...]] = {
    "sns.fetch_mode": ("browser", "http", "rss"),
}

HASH_SIZE_LABELS = {
    4: "4 × 4（16 bit）",
    8: "8 × 8（64 bit）",
    12: "12 × 12（144 bit）",
    16: "16 × 16（256 bit，默认）",
    20: "20 × 20（400 bit）",
    24: "24 × 24（576 bit）",
    28: "28 × 28（784 bit）",
    32: "32 × 32（1024 bit）",
}

_PREFIX_RE = re.compile(r"^【([^】]+)】\s*")


def load_schema(path: Path | None = None) -> dict[str, Any]:
    target = path or SCHEMA_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{target} 的根节点必须是对象")
    return data


def visible_schema_groups(schema: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """返回 15 个可见分组（object 且有 items，且没有 invisible 标记）。

    旧布局的顶层镜像键是标量或 dict，不在此列；`invisible` 的隐藏镜像也不在
    此列。这样设置页只处理真正面向用户的 179 个分组键。
    """
    data = schema if schema is not None else load_schema()
    groups = {
        name: node
        for name, node in data.items()
        if isinstance(node, dict)
        and node.get("type") == "object"
        and not node.get("invisible")
        and isinstance(node.get("items"), dict)
    }
    return groups


def _split_description(text: str) -> tuple[str, str]:
    match = _PREFIX_RE.match(text)
    if not match:
        return text.strip(), ""
    return text[match.end() :].strip(), match.group(1).strip()


def _option_entries(key: str, node: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    raw = node.get("options")
    if raw is None:
        raw = EXTRA_OPTIONS.get(key)
    if not raw:
        return []
    labels = OPTION_LABELS.get(key, {})
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            value, label = item
        else:
            value = item
            if key == "basic.hash_size":
                label = HASH_SIZE_LABELS.get(int(value), str(value))
            else:
                label = labels.get(str(value), str(value))
        entries.append({"value": value, "label": str(label)})
    return entries


def _widget(node: dict[str, Any], meta: dict[str, Any], options: list[dict[str, Any]]) -> str:
    node_type = str(node.get("type") or "")
    if node_type == "bool":
        return "switch"
    if node.get("secret") and node_type == "string":
        return "password"
    if options:
        return "select"
    if node_type in ("int", "float"):
        return "number"
    if node_type in ("object", "dict"):
        return "json"
    if node_type == "list":
        return "list"
    if node_type == "text" or meta.get("multiline"):
        return "textarea"
    return "text"


def _condition(
    group: str,
    node: dict[str, Any],
    sibling_nodes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    raw = node.get("condition")
    if not isinstance(raw, dict):
        return []
    entries: list[dict[str, Any]] = []
    for field_name, expected in raw.items():
        sibling = sibling_nodes.get(str(field_name))
        field_key = f"{group}.{field_name}"
        meta = FIELD_META.get(field_key) or {}
        label = meta.get("label") or str(field_name)
        value_label = str(expected)
        if sibling is not None:
            for entry in _option_entries(field_key, sibling, meta):
                if entry["value"] == expected:
                    value_label = entry["label"]
                    break
        entries.append(
            {
                "field": field_key,
                "value": expected,
                "label": f"{label} = {value_label}",
            }
        )
    return entries


def build_catalog(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造设置页目录；缺标签/缺归类/未知键时直接抛错。"""
    data = schema if schema is not None else load_schema()
    visible = visible_schema_groups(data)

    known_groups: list[str] = []
    seen_keys: set[str] = set()
    pages: list[dict[str, Any]] = []

    for page in PAGES:
        page_groups: list[dict[str, Any]] = []
        for group_id in page["groups"]:
            if group_id not in visible:
                raise ValueError(f"页面 {page['id']} 引用了不可见的 schema 分组: {group_id}")
            if group_id in known_groups:
                raise ValueError(f"schema 分组被重复归类: {group_id}")
            known_groups.append(group_id)
            node = visible[group_id]
            items = node.get("items") or {}
            group_meta = GROUP_META.get(group_id)
            if group_meta is None:
                raise ValueError(f"schema 分组缺少展示元数据: {group_id}")
            fields: list[dict[str, Any]] = []
            for field_name, field_node in items.items():
                key = f"{group_id}.{field_name}"
                if key in seen_keys:
                    raise ValueError(f"配置键被重复归类: {key}")
                seen_keys.add(key)
                meta = FIELD_META.get(key)
                if meta is None:
                    raise ValueError(f"配置键缺少面板元数据（短标签）: {key}")
                description, requirement = _split_description(
                    str(field_node.get("description") or "")
                )
                options = _option_entries(key, field_node, meta)
                fields.append(
                    {
                        "key": key,
                        "group": group_id,
                        "field": field_name,
                        "label": meta["label"],
                        "description": description,
                        "requirement": requirement,
                        "type": field_node.get("type"),
                        "widget": _widget(field_node, meta, options),
                        "default": copy.deepcopy(field_node.get("default")),
                        "secret": bool(field_node.get("secret")),
                        "writable": True,
                        "options": options,
                        "min": meta.get("min"),
                        "max": meta.get("max"),
                        "step": meta.get("step"),
                        "unit": meta.get("unit") or "",
                        "danger": bool(meta.get("danger")),
                        "advanced": bool(meta.get("advanced") or field_node.get("collapsed")),
                        "managed": bool(meta.get("managed")),
                        "plaintext": bool(meta.get("plaintext")),
                        "multiline": bool(meta.get("multiline")),
                        "placeholder": str(meta.get("placeholder") or ""),
                        "condition": _condition(group_id, field_node, items),
                    }
                )
            page_groups.append(
                {
                    "id": group_id,
                    "title": group_meta["title"],
                    "summary": group_meta["summary"],
                    "accent": group_meta.get("accent", ""),
                    "fields": fields,
                }
            )
        pages.append(
            {
                "id": page["id"],
                "title": page["title"],
                "summary": page["summary"],
                "about": bool(page.get("about")),
                "groups": page_groups,
            }
        )

    missing_groups = sorted(set(visible) - set(known_groups))
    if missing_groups:
        raise ValueError(f"这些 schema 分组没有归入任何主题页: {missing_groups}")

    total = sum(len(group["fields"]) for page in pages for group in page["groups"])
    return {
        "pages": pages,
        "counts": {
            "pages": len([page for page in pages if not page["about"]]),
            "groups": len(known_groups),
            "keys": total,
            "writable": total,
            "secrets": sum(
                1
                for page in pages
                for group in page["groups"]
                for field in group["fields"]
                if field["secret"]
            ),
        },
    }


def iter_fields(catalog: dict[str, Any]):
    """按页面顺序遍历所有字段。"""
    for page in catalog["pages"]:
        for group in page["groups"]:
            for field in group["fields"]:
                yield page, group, field


__all__ = [
    "FIELD_META",
    "GROUP_META",
    "PAGES",
    "SCHEMA_PATH",
    "build_catalog",
    "iter_fields",
    "load_schema",
    "visible_schema_groups",
]
