"""微博监控、推送与相册域的常量。"""

from __future__ import annotations

import re
from datetime import timedelta, timezone
from urllib.parse import quote

PLUGIN_NAME = "astrbot_plugin_weibo_suite"
PLUGIN_VERSION = "v0.8.1"

WEIBO_HOME_URL = "https://m.weibo.cn/"
WEIBO_INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
WEIBO_EXTEND_URL = "https://m.weibo.cn/statuses/extend"
WEIBO_SHOW_URL = "https://m.weibo.cn/statuses/show"
WEIBO_VISITOR_HOST = "https://visitor.passport.weibo.cn"
WEIBO_GENVISITOR_URL = f"{WEIBO_VISITOR_HOST}/visitor/genvisitor"
WEIBO_INCARNATE_URL = f"{WEIBO_VISITOR_HOST}/visitor/visitor"
# 游客系统入口页（genvisitor/incarnate 的 Referer）
WEIBO_VISITOR_REFERER = (
    f"{WEIBO_VISITOR_HOST}/visitor/visitor?entry=sinawap&a=enter&url="
    f"{quote(WEIBO_HOME_URL, safe='')}&domain=.weibo.cn&sudaref="
    "&ua=php-sso_sdk_client-0.6.36&_rand=1791030900.1"
)

# 出网白名单：微博时间线接口 + 微博游客身份系统 + 长文全文接口（weibo.com）。
# weibo.com 只为归档长文首选接口（archive.py 的 ARCHIVE_LONGTEXT_URL）开通：
# 之前漏了它，该接口必然被 _validate_url 拒绝、又被 debug 日志吞掉，
# 等于永久静默失效。放开 host 不等于放松防 SSRF——_resolve_host +
# _ensure_public_addrs 仍然逐跳校验解析结果必须是公网地址。
WEIBO_ALLOWED_HOSTS = {"m.weibo.cn", "visitor.passport.weibo.cn", "weibo.com"}

# 相册/视频媒体下载白名单（与 API 白名单分离）：图片 CDN + 视频段/视频 CDN。
# .sina.com.cn 覆盖旧版视频 CDN（*.video.sina.com.cn）
MEDIA_ALLOWED_SUFFIXES = (
    ".sinaimg.cn",
    ".weibocdn.com",
    ".weibocdn.me",
    ".weibo.com",
    ".weibo.cn",
    ".sina.com.cn",
)

# UA 池：游客身份与 UA 绑定（一套身份一套指纹），全部为移动端 UA。
# 第一个为实测验证过的 UA。
UA_POOL = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
)
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.weibo.cn/",
    "MWeibo-Pwa": "1",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_MESSAGE_FORMAT = "📢 微博更新\n【{name}】{time}\n{weibo}\n🔗 {link}"

SEEN_IDS_LIMIT = 500
PENDING_HARD_LIMIT = 100
MIN_INTERVAL_SECONDS = 30
# 批量推送期间 state.json 的落盘合并间隔（秒）：state.json 是全量序列化
# （seen_ids×账号数 + 动态 + 队列），逐条落盘是纯写放大。首批照常立即落盘，
# 崩溃安全窗口最多放宽一个间隔
STATE_SAVE_DEBOUNCE = 10.0
# 时间线翻页补漏上限（共 3 页 = 60 条）：两次检查之间积压超过一页时向后翻，
# 再多说明轮询配置与博主的发博频率严重不匹配，翻页也补不完，交给时效过滤
TIMELINE_MAX_PAGES = 3
# show_full_weibo_text 开启时全文的安全上限（防超长文本刷屏/QQ 风控）
FULL_TEXT_MAX_CHARS = 2000
VISITOR_MAX_AGE = 3 * 24 * 3600  # 游客身份最长复用 3 天，到期主动换新
RENEW_COOLDOWN = 600.0  # 主动续期最小间隔（秒）：频繁 genvisitor 本身是风控信号
RENEW_MAX_PER_HOUR = 3  # 每小时最多续期尝试次数
RENEW_FAIL_BACKOFF = 600.0  # 续期失败退避基数（秒），指数递增，封顶 1 小时
REQUEST_GAP = (2.0, 5.0)  # 相邻两次微博请求的最小随机间隔（秒）
# 媒体 CDN 专用的小间隔（秒）。媒体走的是 sinaimg / weibocdn 这类独立 CDN，
# 与 m.weibo.cn 的接口风控不是一条线：套用 REQUEST_GAP(2~5s) 会把 9~18 张图的
# 一批下载串行拖到分钟级，相册任务直接超时。但"完全不限速 + 并发 5 路"对 CDN
# 也不礼貌，所以取一个很小的间隙来削峰会免得瞬时几十个请求同时打出去。
# 归档原图下载同样走这里。
MEDIA_REQUEST_GAP = 0.2
# 出网校验用 DNS 解析缓存时长（秒）：白名单 host 只有寥寥几个且极少变更，
# 而每个接口请求、每张图片下载前都要 getaddrinfo 一遍
DNS_CACHE_TTL = 300.0
# HTTP 418/432 属于 IP 级风控：全局冷却梯度（秒），触发后暂停所有微博请求
RISK_COOLDOWN_STEPS = (600, 1800, 3600)
# 距上次风控触发超过该时长后冷却梯度重置，避免历史偶发触发导致永久 60 分钟冷却
RISK_ESCALATION_RESET = 2 * 3600

