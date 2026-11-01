"""微博监控、推送与相册业务入口。"""

from __future__ import annotations

import weakref

from ..views_backend.dashboard import register_dashboard
from ..views_backend.sns_api import register_sns_dashboard
from .album import AlbumMixin
from .api import ApiMixin
from .archive import ArchiveMixin
from .archive_db import ArchiveStore
from .commands import CommandsMixin
from .copylib import CopylibMixin
from .monitor import MonitorMixin
from .pipeline import PipelineMixin
from .pusher import PusherMixin
from .runtime import *
from .visitor import VisitorMixin

# 旧平铺键 → (分组, 字段)。AstrBot 的配置面板按 schema 分组展示，而微博域
# 的业务代码一路以平铺键读写；启动时把分组值镜像到平铺键，运行时写入则两边
# 同步，避免"面板改了不生效"与"命令改了重启就丢"。
WEIBO_CFG_PATHS: dict[str, tuple[str, str]] = {
    "poll_interval": ("monitor", "poll_interval"),
    "monitored_uids": ("monitor", "monitored_uids"),
    "include_retweets": ("monitor", "include_retweets"),
    "filter_keywords": ("monitor", "filter_keywords"),
    "whitelist_keywords": ("monitor", "whitelist_keywords"),
    "max_post_age_minutes": ("monitor", "max_post_age_minutes"),
    "proxy": ("monitor", "proxy"),
    "custom_cookie": ("monitor", "custom_cookie"),
    "at_all": ("push", "at_all"),
    "message_format": ("push", "message_format"),
    "push_sessions": ("push", "push_sessions"),
    "push_delay_seconds": ("push", "push_delay_seconds"),
    "max_push_per_round": ("push", "max_push_per_round"),
    "max_pending_retries": ("push", "max_pending_retries"),
    "message_with_images": ("push", "message_with_images"),
    "message_with_videos": ("push", "message_with_videos"),
    "max_videos_per_post": ("push", "max_videos_per_post"),
    "message_send_timeout": ("push", "message_send_timeout"),
    "text_max_length": ("push", "text_max_length"),
    "show_full_weibo_text": ("push", "show_full_weibo_text"),
    "show_hashtags": ("push", "show_hashtags"),
    "weibo_emoji_translate": ("push", "weibo_emoji_translate"),
    "at_all_uids": ("push", "at_all_uids"),
    "at_all_keywords": ("push", "at_all_keywords"),
    "at_all_daily_limit": ("push", "at_all_daily_limit"),
    "at_all_check_permission": ("push", "at_all_check_permission"),
    "at_all_verify": ("push", "at_all_verify"),
    "callback_api_base": ("push", "callback_api_base"),
    "video_prefer_file": ("push", "video_prefer_file"),
    "video_quality": ("push", "video_quality"),
    "video_max_mb": ("push", "video_max_mb"),
    "video_inline_max_mb": ("push", "video_inline_max_mb"),
    "video_file_fallback": ("push", "video_file_fallback"),
    "video_file_temp": ("push", "video_file_temp"),
    "album_enabled": ("album", "album_enabled"),
    "album_rules": ("album", "album_rules"),
    "album_keyword_rules": ("album", "album_keyword_rules"),
    "album_max_images": ("album", "album_max_images"),
    "album_live_gif": ("album", "album_live_gif"),
    "album_same_host": ("album", "album_same_host"),
    "album_notify": ("album", "album_notify"),
    "album_download_concurrency": ("album", "album_download_concurrency"),
    "album_upload_concurrency": ("album", "album_upload_concurrency"),
    "album_upload_interval": ("album", "album_upload_interval"),
    "album_upload_payload_mb": ("album", "album_upload_payload_mb"),
}

WEIBO_LAYOUT_VERSION = "1"


def _resolve_data_dir() -> Path | None:
    """插件数据目录；取不到就返回 None，绝不用当前工作目录顶上。

    退化成 `Path.cwd()` 看着能用，实际会把导出文件、上传缓存写进 AstrBot
    安装目录（甚至插件源码树），既污染发行包又让"数据在哪"变得不可预测。
    调用方拿到 None 应显式报错降级（见 copylib._copylib_dir）。
    """
    try:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception as exc:  # 轻量测试桩/异常环境下不要阻断插件构造
        logger.debug(f"{PLUGIN_NAME} 数据目录解析失败（相关功能降级）: {exc!r}")
        return None


