"""多平台订阅域的常量与风控信号表。

风控相关的每一个数字都只在这里定义一份：服务、调度器、守卫、测试全部引用
同一份常量，改节奏时不会出现"配置里改了 300、代码里还硬编码 180"这种漂移。

依据（研究结论，写在这里是为了让后来人知道数字不是拍的）：

- 抖音：`astrbot_plugin_douyin_subscribe` 官方 README "抖音有风控，不建议低于
  300"，单轮并发"不建议超过 3"；`Douyin_TikTok_Download_API` 的 identities 文档
  要求"身份 = cookie + 指纹 + 出口 IP，三者绑定后永不换绑"，且
  `network_error` 不计入风控连击。
- 小红书：RSSHub 与 `astrbot_plugin_parser` 都靠 SSR 页取数，撞风控的表现是
  `.fe-verify-box`（验证码盒）、302 到 `/login`、`error_code=300031`。
- Instagram：`astrbot_plugin_instagram_monitor` README "轮询间隔建议 15~30 分钟"，
  429 要延长间隔/减少账号；`huvz04/astrbot_plugin_ins` 遇到 `checkpoint_required`
  只能人工在浏览器里过验证。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 平台

PLATFORMS: tuple[str, ...] = ("douyin", "xhs", "instagram")

PLATFORM_LABEL: dict[str, str] = {
    "douyin": "抖音",
    "xhs": "小红书",
    "instagram": "Instagram",
}

# 平台名 -> 指令里允许的别名（用户手打「订阅 抖音 ...」「订阅 ig ...」都能认）
PLATFORM_ALIAS: dict[str, str] = {
    "抖音": "douyin",
    "douyin": "douyin",
    "dy": "douyin",
    "小红书": "xhs",
    "xhs": "xhs",
    "红书": "xhs",
    "instagram": "instagram",
    "ins": "instagram",
    "ig": "instagram",
}

PLATFORM_HOME: dict[str, str] = {
    "douyin": "https://www.douyin.com/",
    "xhs": "https://www.xiaohongshu.com/",
    "instagram": "https://www.instagram.com/",
}

# 用户在持久化浏览器窗口里点一次登录就行；这里只负责把窗口带到正确的页面
PLATFORM_LOGIN_URL: dict[str, str] = {
    "douyin": "https://www.douyin.com/",
    "xhs": "https://www.xiaohongshu.com/explore",
    "instagram": "https://www.instagram.com/accounts/login/",
}

PLATFORM_ACCOUNT_URL: dict[str, str] = {
    # {account_id} 由适配器填：抖音是 sec_uid，小红书是 24 位 user_id，
    # Instagram 是用户名
    "douyin": "https://www.douyin.com/user/{account_id}",
    "xhs": "https://www.xiaohongshu.com/user/profile/{account_id}",
    "instagram": "https://www.instagram.com/{account_id}/",
}

# ---------------------------------------------------------------- 节奏

# 保守档：默认值。低于 MIN_BASE_INTERVAL 的配置会被夹到下限并在面板红字提示
BASE_INTERVAL_DEFAULT: dict[str, int] = {"douyin": 300, "xhs": 300, "instagram": 900}
MIN_BASE_INTERVAL: dict[str, int] = {"douyin": 300, "xhs": 300, "instagram": 900}
# 激进档：用户明确勾选才生效，面板上红字说明"更容易被风控"
AGGRESSIVE_BASE_INTERVAL: dict[str, int] = {"douyin": 180, "xhs": 180, "instagram": 600}

# 抖动比例：同一平台内每个账号的间隔在 base 上随机浮动，避免固定节奏被识别
JITTER_RATIO: dict[str, float] = {"douyin": 0.20, "xhs": 0.25, "instagram": 0.20}

# 平台级并发上限（同时最多几个账号在抓）
PLATFORM_CONCURRENCY: dict[str, int] = {"douyin": 2, "xhs": 1, "instagram": 1}

# 单账号并发恒为 1：同一身份同时打两个请求是最典型的风控特征
ACCOUNT_CONCURRENCY = 1

# 首屏只要这么多条：翻页越多越像爬虫，而且我们只需要"有没有新的"
FETCH_POST_LIMIT = 12

# 每个账号记住多少条已见 id（只用来去重，不受微博那边的 500 影响）
SEEN_IDS_LIMIT = 60

# 最近动态最多留多少条（落盘，面板展示用）
EVENTS_LIMIT = 200

# 账号健康度历史窗口（守卫算成功率/风控率用）
HISTORY_LIMIT = 60
HISTORY_OK_WINDOW = 900  # 15 分钟成功率
HISTORY_RISK_WINDOW = 3600  # 60 分钟风控率

# ---------------------------------------------------------------- 冷却

COOLDOWN_BASE_DEFAULT = 600
COOLDOWN_CAP_DEFAULT = 3600
# 账号健康度低于此值 -> 间隔 ×1.5；低于暂停阈值 -> 暂停该账号
HEALTH_DEGRADE_DEFAULT = 0.4
HEALTH_PAUSE_DEFAULT = 0.2
HEALTH_DEGRADE_FACTOR = 1.5

# 平台熔断：10 分钟内同一平台累计 3 次风控 -> 整个平台停 30 分钟
PLATFORM_TRIP_HITS = 3
PLATFORM_TRIP_WINDOW = 600
PLATFORM_PAUSE_SECONDS = 1800

# 新装/新加账号的预热期：间隔 ×2，一轮只检一个账号
WARMUP_MINUTES_DEFAULT = 30

# 保号模式：默认开启 24 小时，只建基线不推历史，间隔 ×2
PROTECT_MODE_HOURS_DEFAULT = 24

# 凌晨（0:00-8:00 本地时间）没人看，间隔放大，纯属省风控额度
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 8
NIGHT_FACTOR = 2.0

# 一轮最多推几条（防一次性补推把群刷屏）
MAX_PUSH_PER_ROUND = 3

# 投递补发：只补"明确失败"的（帖子, 会话）组合；unknown 视为可能已送达，重试会重复。
# 队列随状态文件落盘，重启不丢；超过 RETRY_QUEUE_LIMIT 时丢最旧的。
MAX_PUSH_RETRIES = 3
MAX_RETRY_PER_ROUND = 5
RETRY_QUEUE_LIMIT = 200

# 抓取超时：浏览器导航慢，给足；HTTP 模式短一些
BROWSER_TIMEOUT_MS = 45000
HTTP_TIMEOUT_SECONDS = 20

# 单条消息最多带几张图（超出只发前几张 + 链接）
DELIVER_MAX_IMAGES = 9

# ---------------------------------------------------------------- 结果与信号

OUTCOME_OK = "ok"
"""抓到了数据（可能是空列表，也可能是若干条新内容）。"""

OUTCOME_EMPTY = "empty"
"""明确拿到了空列表：正常状态，表示该账号没有可见内容。"""

OUTCOME_RISK = "risk"
"""撞风控：必须冷却，且绝不能当成"没有更新"。"""

OUTCOME_NETWORK = "network"
"""网络/超时类失败：计失败连击，但不计入风控连击（上游文档口径）。"""

OUTCOME_AUTH = "auth"
"""登录态失效：暂停该账号并提示用户重新登录，绝不自动重登。"""

OUTCOME_UNAVAILABLE = "unavailable"
"""执行环境不可用（没装 playwright、浏览器起不来、缺 Cookie 等）。"""

# 信号 -> (结果类别, 冷却权重, 面板文案)
SIGNALS: dict[str, tuple[str, float, str]] = {
    # ---- 抖音 ----
    "douyin.argus_blocked": (OUTCOME_RISK, 1.2, "被 ArgusSecurityPlugin 拦截（Uifid/签名不足）"),
    "douyin.empty_silent": (OUTCOME_RISK, 1.2, "返回 200 但内容为空（典型风控表现）"),
    "douyin.http_403": (OUTCOME_RISK, 1.0, "接口 403"),
    "douyin.http_429": (OUTCOME_RISK, 1.0, "接口 429 请求过于频繁"),
    "douyin.status_code_8": (OUTCOME_AUTH, 1.0, "status_code=8 用户未登录"),
    "douyin.captcha": (OUTCOME_RISK, 1.5, "验证码/滑块页面"),
    # ---- 小红书 ----
    "xhs.verify_box": (OUTCOME_RISK, 1.8, "出现风控校验盒 .fe-verify-box"),
    "xhs.redirect_login": (OUTCOME_AUTH, 1.0, "被重定向到登录页"),
    "xhs.error_300031": (OUTCOME_RISK, 1.4, "error_code=300031 笔记暂时无法浏览"),
    "xhs.http_406": (OUTCOME_RISK, 1.2, "接口 406 code=-1"),
    "xhs.empty_state": (OUTCOME_RISK, 1.0, "SSR 里没有 __INITIAL_STATE__（多为被降级）"),
    "xhs.no_user_notes": (OUTCOME_RISK, 1.0, "SSR 里缺少 user.notes 字段"),
    # ---- Instagram ----
    "ig.checkpoint": (OUTCOME_AUTH, 99.0, "checkpoint_required：必须人工在浏览器里过验证"),
    "ig.challenge": (OUTCOME_AUTH, 99.0, "challenge_required：必须人工过验证"),
    "ig.http_429": (OUTCOME_RISK, 1.0, "429 请求过于频繁"),
    "ig.require_login": (OUTCOME_AUTH, 1.0, "require_login：登录态失效"),
    "ig.http_401": (OUTCOME_AUTH, 1.0, "接口 401"),
    "ig.empty_edges": (OUTCOME_RISK, 0.8, "拿到页面但没有 media 字段"),
    # ---- 通用 ----
    "net.timeout": (OUTCOME_NETWORK, 0.0, "超时"),
    "net.error": (OUTCOME_NETWORK, 0.0, "网络错误"),
    "env.unavailable": (OUTCOME_UNAVAILABLE, 0.0, "执行环境不可用"),
    "env.not_logged_in": (OUTCOME_AUTH, 1.0, "尚未登录"),
    "env.unknown": (OUTCOME_NETWORK, 0.0, "未知错误"),
}


def signal_outcome(signal: str) -> str:
    """信号名 -> 结果类别；未登记的信号按网络错误处理（不冷却、只记失败）。"""
    return SIGNALS.get(signal, (OUTCOME_NETWORK, 0.0, signal))[0]


def signal_weight(signal: str) -> float:
    return SIGNALS.get(signal, (OUTCOME_NETWORK, 0.0, signal))[1]


def signal_label(signal: str) -> str:
    return SIGNALS.get(signal, (OUTCOME_NETWORK, 0.0, signal))[2]


# 明确"正常但没有内容"的信号（空列表本身不是错，只是这次没东西）
EMPTY_SIGNALS = frozenset({"ok.empty"})

# ---------------------------------------------------------------- 存储布局

# 全部落在 <data_dir>/sns/ 下：状态、凭据、浏览器 profile 各自分开，
# 这样"清空浏览器登录"和"删除订阅"是两件互不影响的事。
SNS_DIR_NAME = "sns"
SNS_STATE_FILE = "sns_state.json"
SNS_CREDENTIALS_FILE = "credentials.json"
SNS_BROWSER_DIR = "browser"
SNS_SESSIONS_FILE = "sessions.json"

# ---------------------------------------------------------------- 配置键

CFG_GROUP = "sns"

CFG_KEYS: dict[str, object] = {
    # 开关与模式
    "enabled": True,
    "fetch_mode": "browser",  # browser（M1，推荐）/ http（M2，需导出 Cookie）/ rss
    "browser_headed": True,  # 有头窗口：风控最低，也方便用户点登录
    "browser_profile_dir": "",  # 留空 = <data>/sns/browser
    "aggressive": False,  # 激进档：间隔更短，更容易被风控
    # 节奏
    "base_interval_douyin": BASE_INTERVAL_DEFAULT["douyin"],
    "base_interval_xhs": BASE_INTERVAL_DEFAULT["xhs"],
    "base_interval_instagram": BASE_INTERVAL_DEFAULT["instagram"],
    "jitter_ratio": 0.0,  # 0 = 用平台默认抖动
    "platform_concurrency": 0,  # 0 = 用平台默认并发
    # 冷却与保号
    "cooldown_base_seconds": COOLDOWN_BASE_DEFAULT,
    "cooldown_cap_seconds": COOLDOWN_CAP_DEFAULT,
    "health_pause_threshold": HEALTH_PAUSE_DEFAULT,
    "health_degrade_threshold": HEALTH_DEGRADE_DEFAULT,
    "warmup_minutes": WARMUP_MINUTES_DEFAULT,
    "protect_mode_hours": PROTECT_MODE_HOURS_DEFAULT,
    "night_slowdown": True,
    # 投递
    "push_sessions": [],  # 留空 = 复用微博域的 push_sessions
    "message_format": "📢 {platform}更新\n【{name}】{time}\n{text}\n🔗 {link}",
    "message_with_images": True,
    "max_images": DELIVER_MAX_IMAGES,
    "at_all": False,
    "at_all_keywords": [],
    # @全体生效的账号白名单（sec_uid / 昵称 / 平台名任一命中）；空 = 不限博主。
    # 注意：这是 sns 组自己的键，与顶层 push 组的 at_all_uids 不是同一份。
    "at_all_uids": [],
    "text_max_length": 300,
    # 凭据（M2 才用；M1 只记"已登录"状态，不落 Cookie）
    "douyin_cookie": "",
    "xhs_cookie": "",
    "instagram_cookie": "",
}

DEFAULT_MESSAGE_FORMAT = str(CFG_KEYS["message_format"])