# ---------------- 群相册自动上传 ----------------
# 单条微博最多处理的图片数（消息带图与相册上传共用上限）：微博单条上限
# 就是 18 图（2019-09 起对邀请账号放开、后逐步扩面），默认全量处理不丢图；
# 想少下少传（省流量/防刷屏）在配置 album_max_images 调小
DEFAULT_MAX_IMAGES = 18
# v1.11.x 及以前 DEFAULT_MAX_IMAGES 是 9：AstrBot 把当时的默认值物化进部署
# 配置后，升级只补缺键、不改已存值，旧部署里的 9 会一直把 18 图帖切回 9 张。
# 存量配置的一次性迁移以它为判定特征，见 _migrate_album_max_images
OLD_ALBUM_MAX_IMAGES = 9
IMG_MAX_BYTES = 30 * 1024 * 1024  # 单张图片/实况图视频段的下载体积上限
DISK_FLOOR_MB = 1024  # 批次下载前要求临时目录所在盘至少剩余空间（MB），防写满磁盘
ALBUM_TMP_DIR = "album_tmp"  # 相册上传的临时下载目录（插件数据目录下），传完即删
ALBUM_RESOLVE_TTL = 3600.0  # (群, 相册) 解析结果的内存缓存时长（秒）
ALBUM_NAME_SCAN_TTL = 600.0  # 同机相册文件名兜底扫描的缓存时长（秒）
LEDGER_TTL = 30 * 86400  # "这张图传过"台账保留时长（与上游相册插件口径一致）
LEDGER_MAX = 2000  # 每个相册最多记多少条台账
BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\s]+')
# 诊断文件：每批相册下载/上传在重负载动作"之前"写入体量与配置快照并立即刷盘。
# 整机被内存顶死时主日志往往来不及落盘，这份文件重启后还能还原死机前最后几批
# 的状态（与 astrbot_plugin_weibo_album 的 diagnostic.log 同一套做法）
DIAG_FILE = "diagnostic.log"
DIAG_MAX_BYTES = 512 * 1024  # 诊断文件轮转阈值：超限只留后半段
# 错误留存：本插件所有 warning/error 留存进内存环形缓冲 + 后台线程写盘，
# 面板可查。整机被内存顶死时主日志来不及刷盘，诊断文件只覆盖相册重负载动作，
# 这份文件补上"死机前最后在报什么告警"（发送超时、检查失败、风控冷却……）
ERRLOG_FILE = "errors.log"
ERRLOG_MAX_BYTES = 512 * 1024  # 轮转阈值：超限只留后半段
# ERRLOG_BUFFER / ERRLOG_PAGE_LIMIT 与面板共享，见 constants.py
GIF_MAX_BYTES = 30 * 1024 * 1024  # 实况图 GIF 产物上限：超限回落封面静图，防超大 GIF
SINAIMG_RE = re.compile(
    r"(https?:)?//([a-z0-9]+)\.sinaimg\.cn/([a-z0-9]+)/([0-9a-zA-Z]+)\.(\w+)", re.I
)