class WeiboMonitorPlugin(
    VisitorMixin,
    ApiMixin,
    MonitorMixin,
    PusherMixin,
    AlbumMixin,
    ArchiveMixin,
    CopylibMixin,
    PipelineMixin,
    CommandsMixin,
    Star,
):
    """免 Cookie 监控微博账号更新，并归档后转发到绑定 Q 群。"""

    def __init__(self, context: Context, config: dict | None = None):
        # config 必须可省略：star_manager 只在该插件的 data/config/<目录名>_config.json
        # 已存在时才构造 AstrBotConfig 传进来，新装/刚 clone/配置文件被删的场合走的是
        # star_cls_type(context=...) 这条只传 context 的路径。写成必填参数会直接
        # TypeError，WebUI 只显示"插件加载失败"且不给原因。
        super().__init__(context)
        # 保持传入对象的原样引用，不要 dict(config) 拷贝一份：加载器给的是
        # AstrBotConfig，面板改配置后要靠它自身的 save_config() 落盘，拷贝成
        # 纯 dict 会把这条回写通路切断（配置改了但重启即丢）。
        self.config: dict = config if config is not None else {}
        self._http: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._checking = False
        # 数据目录要在构造期就拿到：文案导出、WebUI 上传落盘都用 _data_dir 定位
        # 落点，若拖到 initialize() 才赋值，任何"未走 initialize"的路径（面板在
        # 初始化前的调用、单测直接构造、初始化中途抛异常）都会退化成写当前工作
        # 目录，把用户文案/上传件撒进 AstrBot 安装目录甚至插件源码树。
        self._data_dir: Path | None = _resolve_data_dir()
        # 多平台订阅（抖音/小红书/Instagram）。惰性创建：只有真正用到时才构造，
        # 这样纯微博用户的启动路径与升级前完全一致。
        self.sns: Any = None
        self._state_dirty = False
        self._state_save_task: asyncio.Task | None = None
        # 游客身份 cookie（仅 .weibo.cn 匿名游客，非用户账号）；UA 与身份绑定
        self._visitor_cookies: dict[str, str] = {}
        self._visitor_ts = 0.0
        self._ua: str = ""
        # 续期节奏控制
        self._last_renew_ts = 0.0
        self._renew_times: list[float] = []
        self._renew_fail_count = 0
        self._renew_fail_until = 0.0
        # IP 级风控全局冷却
        self._blocked_until = 0.0
        self._risk_level = 0
        self._last_risk_ts = 0.0
        # 全局请求间隔
        self._last_request_mono = 0.0
        self._proxy: str | None = None
        # SSRF 校验用 DNS 缓存：host -> (getaddrinfo 结果, 过期 monotonic)
        self._dns_cache: dict[str, tuple[list[tuple[Any, ...]], float]] = {}
        # 自定义 Cookie（config.custom_cookie，非空时优先于游客身份）
        self._custom_cookies: dict[str, str] = {}
        # 并发闸：请求间隔与游客续期在轮询任务与指令处理并发时同样全局生效
        self._pace_lock = asyncio.Lock()
        self._renew_lock = asyncio.Lock()
        # uid -> {"name", "seen_ids"(list), "baseline_done"}
        self.accounts: dict[str, dict[str, Any]] = {}
        # 待推送队列: [{"post_id","uid","text","created_ts","retries"}]
        self.pending: list[dict[str, Any]] = []
        self.last_check_ts = 0.0
        # ---- 群相册自动上传 ----
        self._album_tasks: set[asyncio.Task] = set()
        # ---- 视频转发 ----
        self._video_tasks: set[asyncio.Task] = set()
        self._video_preparing: set[str] = set()  # 正在后台下载视频的 post_id
        # 上传并发闸 + 在途 base64 载荷字节预算（配置热改时重建）
        self._up_sem: asyncio.Semaphore | None = None
        self._up_conc = 0
        self._up_bytes = 0
        self._up_cond: asyncio.Condition | None = None
        self._dl_sem: asyncio.Semaphore | None = None
        self._dl_conc = 0
        self._cpu_sem = asyncio.Semaphore(2)  # ffmpeg 转码这类重 CPU 活的全局闸
        self._album_locks: dict[str, asyncio.Lock] = {}  # 同群批次串行化
        # (群, 相册标识) -> (时间戳, album_id, album_name)
        self._album_cache: dict[str, tuple[float, str, str]] = {}
        # same_host 的相册文件名回读很贵：只在台账 miss 时查，并缓存 10 分钟。
        self._album_existing_cache: dict[str, tuple[float, str]] = {}
        # bot 实例 -> 登录账号 self_id；弱引用避免协议端重连后旧对象泄漏
        self._self_ids: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._ffmpeg = ""  # ffmpeg 可执行文件路径，initialize 时探测
        self._diag_path: Path | None = None  # 相册上传诊断文件（initialize 时解析）
        self._errlog: _ErrorLogStore | None = None  # 错误留存（initialize 时挂载）
        self._errlog_target: logging.Logger | None = None  # 留存挂在哪个日志器
        # ---- WebUI 控制面板（dashboard.py 注册路由，旧版 AstrBot 自动降级）----
        self._web_ready = False
        self._sns_web_ready = False
        self._manual_check_task: asyncio.Task | None = None
        # 面板手动取消：被取消的条目在撤销窗口内留在这里（可一键放回队列）。
        # 与 pending 分开存：pending 是"还要发的"，这里是"已经决定不发、还能反悔的"，
        # 混在一起会让 _flush_pending、面板计数、时效清理三处的口径都要加判断
        self.cancelled: list[dict[str, Any]] = []
        # 本轮推送是否被面板叫停。_flush_pending 在条目之间检查：置位就收尾，剩余
        # 条目原样留在队列里（不消耗重试次数——叫停不是失败）
        self._push_round_stop = False
        # 本轮推送是否正在进行：面板据此判断"停止本轮"此刻是否有效，
        # 也用于避免两处同时改 _push_round_stop 造成误清
        self._push_round_active = False
        # 运行统计与最近动态（随 state.json 持久化，供面板展示）
        self.stats: dict[str, Any] = {
            "push_ok": 0,
            "push_fail": 0,
            "album_uploaded": 0,
            "album_skipped": 0,
            "album_failed": 0,
            "by_uid": {},  # uid -> {"name", "push_ok"}
            "daily": {},  # "YYYY-MM-DD" -> {"push_ok", "push_fail"}
        }
        # 已执行过的一次性升级迁移标记（随 state.json 持久化）：
        # 标记存在就不再检查，迁移动作永不与用户后续的手工配置打架
        self._migrations: set[str] = set()
        self.activity: deque[dict[str, Any]] = deque(maxlen=ACTIVITY_MAX)
        # ---- @全体成员 的生效条件（权限 / 频次 / 回读确认）----
        # 机器人在各群的角色缓存："platform_id:群号" -> (时间戳, owner/admin/member)
        self._at_all_roles: dict[str, tuple[float, str]] = {}
        # 24 小时频次台账：QQ 的 @全体 次数按账号算、所有群共享，随 state.json 落盘
        # （不落盘的话一次重载就能把已经用掉的次数洗掉，接着发就是继续被降级）
        self._at_all_sent: list[float] = []
        # 最近一次 @全体 的实际结果（面板「推送 @全体成员」卡片底部展示）
        self._at_all_last: dict[str, Any] = {}
        # 同一会话同一条原因的记录时间戳：投递失败/权限不足会连续命中，不做去重
        # 就是每轮推送往日志和动态里各刷一条
        self._at_all_notices: dict[str, float] = {}
        # ---- 归档（P3）：这里只建宿主状态，业务全部在 archive.py（ArchiveMixin）----
        # 有界队列：满即丢弃，归档永远是旁路，不能反压推送或回填。
        self._archive_dl_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=ARCHIVE_QUEUE_MAX)
        self._archive_worker_task: asyncio.Task | None = None
        # 回填停止标记（指令置位，回填循环在当前页处理完后退出）
        self._backfill_stop = False
        self._backfill_task: asyncio.Task | None = None
        self._archive: ArchiveStore | None = None
        # 最近一次回填统计：archive.py 写，微博回填状态 指令读
        self._backfill_last_stats: dict[str, Any] = {}
        # ---- 图床/向量管线（P4）：宿主状态在这里，业务全部在 pipeline.py ----
        # 服务由入口 main.py 注入（溯源域的 bed/vector/library）；未注入时管线整体降级
        self._pipeline_services: Any | None = None
        self._pipeline_queue: asyncio.Queue[str] | None = None
        self._pipeline_worker_task: asyncio.Task | None = None
        self._pipeline_pending: set[str] = set()
        self._pipeline_last_error: str = ""
        self._pipeline_backoff: float = 0.0

    async def initialize(self):
        # 面板分组 → 平铺镜像：必须先于任何业务配置读取
        self._sync_config_groups()
        self._data_dir = _resolve_data_dir() or self._data_dir
        if self._data_dir is None:
            raise RuntimeError(f"{PLUGIN_NAME} 无法确定数据目录（StarTools.get_data_dir 失败）")
        # 归档库先于轮询打开：增量归档钩子挂在 _check_account 上，store 未就绪
        # 时钩子会静默跳过。archive_open 自身不抛，这里只是双保险：归档坏了
        # 也绝不能让插件起不来。
        try:
            await self.archive_open()
        except Exception as e:  # 归档失败不阻断启动
            logger.warning(f"{PLUGIN_NAME} 归档库打开失败（归档功能降级）: {e}")
        # 图床管线要读 archive.db，必须在归档库打开之后启动；没注入服务时只是空转
        try:
            self.pipeline_start()
        except Exception as e:  # 管线失败同样不阻断监控主链
            logger.warning(f"{PLUGIN_NAME} 图床管线启动失败（管线降级）: {e}")
        self._load_state()
        # 存量配置迁移放在 state 读取之后：迁移标记就在 state.json 里，
        # 先读标记才能保证"迁移过就永不再动"的语义
        self._migrate_album_max_images()
        self._sync_accounts_from_config()
        # 实况图转 GIF 依赖 ffmpeg（缺失时回落封面静图，不影响其余功能）
        self._ffmpeg = shutil.which("ffmpeg") or ""
        if not self._ffmpeg:
            logger.info(f"{PLUGIN_NAME} 未找到 ffmpeg，实况图将只上传封面静图")
        # 诊断文件随启动记录一份流控配置快照，配置改了对不上号时以它为准
        self._diag_path = self._data_dir / DIAG_FILE
        # 统一错误留存：溯源域与微博域共用同一套留存器和同一份日志。
        self._errlog = install_error_log(self, self._data_dir / ERRLOG_FILE)
        restored = await asyncio.to_thread(self._errlog.restore)
        if restored:
            logger.info(
                f"错误留存已回读上次运行残留的 {restored} 条告警"
                "（面板可查，可能包含崩溃前的最后记录）"
            )
        try:
            self._web_ready = register_dashboard(
                self, PLUGIN_NAME, PLUGIN_VERSION, _extract_uid, redact_proxy
            )
        except Exception as exc:  # 面板异常绝不阻断监控、归档或推送
            self._web_ready = False
            logger.warning(f"{PLUGIN_NAME} 统一 WebUI 注册失败（面板降级）: {exc!r}")
        # 订阅域的 7 条接口单独注册：它失效不该把整个面板拖下水
        try:
            self._sns_web_ready = register_sns_dashboard(self, PLUGIN_NAME, PLUGIN_VERSION)
        except Exception as exc:
            self._sns_web_ready = False
            logger.warning(f"{PLUGIN_NAME} 订阅面板接口注册失败（降级）: {exc!r}")
        await self._diag(
            f"启动 ffmpeg={'有' if self._ffmpeg else '无'} "
            f"album_download_concurrency={self._int_cfg('album_download_concurrency', 5)} "
            f"album_upload_concurrency={self._int_cfg('album_upload_concurrency', 3)} "
            f"album_upload_payload_mb={self._float_cfg('album_upload_payload_mb', 32.0):g} "
            f"album_live_gif={bool(self.config.get('album_live_gif', True))} "
            f"message_with_videos={self._videos_enabled()} "
            f"video_max_mb={self.config.get('video_max_mb')} "
            f"video_quality={self.config.get('video_quality')}"
        )
        # 清掉上次运行残留的下载临时文件（正常流程传完即删，这里只兜底）
        await asyncio.to_thread(self._wipe_album_tmp)
        await asyncio.to_thread(self._wipe_video_tmp)
        # DummyCookieJar：cookie 全部手工管理，避免与 jar 自动附带冲突。
        # 连接器 DNS 缓存放宽到 5 分钟（默认 10 秒）：微博系 host 极少变更，
        # 与校验侧的 _dns_cache 一起把每批图片几十次解析压到个位数
        self._http = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(ttl_dns_cache=300),
        )
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            if proxy.startswith(("http://", "https://")):
                self._proxy = proxy
                logger.info(f"{PLUGIN_NAME} 使用代理出网: {redact_proxy(proxy)}")
            else:
                logger.warning(
                    f"{PLUGIN_NAME} proxy 配置无效（需 http:// 或 https:// 开头），"
                    f"已忽略: {redact_proxy(proxy)}"
                )
        # 自定义 Cookie 模式：直接使用用户身份，跳过游客身份的预热与续期
        self._custom_cookies = self._parse_cookie_string(
            str(self.config.get("custom_cookie") or "")
        )
        # 启动预热：身份过期则换新；身份健在则访问一次主页刷新 _T_WM/XSRF 指纹
        if self._custom_cookies:
            logger.info(
                f"{PLUGIN_NAME} 已启用自定义 Cookie（{sorted(self._custom_cookies)}），"
                "游客身份流程已跳过"
            )
        elif self._visitor_cookies.get("SUB") and (
            time.time() - self._visitor_ts <= VISITOR_MAX_AGE
        ):
            await self._warmup_quietly()
        else:
            await self._renew_visitor_quietly()
        self._poll_task = asyncio.create_task(self._poll_loop())
        # 多平台订阅域：取数、守卫、投递全部由 SnsService 自己管；这里只负责把它
        # 拉起来。投递走的是本域已有的 deliver_sns_payload()，两条推送共用一条链。
        try:
            await self.sns_start()
        except Exception as exc:  # 订阅域起不来不能影响微博监控
            logger.warning(f"{PLUGIN_NAME} 多平台订阅启动失败（该功能降级）: {exc!r}")
        logger.info(
            f"{PLUGIN_NAME} 已启动：监控 {len(self.accounts)} 个账号，"
            f"推送目标 {len(self._sessions())} 个"
        )

    # ---------------------------------------------------------------- 多平台订阅

    def sns_cfg(self, key: str, default=None):
        """读 `sns` 配置组（整组是唯一定义面：AstrBot 会删掉未声明的平铺键）。"""
        group = self.config.get("sns")
        if isinstance(group, dict) and group.get(key) is not None:
            return group[key]
        return default

    def sns_service(self):
        """惰性构造订阅服务（需求驱动：不订阅就不加载任何订阅域代码）。"""
        if self.sns is None:
            from ..sns.service import SnsService

            self.sns = SnsService(self, logger=logger)
        return self.sns

    async def sns_start(self) -> None:
        if not self.sns_cfg("enabled", True):
            return
        service = self.sns_service()
        await service.start()

    async def sns_stop(self) -> None:
        if self.sns is not None:
            await self.sns.stop()
            self.sns = None

    async def terminate(self):
        # 留存 Handler 先排空并摘除：重载时重复挂载会把每条告警收两遍。
        remove_error_log(self)
        # 订阅域要在关 HTTP 会话/浏览器之前停：它在途的取数还挂着 session
        try:
            await self.sns_stop()
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} 多平台订阅停止失败: {exc}")
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        # 面板触发的手动检查也要撤掉，避免 terminate 后还在写状态
        if self._manual_check_task:
            self._manual_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._manual_check_task
            self._manual_check_task = None
        # 相册上传是独立任务，要在关掉 HTTP 会话之前撤掉（在途下载引用着 session）
        for task in list(self._album_tasks):
            task.cancel()
        if self._album_tasks:
            await asyncio.gather(*self._album_tasks, return_exceptions=True)
            self._album_tasks.clear()
        # 视频下载任务同样在关 HTTP 会话之前撤掉
        for task in list(self._video_tasks):
            task.cancel()
        if self._video_tasks:
            await asyncio.gather(*self._video_tasks, return_exceptions=True)
            self._video_tasks.clear()
        # 归档：先撤回填（它可能正在用 HTTP 翻页/下载），再关下载 worker 与 SQLite。
        # 顺序在关 HTTP 会话之前，否则在途下载会撞上已关闭的 session。
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._backfill_task
            self._backfill_task = None
        # 面板触发的任务（图床重扫、手动检查等）要在关业务资源之前撤掉：
        # 它们会调 trace 域的方法，资源关了还会继续写状态。
        dashboard_api = getattr(self, "_dashboard_api", None)
        if dashboard_api is not None:
            try:
                await dashboard_api.shutdown()
            except Exception as e:  # 面板收尾失败不影响其余清理
                logger.warning(f"{PLUGIN_NAME} 管理面板后台任务停止失败: {e}")
        # 管线 worker 也在用 HTTP 与 archive.db：必须在关归档库之前停，否则它会
        # 在关闭中的连接上继续跑单帖流程
        try:
            await self.pipeline_stop()
        except Exception as e:  # 停止失败不影响其余收尾
            logger.warning(f"{PLUGIN_NAME} 图床管线停止失败: {e}")
        try:
            await self.archive_close()
        except Exception as e:  # 关闭失败不影响其余收尾
            logger.warning(f"{PLUGIN_NAME} 归档库关闭失败: {e}")
        if self._state_save_task and not self._state_save_task.done():
            self._state_save_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._state_save_task
            self._state_save_task = None
        self._save_state()
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None
        await asyncio.to_thread(self._wipe_album_tmp)
        await asyncio.to_thread(self._wipe_video_tmp)
        logger.info(f"{PLUGIN_NAME} 已停止")

    def _save_config(self):
        """把内存里的 self.config 落盘。

        config 来自加载器时是 AstrBotConfig（带 save_config）；只有配置项被
        手工构造/测试注入时才是纯 dict，那时没有 save_config 可调。统一走这里，
        不让某个调用点因为 config 类型不同而 AttributeError。
        """
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()

    def _cfg_group(self, group: str) -> dict:
        value = self.config.get(group)
        return value if isinstance(value, dict) else {}

    def _ensure_cfg_group(self, group: str) -> dict:
        value = self.config.get(group)
        if not isinstance(value, dict):
            value = {}
            self.config[group] = value
        return value

    def _sync_config_groups(self) -> None:
        """把面板分组值镜像到平铺键（微博域业务代码只读平铺键）。

        AstrBot 按 schema 删除未声明的键，旧版平铺配置无法靠"另存一份"存活；
        分组是唯一持久面，所以启动时以分组为准覆盖平铺镜像。运行期写入统一走
        `_set_cfg`，同时更新两边，避免"面板改了不生效"或"命令改了重启就丢"。
        """
        for key, (group, field) in WEIBO_CFG_PATHS.items():
            group_cfg = self._cfg_group(group)
            if field in group_cfg:
                self.config[key] = group_cfg[field]

    def _set_cfg(self, key: str, value) -> None:
        """写配置：平铺键供业务代码读取，同时回写 schema 分组供面板展示。"""
        self.config[key] = value
        path = WEIBO_CFG_PATHS.get(key)
        if path:
            group, field = path
            self._ensure_cfg_group(group)[field] = value

    def _migrate_album_max_images(self):
        """存量配置一次性迁移：旧默认 9 → 18（v1.12.1）。

        v1.11.x 及以前 album_max_images 默认 9，AstrBot 会把默认值物化进部署
        的 config 文件；v1.12.0 把默认改到 18，但升级只补缺键、已存的 9 不会
        跟着变，18 图帖照旧被 `[:max_imgs]` 切回 9 张（线上复现过"识别 18、
        上传 9"）。这里只在「存量值恰好等于旧默认 9 且从未迁移过」时上调一次：

        - 判定特征是"恰好等于旧默认"：用户真改过的值（5/12/…）一眼可见，
          绝不碰；
        - 标记落 state.json，迁移过后用户再手动设 9 属于明确意图，尊重之，
          此后任何启动都不再自动改动；
        - 上调同时写回 config（_save_config）并打 warning（错误留存/面板可
          查），用户能知道配置被动过、想去哪改回。
        """
        if "album_max_images_v18" in self._migrations:
            return
        try:
            cur = int(self.config.get("album_max_images", DEFAULT_MAX_IMAGES))
        except (TypeError, ValueError):
            cur = DEFAULT_MAX_IMAGES
        # 标记先行：本次无论是否上调，此后都不再检查第二次
        self._migrations.add("album_max_images_v18")
        if cur != OLD_ALBUM_MAX_IMAGES:
            return
        self._set_cfg("album_max_images", DEFAULT_MAX_IMAGES)
        self._save_config()
        logger.warning(
            f"{PLUGIN_NAME} 检测到 album_max_images 仍为旧版本默认值 "
            f"{OLD_ALBUM_MAX_IMAGES}（超 9 图微博会被截回 9 张），"
            f"已一次性自动上调为 {DEFAULT_MAX_IMAGES}（微博单条图片上限）；"
            "如确实需要限制单条张数，请在插件配置里改回，之后不会再自动调整"
        )

    def _sessions(self) -> list[str]:
        return [str(s) for s in (self.config.get("push_sessions") or [])]

    def _int_cfg(self, key: str, default: int) -> int:
        v = self.config.get(key)
        if v is None or v == "":
            return default
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    def _float_cfg(self, key: str, default: float) -> float:
        v = self.config.get(key)
        if v is None or v == "":
            return default
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if v >= 0 else default

    def _message_format(self) -> str:
        return str(self.config.get("message_format") or DEFAULT_MESSAGE_FORMAT).replace("\\n", "\n")

    def _max_post_age(self) -> float:
        """时效过滤上限（秒）；0 表示关闭。"""
        return float(self._int_cfg("max_post_age_minutes", 0) * 60)

    def _sync_accounts_from_config(self):
        """以配置里的 monitored_uids 为准同步监控列表（兼容 WebUI 直接改动）。"""
        uids: list[str] = []
        for item in self.config.get("monitored_uids") or []:
            uid = str(item).strip()
            if uid.isdigit():
                uids.append(uid)
        for uid in dict.fromkeys(uids):  # 去重且保持顺序
            if uid not in self.accounts:
                self.accounts[uid] = {
                    "name": uid,
                    "seen_ids": [],
                    "baseline_done": False,
                    "fail_count": 0,
                    "next_retry_ts": 0.0,
                }
        for uid in [u for u in self.accounts if u not in uids]:
            self.accounts.pop(uid, None)

    def _state_file(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / "state.json"

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        """补齐条目的可选字段（旧 state.json 缺字段时用默认值）。

        待推送队列与「已取消（可撤销）」区共用这一套归一化：恢复就是把取消区的
        条目原样放回发送链路，字段形状必须一致，否则恢复后会在 _build_chain /
        _video_ready 里直接撞 KeyError。
        """
        item.setdefault("retries", 0)
        item.setdefault("created_ts", 0.0)
        # v1.4.0 起待推送条目携带图片信息，旧状态文件没有该字段
        if not isinstance(item.get("pics"), list):
            item["pics"] = []
        # v1.6.0 起携带视频信息；旧条目里的 video_file 路径若已随
        # video_tmp 清空失效，_video_ready 会自动重置为待下载
        if not isinstance(item.get("videos"), list):
            item["videos"] = []
        if not isinstance(item.get("video_files"), list):
            item["video_files"] = []
        item.setdefault("video_note", "")
        # v1.7.2 起记录直链新鲜度与已试轮数。旧条目没有 videos_ts，按 0
        # 处理即"早已过签名窗口"，下载前必然先重取直链——旧链注定 403
        item.setdefault("videos_ts", 0.0)
        item.setdefault("video_attempts", 0)
        item.setdefault("video_fail", "")
        # v1.7.0 起携带原始正文（关键词相册路由匹配用）；旧条目回退渲染后消息
        item.setdefault("raw_text", "")
        return item

    @staticmethod
    def _dump_item(item: dict[str, Any]) -> dict[str, Any]:
        """落盘前的条目副本：滤掉运行期内部标记（下划线开头）。

        _send_active / _cancel_requested / _cancel_done 只在内存里表达"这一条
        正在发送 / 已被取消"，写进 state.json 没有意义，重启后读到反而会把一条
        普通条目误判成"发送中"或"已取消"。
        """
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _load_state(self):
        try:
            raw = self._state_file().read_text(encoding="utf-8")
            state = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 读取 state.json 失败，将重建状态: {e}")
            return
        for uid, info in (state.get("accounts") or {}).items():
            if not str(uid).isdigit():
                continue
            seen = [str(i) for i in (info.get("seen_ids") or [])][-SEEN_IDS_LIMIT:]
            self.accounts[str(uid)] = {
                "name": str(info.get("name") or uid),
                "seen_ids": seen,
                "baseline_done": bool(info.get("baseline_done")),
                "fail_count": 0,
                "next_retry_ts": 0.0,
            }
        pending: list[dict[str, Any]] = []
        unverified: list[dict[str, Any]] = []
        for item in state.get("pending") or []:
            if isinstance(item, dict) and item.get("post_id") and item.get("text"):
                # 发送中途崩溃/重载的条目带着"已开始发送"戳：那次发送的结果
                # 未知，而大载荷最常见的结局是"实际已送达但判超时/进程被杀"——
                # 再发一遍就是群里两条一样的消息。宁可漏不可重：跳过并记账，
                # 真没发出去的场合群里有原帖链接可看
                if item.get("last_send_ts"):
                    unverified.append(item)
                    continue
                pending.append(self._normalize_item(item))
        self.pending = pending[-PENDING_HARD_LIMIT:]
        # 面板取消掉、还在撤销窗口内的条目：与待推送条目共用同一套字段归一化，
        # 恢复时才能原样回到发送链路。过了窗口（或超保留条数）的在这里就被清掉，
        # 它们的媒体临时文件也正是在这一步真正删除的
        cancelled: list[dict[str, Any]] = []
        for item in state.get("cancelled") or []:
            if isinstance(item, dict) and item.get("post_id"):
                item.setdefault("cancelled_ts", 0.0)
                cancelled.append(self._normalize_item(item))
        self.cancelled = cancelled
        self._cancel_prune()
        self.last_check_ts = float(state.get("last_check_ts") or 0)
        visitor = state.get("visitor") or {}
        cookies = visitor.get("cookies") or {}
        if isinstance(cookies, dict) and cookies.get("SUB"):
            self._visitor_cookies = {str(k): str(v) for k, v in cookies.items()}
            self._visitor_ts = float(visitor.get("ts") or 0)
            ua = str(visitor.get("ua") or "")
            self._ua = ua if ua in UA_POOL else ""
        # v1.5.0 起随状态文件持久化运行统计与最近动态（旧文件缺省时用默认值）
        stats = state.get("stats")
        if isinstance(stats, dict):
            merged = dict(self.stats)
            for key in (
                "push_ok",
                "push_fail",
                "album_uploaded",
                "album_skipped",
                "album_failed",
            ):
                try:
                    merged[key] = max(0, int(stats.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            if isinstance(stats.get("by_uid"), dict):
                merged["by_uid"] = {
                    str(u): d
                    for u, d in stats["by_uid"].items()
                    if str(u).isdigit() and isinstance(d, dict)
                }
            if isinstance(stats.get("daily"), dict):
                merged["daily"] = {
                    str(k): v for k, v in stats["daily"].items() if isinstance(v, dict)
                }
            self.stats = merged
        acts = state.get("activity")
        if isinstance(acts, list):
            self.activity.clear()
            for act in acts[-ACTIVITY_MAX:]:
                if not isinstance(act, dict):
                    continue
                self.activity.append(
                    {
                        "ts": float(act.get("ts") or 0),
                        "kind": str(act.get("kind") or "info"),
                        "uid": str(act.get("uid") or ""),
                        "name": str(act.get("name") or ""),
                        "text": str(act.get("text") or ""),
                        "detail": str(act.get("detail") or ""),
                    }
                )
        # 跳过"发送中"条目的记账要放在动态历史加载之后：_record_push 会写动态，
        # 提前写会被上面这段覆盖掉
        for item in unverified:
            self._record_push(
                str(item.get("uid") or ""),
                False,
                str(item.get("text") or ""),
                f"重启/重载前正在推送微博 {item.get('post_id')}，发送结果未知，"
                "为防重复未重发；如群里没收到请点原帖链接",
            )
        # 一次性升级迁移标记（旧文件没有该键时保持空集，迁移逻辑自行兜底）
        migs = state.get("migrations")
        if isinstance(migs, list):
            self._migrations = {str(m) for m in migs if isinstance(m, str)}
        # @全体 的 24 小时频次台账与上次投递结果（旧文件没有这两个键时保持默认）
        sent = state.get("at_all_sent")
        if isinstance(sent, list):
            self._at_all_sent = [float(ts) for ts in sent if isinstance(ts, (int, float))][
                -AT_ALL_QUOTA_MAX_KEEP:
            ]
        last = state.get("at_all_last")
        if isinstance(last, dict) and last:
            self._at_all_last = {
                "ts": float(last.get("ts") or 0),
                "session": str(last.get("session") or ""),
                "state": str(last.get("state") or ""),
                "detail": str(last.get("detail") or ""),
            }
        self._at_all_quota_used()  # 顺带清掉已过窗口的时间戳
        self._prune_stats()

    def _save_state(self):
        if self._data_dir is None:
            return
        scheduled = self._state_save_task
        if scheduled and not scheduled.done() and scheduled is not asyncio.current_task():
            scheduled.cancel()
            self._state_save_task = None
        self._prune_stats()
        state = {
            "version": 2,
            "accounts": {
                uid: {
                    "name": info["name"],
                    "seen_ids": info["seen_ids"][-SEEN_IDS_LIMIT:],
                    "baseline_done": info["baseline_done"],
                }
                for uid, info in self.accounts.items()
            },
            "pending": [self._dump_item(it) for it in self.pending],
            # 面板取消的条目（撤销窗口内可恢复）：不落盘的话插件一重载就再也
            # 恢复不了，而"取消"恰恰多发生在需要重载配置排障的时候
            "cancelled": [self._dump_item(it) for it in self.cancelled],
            "last_check_ts": self.last_check_ts,
            "visitor": {
                "cookies": self._visitor_cookies,
                "ts": self._visitor_ts,
                "ua": self._ua,
            },
            "stats": self.stats,
            "activity": list(self.activity),
            # @全体 的频次台账（账号级、所有群共享）与最近一次投递结果：重载后
            # 接着算次数，面板也能看到上次到底生效没有
            "at_all_sent": self._at_all_sent[-AT_ALL_QUOTA_MAX_KEEP:],
            "at_all_last": self._at_all_last,
            # 一次性升级迁移标记：重启/重载后据此不再重复迁移
            "migrations": sorted(self._migrations),
        }
        try:
            # 先写临时文件再原子替换：进程恰好死在写入中途不会留下截断的
            # state.json（截断会让下次启动丢掉游客身份与整个待推送队列）
            tmp = self._data_dir / "state.json.tmp"
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_file())
            self._state_dirty = False
        except Exception as e:
            # 写盘失败（磁盘满/权限/临时文件被占用）必须把 dirty 置回：否则内存里
            # 已经推进的 pending、seen_ids、游客 cookie 永远不会再尝试落盘，
            # 下次重载回到旧快照——丢队列、丢身份，还可能重复推送。
            self._state_dirty = True
            logger.warning(f"{PLUGIN_NAME} 保存 state.json 失败（稍后重试）: {e}")
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _save_state_debounced(self) -> None:
        """Mark state dirty and coalesce high-frequency writes into one save."""
        self._state_dirty = True
        if self._data_dir is None:
            return
        scheduled = self._state_save_task
        if scheduled and not scheduled.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # 同步调用（测试/脚本）没有事件循环：直接落盘
            self._save_state()
            return
        self._state_save_task = asyncio.create_task(self._flush_state_after_debounce())

    async def _flush_state_after_debounce(self) -> None:
        try:
            await asyncio.sleep(STATE_SAVE_DEBOUNCE)
            if self._state_dirty:
                self._save_state()
        finally:
            if self._state_save_task is asyncio.current_task():
                self._state_save_task = None

    def _account_name(self, uid: str) -> str:
        return str(self.accounts.get(uid, {}).get("name") or uid)

    def _record_event(
        self,
        kind: str,
        uid: str = "",
        name: str = "",
        text: str = "",
        detail: str = "",
    ):
        """记一条最近动态（面板展示用）；text/detail 各自截断防 state.json 膨胀。

        detail 用 ACTIVITY_DETAIL_MAX 而不是和 text 同一个 80：拒收回执这类诊断
        文字截到一半就只剩噪音，面板上读不出是 1200 的哪一档失败。
        """
        self.activity.append(
            {
                "ts": time.time(),
                "kind": kind,
                "uid": str(uid or ""),
                "name": str(name or ""),
                "text": str(text or "")[:ACTIVITY_TEXT_MAX],
                "detail": str(detail or "")[:ACTIVITY_DETAIL_MAX],
            }
        )

    def _record_push(self, uid: str, ok: bool, text: str, detail: str = ""):
        """推送成功/放弃的统一记账：动态 + 累计 + 按天 + 按博主。"""
        key = "push_ok" if ok else "push_fail"
        self.stats[key] = int(self.stats.get(key, 0)) + 1
        day = datetime.now().strftime("%Y-%m-%d")
        bucket = self.stats.setdefault("daily", {}).setdefault(day, {"push_ok": 0, "push_fail": 0})
        bucket[key] = int(bucket.get(key, 0)) + 1
        name = self._account_name(uid)
        if uid:
            entry = self.stats.setdefault("by_uid", {}).setdefault(
                str(uid), {"name": "", "push_ok": 0}
            )
            entry["name"] = name
            if ok:
                entry["push_ok"] = int(entry.get("push_ok", 0)) + 1
        self._record_event(
            "push" if ok else "push_fail", uid=uid, name=name, text=text, detail=detail
        )

    def _record_album(self, uploaded: int, skipped: int, failed: int, detail: str):
        """相册上传结果记账：张数进累计，批次进最近动态。"""
        self.stats["album_uploaded"] = int(self.stats.get("album_uploaded", 0)) + uploaded
        self.stats["album_skipped"] = int(self.stats.get("album_skipped", 0)) + skipped
        self.stats["album_failed"] = int(self.stats.get("album_failed", 0)) + failed
        self._record_event("album_fail" if failed else "album", detail=detail)

    def _prune_stats(self):
        """统计桶只留最近 STATS_DAILY_KEEP 天；博主计数只在长出头时裁掉已取消监控的。"""
        cutoff = (datetime.now() - timedelta(days=STATS_DAILY_KEEP)).strftime("%Y-%m-%d")
        daily = self.stats.get("daily") or {}
        self.stats["daily"] = {k: v for k, v in daily.items() if str(k) >= cutoff}
        by_uid = self.stats.get("by_uid") or {}
        if len(by_uid) > 64:
            self.stats["by_uid"] = {u: d for u, d in by_uid.items() if u in self.accounts}

    def _mem_snapshot(self) -> dict[str, int] | None:
        """当前内存水位 (可用/总量 MB)；读不到（非 Linux/容器未挂载）返回 None。

        前两次"服务器无响应"都是内存顶穿——面板把它亮出来，配合错误留存，
        死机前最后一眼数据里就有内存余量。
        """
        try:
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts:
                        info[key.strip()] = int(parts[0])
            total = info.get("MemTotal", 0) // 1024
            avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
            return {"total_mb": total, "available_mb": avail} if total else None
        except (OSError, ValueError):
            return None

    def _disk_snapshot(self) -> dict[str, int] | None:
        """插件数据目录所在盘的余量（媒体下载/视频缓存的落盘位置）。"""
        if self._data_dir is None:
            return None
        try:
            usage = shutil.disk_usage(self._data_dir)
        except OSError:
            return None
        return {
            "total_mb": usage.total // 1048576,
            "free_mb": usage.free // 1048576,
        }

    async def clear_error_log(self) -> int:
        """清空错误留存（面板按钮），返回清掉的条数。"""
        if self._errlog is None:
            return 0
        return self._errlog.clear()