# ---------------- 视频转发 ----------------
VIDEO_TMP_DIR = "video_tmp"  # 视频推送的临时下载目录（插件数据目录下），发完即删
VIDEO_MAX_MB_DEFAULT = 95  # 单视频下载/发送体积上限默认值：QQ 视频消息硬限 100MB，留出余量
# base64 内嵌发送的体积上限默认值：base64 一路上是文件体积的 5~6 倍瞬时内存
# （读入字节 + 编码串 + 载荷串 + JSON 序列化，插件与协议端两侧各来一遍），
# 95MB 的视频就是 500MB 级的内存尖峰，小内存服务器直接被顶死（整机无响应、
# 之后超时重发再撞上重复推送，都是它的连锁反应）。30MB 约对应 160MB 级尖峰，
# 是 1~2GB 内存机器能安全消化的量级
VIDEO_INLINE_MB_DEFAULT = 30
VIDEO_DL_TIMEOUT = 300.0  # 单个视频下载总超时（秒）：几十上百 MB 的文件比图片慢得多
# 含视频消息的发送超时下限：视频上传 QQ 比九张图还慢，沿用图片的 60s 常常
# "实际已发出但被判超时"，重试会让群里重复出现同一条视频
VIDEO_SEND_TIMEOUT_FLOOR = 180
# 含图片消息的发送超时放大：消息里的图片以 URL 交给协议端自行抓取再走富媒体
# 上传，NapCat 要把每张图处理完才回回执，多图串行的总耗时随张数线性涨。协议端
# 富媒体通道劣化时单张十几秒，多图 plog 轻松顶穿 60s——WS 帧早已交出去，群里
# 实际收到了，插件却按超时记"放弃"（2026-10-10 线上实录：多图帖 60s 超时、群里
# 可见）。视频有固定下限、图片按张数放大：基础 60s + 每张 20s，九图放大到 240s；
# 发得快时该返回就返回，放大只抬"等多久才判超时"的上限
PIC_SEND_TIMEOUT_BASE = 60
PIC_SEND_TIMEOUT_PER_PIC = 20
VIDEO_FALLBACK_NOTE = "🎬 视频未能自动转发，请点原帖链接观看"
VIDEO_PARTIAL_NOTE = "🎬 部分视频未能自动转发，请点原帖链接观看"
# 视频消息被协议端拒收后改发文件成功时的说明：视频确实进群了（点文件消息即可
# 接收），只是不是"点开就播"的视频消息，措辞要跟降级提示区分开。
# 两种落点分开写：临时/在线文件不会出现在群文件列表里，说成"群文件"会让成员去
# 群里找不到；落进群文件那些部署才用"群文件"那句。
VIDEO_FILE_TEMP_NOTE = "🎬 视频已改以文件消息发送（临时文件，请及时接收或转存，超时可能失效）"
VIDEO_FILE_SENT_NOTE = "🎬 视频已改以群文件发送，可在群文件里下载或在线播放"
# video_prefer_file（配置直选文件，不是被拒收降级）专用的两句："已改以"说的是
# 半路降级，主动直选文件没有"改"的过程，措辞不撒谎
VIDEO_FILE_FIRST_TEMP_NOTE = "🎬 视频以文件消息发送（临时文件，请及时接收或转存，超时可能失效）"
VIDEO_FILE_FIRST_SENT_NOTE = "🎬 视频以群文件发送，可在群文件里下载或在线播放"
VIDEO_FILE_UPLOAD_TIMEOUT = 180.0  # 文件上传总超时（秒）：几十 MB 的 base64 比发消息还慢
# 微博视频直链的签名有效窗实测约 3600 秒：留 10 分钟余量，条目里挂过窗的直链
# 先重取再下载（拿旧链去下必然 403，白烧一轮还把这条视频永久判死）
VIDEO_URL_FRESH_SECONDS = 3000.0
# 一条微博的视频最多下载几轮，耗尽才落"未能自动转发"提示。轮与轮之间隔一个
# 轮询间隔，且每轮都重取直链：实测 CDN 对同一连接的连续请求偶尔直接回 403，
# 稍后再请求同一条链又是 200——一次失败就判死等于永久放弃这条视频
VIDEO_FETCH_MAX_ATTEMPTS = 3
PIC_REJECTED_NOTE = "🖼 图片超出 QQ 收发限制未能发出，原图请看原帖链接"
# QQNT 对像素尺寸/体积超限的富媒体直接拒收整条消息（NapCatQQ#1443），回的是
# retcode 1200 + 这句 errMsg。按文本判而不是按码判：1200 是 WS 通道的通用失败码，
# 参数错、下载失败也走它，只有文案能区分"这条重试一万次也不会成"。图片和视频段
# 都撞这个回执，所以摘段要按嫌疑顺序升级（见 _strip_rejected_segment）
RICH_MEDIA_REJECT_MARKER = "rich media transfer failed"
# 拒收回执留档：只留能定性的三样字段，不是把整段 ActionFailed 灌进 state.json 与面板。
# 现场判"是体积超限、NapCat 版本回归，还是风控"全靠它——2026-10-09 那条 5.6MB 的
# 视频被判"富媒体超限"就是这么发现的：回执里根本没有体积信息，结论是猜的。
REJECT_DETAIL_MAX = 200
_REJECT_RETCODE_RE = re.compile(r"retcode=(\d+)")
_REJECT_RESULT_RE = re.compile(r'"result"\s*:\s*(-?\d+)')
_REJECT_ERRMSG_RE = re.compile(r'"errMsg"\s*:\s*"([^"]{0,80})"')

# ---------------- WebUI 控制面板（dashboard.py 提供页面与 API） ----------------
ACTIVITY_MAX = 200  # 最近动态最多保留条数（随 state.json 持久化）
ACTIVITY_TEXT_MAX = 80  # 单条动态摘要截断长度，防 state.json 膨胀
# 动态说明（detail）另给一份更宽的额度：拒收回执、降级来源这类诊断文字要连
# 字段名一起说完整才读得懂，按 80 截到一半等于没记。text 仍按 80 截，那是微博
# 正文摘要，短才对。
ACTIVITY_DETAIL_MAX = 240
STATS_DAILY_KEEP = 14  # 按天推送统计桶保留天数

# ---------------- 面板手动取消推送 ----------------
# 推送一直失败（协议端拒收、消息发不出去、群里那条就是不要了）时，等 max_pending_retries
# 烧完最长要 10 分钟以上，期间面板没有任何"就到此为止"的手段。取消分两级：
#   · 单条取消：把这一条从待推送队列摘出去，不再重发；
#   · 停止本轮：本轮 _flush_pending 立即收尾，剩余条目原样留队、不消耗重试次数。
# 取消是误点成本很低却代价不小的操作（点了就再也没有那条微博进群），所以单条取消
# 进"可撤销保留区"，PUSH_CANCEL_UNDO_SECONDS 内可在面板一键放回队列。
PUSH_CANCEL_UNDO_SECONDS = 1800.0  # 取消后可撤销窗口（秒）
# 保留区最多留几条：窗口内取消很多条时防 state.json 无上限膨胀，超出只淘汰最旧的
PUSH_CANCEL_KEEP = 20

# ---------------- @全体成员的生效条件（权限 / 频次 / 回读确认） ----------------
# QQ 群的 @全体 不是"发了就算"：发送者必须是群主或管理员，且账号级有频次上限
# （据 QQ 会员说明：非超级会员的管理员 10 次/天、超级会员 20 次/天，**所有群共享**）。
# 条件不满足时 QQ 往往不报错，而是把 at 段降级成一条纯文本 "@全体成员"——群里
# 看着像 @ 了，实际谁都收不到提醒，插件这边却记成"推送成功"。所以链首插 AtAll
# 之前先过一道闸门（_at_all_gate），发出去之后再回读确认（_verify_at_all）。
AT_ALL_QUOTA_DEFAULT = 10  # 24 小时内 @全体 次数上限的默认值（0 = 不限制）
AT_ALL_QUOTA_WINDOW = 86400.0  # 频次窗口（秒）：按 24 小时滚动计，比"自然日"保守
AT_ALL_QUOTA_MAX_KEEP = 200  # 频次台账最多留多少个时间戳，防 state.json 膨胀
AT_ALL_ROLE_TTL = 600.0  # 机器人群角色的缓存时长（秒）：角色极少变，不必每条推送都查
AT_ALL_VERIFY_TRIES = 2  # 回读确认的尝试次数（协议端落库有秒级延迟）
AT_ALL_VERIFY_GAP = 0.8  # 两次回读之间的间隔（秒）
AT_ALL_VERIFY_COUNT = 5  # 回读时拉最近几条群消息（要能覆盖多个群同时推送）
AT_ALL_NOTICE_TTL = 1800.0  # 同一条"没 @ 的原因"多久内不重复记日志/动态（防刷屏）
# 权限/回读这类"为了确认一件事"的接口调用必须有上限：aiocqhttp 的 API 超时默认
# 180 秒，协议端一旦卡住，一次查角色就能把整轮推送拖住三分钟——比不发 @全体 糟得多
AT_ALL_API_TIMEOUT = 8.0
AT_ALL_ROLE_NAMES = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

# ---- 归档（P3）：库文件与媒体根都落在 data_dir 下 ----
ARCHIVE_DB_NAME = "archive.db"
ARCHIVE_DIR_NAME = "archive"
# 原图下载队列上限：归档是旁路，永远不能反压推送/回填
ARCHIVE_QUEUE_MAX = 500
# 队列满时转存待办的条数上限：不能因为"满了"就把 mid 直接丢掉（那会让 media
# 永远停在 pending，只有对账才看得出来）。满了丢最旧的，等 worker 空闲重投。
ARCHIVE_PENDING_RETRY_MAX = 500
# 回填翻页安全上限：异常数据（如游标不前进）时也必须能停下来
BACKFILL_SAFETY_MAX_PAGES = 1000
# 媒体状态顺序即推进等级（rank 0..N），DB 层据此保证状态单调不回退
MEDIA_STATES = ("pending", "downloaded", "album", "bed", "thumb", "vector")

# ---- 图床/向量管线（P4）：队列上限与缩略图约定 ----
# 与归档队列分开：归档只搬字节，管线要跑图床/embed，彼此不能互相反压
PIPELINE_QUEUE_MAX = 500
# 缩略图目录：与 img-indexer 现网一致（Qdrant payload.thumb_url 指向这里）
THUMBNAIL_DIR_NAME = "thumbnails"
PIPELINE_STAGE_DIR_NAME = "pipeline_stage"

# ---- 文案库（P5/F3）：archive.db 即文案库，CSV/MD 只是导入导出通道 ----
COPYLIB_DIR_NAME = "copylib"
COPYLIB_LIST_LIMIT = 10
COPYLIB_CSV_HEADER = ("序号", "发布时间", "来源", "文案", "微博链接")

_MONTH_MAP = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_TZ_CN = timezone(timedelta(hours=8))
_STD_TIME_RE = re.compile(r"^(\w{3}) (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) \+0800 (\d{4})$")
_JSONP_RE = re.compile(r"\((\{.*\})\)\s*;?\s*$")
_UID_PATTERNS = (
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.com/(\d+)/?"),
)

USAGE = (
    "微博监控指令：\n"
    "微博绑定 - 把当前群绑定为推送目标（管理员）\n"
    "微博解绑 - 解除当前群推送（管理员）\n"
    "微博添加 <uid或主页链接> - 添加监控账号（管理员）\n"
    "微博删除 <uid> - 取消监控（管理员）\n"
    "微博列表 - 查看监控账号与推送目标\n"
    "微博相册绑定 <uid> <相册名或ID> - 该博主的微博图片自动上传到本群相册（管理员）\n"
    "微博相册移除 <uid> - 解除博主的相册绑定（管理员）\n"
    "微博相册列表 - 查看所有相册绑定规则\n"
    "微博列相册 - 查看本群的相册列表（管理员）\n"
    "微博关键词相册 <关键词> <群号> <相册名或ID> - 命中关键词的微博图片"
    "只传固定群相册（管理员，全局规则；群内可省群号）\n"
    "微博关键词相册移除 <关键词> - 删除同名全局关键词规则（管理员）\n"
    "微博博主关键词相册 <uid> <关键词> <群号> <相册名或ID> - 关键词规则"
    "只对该博主生效，优先于全局（管理员；群内可省群号）\n"
    "微博博主关键词相册移除 <uid> <关键词> - 删除该博主的关键词规则（管理员）\n"
    "微博关键词相册列表 - 查看关键词路由规则\n"
    "微博检测 - 立即检查一轮（管理员）\n"
    "微博状态 - 查看运行状态\n"
    "微博回填 <uid> [--since YYYY-MM-DD] [--full] - 全量回填该博主历史微博（管理员）\n"
    "微博回填状态 - 查看回填进度、统计与游标\n"
    "微博回填停止 - 中止当前回填（管理员，游标保留可续跑）\n"
    "微博归档导入 <文件路径> - 导入 JSON 种子文件到归档库（管理员）\n"
    "微博归档查询 [uid] [页码] - 查看归档微博（每页 10 条）\n"
    "微博对账 [uid] - 核对归档完整性（管理员）\n"
    "微博管线状态 - 查看图床/向量管线的队列与六态进度\n"
    "微博管线补跑 - 把归档库里没跑完图床/向量的帖子重新入队（管理员）\n"
    "微博文案库 [uid] - 查看文案库清洗进度与账号分布\n"
    "微博文案列表 [uid] [关键词] - 查看文案（默认最近 10 条）\n"
    "微博文案清洗 [uid] [条数] - 补齐未清洗的文案（管理员）\n"
    "微博文案导出 [uid] [csv|md] - 导出文案到 copylib/（管理员）\n"
    "微博文案导入 <CSV 文件路径> [uid] - 把 Node 版 CSV 文案导入归档库（管理员）\n"
    "WebUI 控制面板：AstrBot 管理面板 - 插件 - 本插件详情页打开"
)

__all__ = [name for name in globals() if not name.startswith("__")]
