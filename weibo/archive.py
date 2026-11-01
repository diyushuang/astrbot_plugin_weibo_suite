"""归档编排：archive.db 中心账本 + 原图下载 worker + 回填 + JSON 导入 + 对账。

本模块只做编排，SQL 细节全部在 ArchiveStore（weibo/archive_db.py）。纪律：归档是旁路
能力，任何失败都不得抛给调用方——store 未就绪 / 网络异常 / 配置缺失一律降级返回并
logger.warning，绝不阻塞推送主链路。
"""

from __future__ import annotations

from .archive_db import ArchiveStore
from .runtime import *

# 常量兜底：constants.py 由轨道 C 并行落地，落地前本模块也必须能 import；落地后沿用常量值。
ARCHIVE_DB_NAME = globals().get("ARCHIVE_DB_NAME") or "archive.db"
ARCHIVE_DIR_NAME = globals().get("ARCHIVE_DIR_NAME") or "archive"
ARCHIVE_QUEUE_MAX = globals().get("ARCHIVE_QUEUE_MAX") or 500
BACKFILL_SAFETY_MAX_PAGES = globals().get("BACKFILL_SAFETY_MAX_PAGES") or 1000
ARCHIVE_CLOSE_GRACE_SECONDS = globals().get("ARCHIVE_CLOSE_GRACE_SECONDS") or 5.0
MEDIA_STATES = tuple(
    globals().get("MEDIA_STATES") or ("pending", "downloaded", "album", "bed", "thumb", "vector")
)

ARCHIVE_LONGTEXT_URL = "https://weibo.com/ajax/statuses/longtext"
ARCHIVE_GAP_LIMIT = 20  # 对账明细最多 20 条（按类型分组后取前 N）
ARCHIVE_GAP_KEEP = 40  # 单类型最多缓存多少条明细，避免全库扫描时吃内存
ARCHIVE_SCAN_LIMIT = 5000  # 对账扫描帖子上限（防御性，避免一次命令扫几万条）
ARCHIVE_SCAN_PAGE = 200
# 关闭哨兵：worker 读到它就干净退出（不当作待下载的 mid）
_ARCHIVE_STOP = object()


def _archive_int(value, default: int = 0) -> int:
    """宽容 int：None / "" / "abc" 一律回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _archive_safe_seg(value, fallback: str) -> str:
    """把 uid/mid/pid 变成安全路径片段（去掉分隔符与首尾点）。"""
    text = BAD_NAME_RE.sub("", str(value or "")).strip().strip(".")
    return text[:64] or fallback


def _archive_safe_ext(value) -> str:
    """扩展名只留字母数字，缺省 jpg。"""
    text = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).lower()[:8]
    return text or "jpg"


def _archive_free_bytes(path) -> int | None:
    """目录所在磁盘的剩余字节数；目录还不存在时向上找到最近的已存在祖先。

    归档目录是**按需创建**的（第一条原图落盘时才 mkdir），所以直接对
    ``media_root`` 调 ``shutil.disk_usage`` 会在首次归档时抛 FileNotFoundError，
    把整条下载链带崩。这里向上回溯到已存在的祖先再问余量；实在取不到就返回
    None，由调用方决定"未知余量"怎么办——不能因为量不出来就误判成写满。
    """
    try:
        candidate = Path(path)
    except TypeError:
        return None
    for _ in range(16):
        try:
            if candidate.exists():
                return int(shutil.disk_usage(str(candidate)).free)
        except OSError:
            pass
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None


def _archive_longtext_of(payload) -> str:
    """从 longtext 响应里挖出全文（兼容 data 包裹与裸字段）。"""
    if not isinstance(payload, dict):
        return ""
    for holder in (payload.get("data"), payload):
        if not isinstance(holder, dict):
            continue
        for key in ("longTextContent_raw", "longTextContent"):
            text = holder.get(key)
            if isinstance(text, str) and text.strip():
                return text
    return ""


class ArchiveMixin:
    """归档编排：落账、原图下载、全量回填、JSON 种子导入、对账。"""

    # ------------------------------------------------------------------
    # 配置与生命周期
    # ------------------------------------------------------------------
    def _archive_group(self) -> dict:
        config = getattr(self, "config", None)
        if not hasattr(config, "get"):
            return {}
        group = config.get("archive")
        return group if isinstance(group, dict) else {}

    def _archive_bool(self, key: str, default: bool) -> bool:
        value = self._archive_group().get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(default if value is None else value)

    def _archive_float(self, key: str, default: float) -> float:
        value = self._archive_group().get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _archive_enabled(self) -> bool:
        return self._archive_bool("enabled", True)

    async def archive_open(self) -> None:
        """建库 + 懒启动下载 worker；幂等，异常只 warning（归档坏了不能拖垮插件）。"""
        try:
            if not self._archive_enabled():
                return
            if self.archive_store() is None:
                base = getattr(self, "_data_dir", None)
                if not base:
                    logger.warning("归档未启用：数据目录尚未就绪")
                    return
                base_path = Path(base)
                await asyncio.to_thread(base_path.mkdir, parents=True, exist_ok=True)
                store = ArchiveStore(base_path / ARCHIVE_DB_NAME, base_path / ARCHIVE_DIR_NAME)
                self._archive = store
            task = getattr(self, "_archive_worker_task", None)
            if task is None or task.done():
                self._archive_worker_task = asyncio.create_task(self._archive_worker())
            logger.info("归档已就绪：%s", getattr(self._archive, "db_path", ARCHIVE_DB_NAME))
        except Exception as e:
            logger.warning(f"归档初始化失败（不影响其它功能）: {e}")

    async def archive_close(self) -> None:
        """让 worker 收尾、必要时硬取消、关库；幂等不抛。

        先投停止哨兵而不是直接 cancel：cancel 会打断正在写的原图，让这条 mid
        既没 mark_media 也不在队列里（进程退出后彻底消失，只有对账能发现）。
        给一个有限宽限（ARCHIVE_CLOSE_GRACE_SECONDS）让当前那条下完，超时才硬取消。
        """
        task = getattr(self, "_archive_worker_task", None)
        self._archive_worker_task = None
        if task is not None and not task.done():
            stopped = False
            try:
                self._archive_queue().put_nowait(_ARCHIVE_STOP)
                stopped = True
            except Exception:  # 队列满/已关闭：只能硬取消
                stopped = False
            if not stopped:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                store = getattr(self, "_archive", None)
                self._archive = None
                if store is not None:
                    with contextlib.suppress(Exception):
                        store.close()
                return
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=ARCHIVE_CLOSE_GRACE_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:  # worker 自己抛错退出：不用再 cancel
                logger.debug("归档 worker 关闭时已自行退出", exc_info=True)
        store = getattr(self, "_archive", None)
        self._archive = None
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()

    def archive_store(self) -> ArchiveStore | None:
        return getattr(self, "_archive", None)

    def archive_ready(self) -> bool:
        return self.archive_store() is not None

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------
    def _archive_queue(self) -> asyncio.Queue:
        queue = getattr(self, "_archive_dl_queue", None)
        if not isinstance(queue, asyncio.Queue):
            queue = asyncio.Queue(maxsize=ARCHIVE_QUEUE_MAX)
            self._archive_dl_queue = queue
        return queue

    def _archive_media_root(self) -> Path | None:
        base = getattr(self, "_data_dir", None)
        if not base:
            return None
        return Path(base) / ARCHIVE_DIR_NAME

    @staticmethod
    def _archive_bid_from_link(link) -> str:
        text = str(link or "").strip()
        if "weibo.com/" not in text:
            return ""
        tail = text.rstrip("/").rsplit("/", 1)[-1]
        return tail if tail.isalnum() else ""

    @staticmethod
    def _archive_pic_rows(pics) -> list[dict]:
        rows = []
        for pic in pics or []:
            if not isinstance(pic, dict):
                continue
            pid = str(pic.get("pid") or "").strip()
            if not pid:
                continue
            rows.append(
                {
                    "pid": pid,
                    "url": str(pic.get("url") or ""),
                    "alt_url": str(pic.get("alt_url") or ""),
                    "ext": _archive_safe_ext(pic.get("ext")),
                    "kind": "live" if str(pic.get("kind") or "") == "live" else "pic",
                }
            )
        return rows

    @staticmethod
    def _archive_media_items(pics) -> list[dict]:
        items = []
        for idx, pic in enumerate(pics or []):
            if not isinstance(pic, dict):
                continue
            pid = str(pic.get("pid") or "").strip()
            if not pid:
                continue
            items.append(
                {
                    "pid": pid,
                    "url": str(pic.get("url") or ""),
                    "alt_url": str(pic.get("alt_url") or ""),
                    "ext": _archive_safe_ext(pic.get("ext")),
                    "kind": "live" if str(pic.get("kind") or "") == "live" else "image",
                    "idx": idx,
                }
            )
        return items

    @staticmethod
    def _archive_post_row(post: dict, mid: str, uid: str, source: str) -> dict:
        try:
            raw_json = json.dumps(post, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_json = ""
        return {
            "mid": mid,
            "uid": uid,
            "author": str(post.get("author") or ""),
            "bid": str(
                post.get("bid") or ArchiveMixin._archive_bid_from_link(post.get("link")) or ""
            ),
            "link": str(post.get("link") or ""),
            "created_at": _archive_int(post.get("created_ts"), 0),
            "created_raw": str(post.get("created_raw") or post.get("time") or ""),
            "text": str(post.get("text") or ""),
            "text_raw": str(post.get("raw_text") or post.get("text_raw") or ""),
            # P2-4：text 是否已经是长文全文，默认 0；只有补全真的换过正文才置 1。
            "text_full": 1 if post.get("text_full") else 0,
            "is_long": 1 if post.get("is_long") else 0,
            "is_retweet": 1 if post.get("is_retweet") else 0,
            "orig_text": str(post.get("orig_text") or ""),
            # 时间线卡片缺 pic_num 时解析侧给 0（不等于"自述 0 张"）。有图却
            # 自述 0 与库内"未知=-1"哨兵冲突，会让对账把每张带图帖报成缺图。
            "pic_total": (
                -1
                if _archive_int(post.get("pic_total"), -1) <= 0 and post.get("pics")
                else _archive_int(post.get("pic_total"), -1)
            ),
            "video_src": str(post.get("video_src") or ""),
            "raw_json": raw_json,
            "source": source,
        }

    # ------------------------------------------------------------------
    # 增量归档
    # ------------------------------------------------------------------
    async def archive_post(self, post: dict, *, source: str = "monitor") -> bool:
        """同步落账一条帖（upsert posts + media），并按需入队原图；失败返回 False。"""
        # 增量归档也要补全长文：推送侧的 text 默认截到 100 字，直接落账会让
        # 文案库永久缺少全文（cleaned_at 一旦写入，后续清洗也不会再补）。
        if isinstance(post, dict) and post.get("is_long"):
            post = await self._archive_enrich_apply(post, force=False)
        ok, _queued = await self._archive_save_post(post, source)
        return ok

    async def _archive_save_post(self, post, source: str) -> tuple[bool, bool]:
        """落账实现：返回 (是否成功, 是否入队了下载)；重复 mid 幂等。"""
        store = self.archive_store()
        if store is None or not isinstance(post, dict):
            return False, False
        mid = str(post.get("id") or post.get("mid") or "").strip()
        uid = str(post.get("uid") or "").strip()
        if not mid or not uid:
            return False, False
        try:
            pics = [p for p in (post.get("pics") or []) if isinstance(p, dict)]
            existed = await asyncio.to_thread(store.has_post, mid)
            row = self._archive_post_row(post, mid, uid, source)
            # 只有首见才把图集交给 upsert：它会写入 pics_json，同时保留接口自述的
            # pic_total（自述图数 > 实际图数 = 对账缺口信号，不能被改成实际图数）。
            if not existed and pics:
                row["pics"] = self._archive_pic_rows(pics)
            await asyncio.to_thread(store.upsert_post, row)
            if pics:
                await asyncio.to_thread(store.upsert_media, mid, self._archive_media_items(pics))
                # 重复调用不带图（避免覆盖已有图集）；只有库里图集仍是空的（早年落账 /
                # 上次没拿到图信息）才补一次 set_pics。
                if existed and not (await asyncio.to_thread(store.get_post, mid) or {}).get("pics"):
                    await asyncio.to_thread(store.set_pics, mid, self._archive_pic_rows(pics))
            # 文案清洗（P5）：清洗结果写 cleaned_text，archive.db 即文案库（F3）；
            # 清洗失败只记日志，绝不影响归档与推送。copylib.auto_clean 关闭时
            # 留空 cleaned_at，等 /微博文案清洗 再批量处理。
            auto_clean = True
            group = self.config.get("copylib") if hasattr(self.config, "get") else None
            if isinstance(group, dict) and "auto_clean" in group:
                auto_clean = bool(group.get("auto_clean"))
            if auto_clean:
                try:
                    await asyncio.to_thread(self._archive_clean_post, store, mid, post)
                except Exception as e:
                    logger.warning(f"文案清洗失败（mid={mid}）: {e}")
            queued = False
            if pics and self._archive_bool("download_originals", True):
                queued = self.archive_enqueue_download(mid)
            # 整帖交给图床/向量管线：是否真入队由 pipeline 自己按开关与容量判断。
            # 用 getattr 探测而不是直接调用——单独跑微博域（没有 PipelineMixin 的
            # 测试桩）时这里必须静默跳过。
            notify = getattr(self, "pipeline_notify_archived", None)
            if callable(notify):
                try:
                    notify(mid)
                except Exception as e:  # 管线入队失败绝不能反过来让归档失败
                    logger.warning(f"图床管线入队失败（mid={mid}）: {e}")
            return True, queued
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"归档落账失败（mid={mid}）: {e}")
            return False, False

    @staticmethod
    def _archive_clean_post(store: ArchiveStore, mid: str, post: dict) -> None:
        """清洗一条帖子正文并落库（cleaned_text + cleaned_at）；纯图/纯链接帖记为已清洗。"""
        try:
            from .cleaner import clean_mblog_text
        except ImportError:  # 清洗模块缺失（老包/裁剪包）时归档保持可用
            logger.warning("文案清洗模块不可用，跳过 cleaned_text 落库")
            return
        text = str(post.get("text") or "")
        cleaned = clean_mblog_text(text)
        store.mark_post_cleaned(mid, cleaned)

    async def archive_enrich(self, post: dict) -> dict:
        """L2 补全：长文正文 / 图集 pic_ids；只在疑似需要时触发，失败原样返回。"""
        return await self._archive_enrich_apply(post, force=False)

    async def _archive_enrich_apply(self, post, force: bool) -> dict:
        if not isinstance(post, dict):
            return post
        mid = str(post.get("id") or post.get("mid") or "").strip()
        try:
            text = str(post.get("text") or "")
            pics = [p for p in (post.get("pics") or []) if isinstance(p, dict)]
            pic_total = _archive_int(post.get("pic_total"), -1)
            is_long = bool(post.get("is_long"))
            # 长文一律补全：text_max_length 默认 100，按">=140 或含全文"判定
            # 永远不成立，归档库里就只剩摘要，清洗后也无法恢复全文。
            # force=True（/微博回填 --full）绕过 is_long 这类启发式：字段缺失时
            # 也要真的去拉一次长文，否则 --full 就是空操作（历史 bug）。
            need_text = bool(is_long) or force
            # 图集补全不跟随 force：_ensure_full_pics 自己会判定要不要补，
            # 无条件调用等于给每条旧帖多花一次详情请求。
            need_pics = pic_total > len(pics)
            if need_text and mid:
                full = await self._archive_long_text(mid)
                # 只在真拿到更完整正文时替换；force 允许等长覆盖（截断形态可能
                # 不同），但永不把已有正文改短。
                if full and (len(full) > len(text) or (force and len(full) >= len(text))):
                    post["text"] = full
                    post["text_full"] = 1
            if need_pics:
                ensure = getattr(self, "_ensure_full_pics", None)
                if callable(ensure):
                    await ensure(post)
            return post
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"归档补全失败（mid={mid}）: {e}")
            return post

    async def _archive_long_text(self, mid: str) -> str | None:
        """取长文全文：优先 weibo.com/ajax/statuses/longtext，失败回落 _fetch_long_text。"""
        mid = str(mid or "").strip()
        if not mid:
            return None
        api_get = getattr(self, "_api_get", None)
        if callable(api_get):
            try:
                payload = await api_get(ARCHIVE_LONGTEXT_URL, {"id": mid})
                text = _archive_longtext_of(payload)
                if text:
                    return _clean_text(text, 0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 首选接口失效是可诊断故障（host 白名单、接口改版、风控），
                # 不能像以前那样只打 debug —— 那等于把一条永久失效的链路
                # 藏起来，只剩回落接口在默默兜底。同一次运行只告警一次，
                # 避免每篇长文刷屏。
                if not getattr(self, "_archive_longtext_warned", False):
                    self._archive_longtext_warned = True
                    logger.warning(f"归档长文首选接口不可用（后续静默，仍走回落接口）: {e}")
        fetch = getattr(self, "_fetch_long_text", None)
        if callable(fetch):
            try:
                text = await fetch(mid)
                if text:
                    return text
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"归档长文回落接口失败（{mid}）: {e}")
        return None

    # ------------------------------------------------------------------
    # 原图下载 worker
    # ------------------------------------------------------------------
    async def _archive_worker(self) -> None:
        """单 worker 串行消费 mid 队列；每项必须 task_done，取消即退出。"""
        queue = self._archive_queue()
        while True:
            try:
                mid = await queue.get()
            except asyncio.CancelledError:
                raise
            if mid is _ARCHIVE_STOP:  # 关闭哨兵：当前已无在途任务
                with contextlib.suppress(Exception):
                    queue.task_done()
                return
            try:
                await self.archive_download_post(str(mid))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"归档原图下载失败（mid={mid}）: {e}")
            finally:
                with contextlib.suppress(Exception):
                    queue.task_done()
                # 队列腾出空位就把"满了被拒"的 mid 重投回来
                with contextlib.suppress(Exception):
                    self._archive_drain_retry(queue)

    def archive_enqueue_download(self, mid: str) -> bool:
        """入队一个 mid 的原图下载；未就绪 / 队列满返回 False，不抛。

        队列满时不再只写一条 debug 就丢掉：被丢的帖 media 会永远停在
        `pending`，只有跑对账才看得见。这里退回一个**有界待办集合**，
        worker 空闲时重投，保证"满了"不等于"丢了"。
        """
        if not self.archive_ready():
            return False
        try:
            queue = self._archive_queue()
            try:
                queue.put_nowait(str(mid))
                return True
            except asyncio.QueueFull:
                pending = self._archive_pending_retry()
                if len(pending) >= ARCHIVE_PENDING_RETRY_MAX:
                    pending.popitem(last=False)
                pending[str(mid)] = None
                logger.warning(
                    "归档下载队列已满，已转入待办（当前 %d 条，mid=%s）",
                    len(pending),
                    mid,
                )
                return False
        except Exception as e:
            logger.warning(f"归档入队失败（mid={mid}）: {e}")
            return False

    def _archive_pending_retry(self):
        """队列满时暂存 mid 的有序集合（FIFO，满了丢最旧的）。"""
        pending = getattr(self, "_archive_retry_mids", None)
        if pending is None:
            from collections import OrderedDict

            pending = OrderedDict()
            self._archive_retry_mids = pending
        return pending

    def _archive_drain_retry(self, queue: asyncio.Queue) -> int:
        """把待办里的 mid 重投进队列，返回本次重投数（队列满就停）。"""
        pending = self._archive_pending_retry()
        moved = 0
        while pending:
            if queue.full():
                break
            mid, _ = pending.popitem(last=False)
            try:
                queue.put_nowait(mid)
                moved += 1
            except asyncio.QueueFull:
                pending[mid] = None
                break
        return moved

    async def archive_download_post(self, mid: str) -> dict:
        """把一条帖里未落盘的图逐张下载到 archive/<uid>/<mid>/。"""
        result = {"mid": str(mid), "downloaded": 0, "failed": 0}
        store = self.archive_store()
        media_root = self._archive_media_root()
        if store is None or media_root is None:
            return result
        mid = str(mid or "").strip()
        if not mid:
            return result
        post = await asyncio.to_thread(store.get_post, mid)
        uid = str((post or {}).get("uid") or "unknown")
        folder = media_root / _archive_safe_seg(uid, "unknown") / _archive_safe_seg(mid, "mid")
        rows = await asyncio.to_thread(store.list_media, mid) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("pid") or "").strip()
            if not pid:
                continue
            local = str(row.get("local_path") or "")
            state = str(row.get("state") or "pending")
            if state != "pending" and local and Path(local).is_file():
                continue
            # 磁盘余量保护：单图上限 30MB、单帖最多 ~18 张、回填队列 500 个 mid，
            # 一条回填链就能写到上百 GB。写满盘会连带 WAL/state.json/AstrBot 一起挂，
            # 失败点离原因很远——所以在这里主动刹车（与相册链路同一套口径）。
            free_bytes = await asyncio.to_thread(_archive_free_bytes, media_root)
            if free_bytes is None:
                # 量不到余量不等于写满了：放行，但不能假装自己检查过。
                if not getattr(self, "_archive_disk_unknown_warned", False):
                    self._archive_disk_unknown_warned = True
                    logger.warning("取不到归档目录的磁盘余量，本轮跳过余量保护（mid=%s）", mid)
            elif free_bytes < DISK_FLOOR_MB * 1048576:
                logger.warning(
                    "归档原图下载已暂停：磁盘仅剩 %dMB（下限 %dMB，mid=%s）",
                    free_bytes // 1048576,
                    DISK_FLOOR_MB,
                    mid,
                )
                result["paused"] = "disk"
                return result
            dest = folder / f"{_archive_safe_seg(pid, 'pic')}.{_archive_safe_ext(row.get('ext'))}"
            pic = {
                "pid": pid,
                "url": str(row.get("url") or ""),
                "alt_url": str(row.get("alt_url") or ""),
                "ext": _archive_safe_ext(row.get("ext")),
                "kind": str(row.get("kind") or "image"),
                "video_url": "",
            }
            try:
                await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                saved = await self._download_pic(pic, dest)
                size = await asyncio.to_thread(
                    lambda p=saved: p.stat().st_size if p.is_file() else 0
                )
                await asyncio.to_thread(
                    store.mark_media,
                    mid,
                    pid,
                    "downloaded",
                    local_path=str(saved or dest),
                    size=size,
                )
                result["downloaded"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"归档原图下载失败（mid={mid}, pid={pid}）: {e}")
                result["failed"] += 1
                await asyncio.to_thread(store.bump_media_attempt, mid, pid, str(e)[:200])
        return result

    def archive_local_media(self, mid: str, pid: str) -> Path | None:
        """同步查本地原图；未就绪或未落盘返回 None（调用方静默回退远端）。"""
        store = self.archive_store()
        if store is None:
            return None
        try:
            return store.media_file(str(mid or ""), str(pid or ""))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 全量回填
    # ------------------------------------------------------------------
    def _archive_since_ts(self, since: str) -> int:
        """把 `since` 解析成时间戳；非法输入**不**回落"全量"。

        曾经解析失败会 warning 后返回 0，于是 `--since 2024` 这种少写一位的输入
        会静默变成 1000 页、每页 3s 的全量回填，跑几小时并触发风控——用户通常
        还不知道是自己打错了。这里抛 `ValueError`，由指令层回显错误。
        """
        raw = str(since or "").strip()
        if not raw:
            raw = str(self._archive_group().get("since") or "").strip()
        if not raw:
            return 0
        try:
            day = datetime.strptime(raw[:10], "%Y-%m-%d")
            return int(day.replace(tzinfo=_TZ_CN).timestamp())
        except (TypeError, ValueError) as e:
            raise ValueError(f"since 需形如 YYYY-MM-DD（收到 {raw!r}）") from e

    def archive_backfill_stop(self) -> None:
        """请求停止：当前页处理完后退出。"""
        self._backfill_stop = True

    def archive_backfill_running(self) -> bool:
        if getattr(self, "_backfill_active", False):
            return True
        task = getattr(self, "_backfill_task", None)
        return task is not None and not task.done()

    async def archive_backfill(self, uid: str, *, since: str = "", full: bool = False) -> dict:
        """逐页翻时间线 upsert by mid；游标断点续跑，风控/异常只暂停不抛。"""
        uid = str(uid or "").strip()
        if not uid or not uid.isdigit() or len(uid) > 11:
            return {"error": "uid 不合法"}
        store = self.archive_store()
        if store is None:
            return {"error": "归档未启用"}
        current = asyncio.current_task()
        running = getattr(self, "_backfill_task", None)
        if running is not None and running is not current and not running.done():
            return {"error": "已有回填任务在跑"}

        stats = {
            "uid": uid,
            "pages": 0,
            "fetched": 0,
            "inserted": 0,
            "existing": 0,
            "skipped_since": 0,
            "downloaded_queued": 0,
            "stopped": "",
            "next_page": 1,
            "status": "error",
            "elapsed": 0.0,
        }
        try:
            since_ts = self._archive_since_ts(since)
        except ValueError as e:
            return {"error": str(e), "status": "error"}
        delay = max(0.0, self._archive_float("page_delay", 3.0))
        page_limit = max(1, _archive_int(BACKFILL_SAFETY_MAX_PAGES, 1000))
        self._backfill_stop = False
        self._backfill_active = True
        if current is not None:
            self._backfill_task = current
        page = 1
        oldest = 0
        started = time.monotonic()
        stats["next_page"] = page
        try:
            cursor = await asyncio.to_thread(store.get_cursor, uid)
            # running / paused / error 都保留断点：风控暂停与抓取失败后重跑必须
            # 从失败页继续，否则每页都要重抓，反而更容易再次触发风控。
            if cursor and str(cursor.get("status") or "") not in ("", "done"):
                page = max(1, _archive_int(cursor.get("next_page"), 1))
                oldest = _archive_int(cursor.get("oldest_created_at"), 0)
                stats["next_page"] = page
            while True:
                if self._backfill_stop:
                    stats["stopped"] = "paused"
                    stats["status"] = "paused"
                    break
                try:
                    posts = await self._fetch_timeline_page(uid, page)
                except WeiboRiskError as e:
                    logger.warning(f"回填触发风控，已暂停（uid={uid}, page={page}）: {e}")
                    stats["stopped"] = "risk"
                    stats["status"] = "paused"
                    await asyncio.to_thread(store.set_cursor, uid, page, oldest, "paused")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"回填抓取失败（uid={uid}, page={page}）: {e}")
                    stats["stopped"] = "error"
                    stats["status"] = "error"
                    await asyncio.to_thread(store.set_cursor, uid, page, oldest, "error")
                    break
                posts = posts or []
                stats["pages"] = page
                stats["fetched"] += len(posts)
                if not posts:
                    stats["stopped"] = "end"
                    stats["status"] = "done"
                    await asyncio.to_thread(store.clear_cursor, uid)
                    break
                created_list = [
                    _archive_int(p.get("created_ts"), 0) for p in posts if isinstance(p, dict)
                ]
                created_list = [ts for ts in created_list if ts]
                page_oldest = min(created_list) if created_list else 0
                if page_oldest:
                    oldest = page_oldest
                for post in posts:
                    if not isinstance(post, dict):
                        continue
                    created = _archive_int(post.get("created_ts"), 0)
                    if since_ts and created and created < since_ts:
                        stats["skipped_since"] += 1
                        continue
                    mid = str(post.get("id") or post.get("mid") or "").strip()
                    if not mid:
                        continue
                    if await asyncio.to_thread(store.has_post, mid):
                        stats["existing"] += 1
                        continue
                    await self._archive_enrich_apply(post, force=full)
                    ok, queued = await self._archive_save_post(post, "backfill")
                    if ok:
                        stats["inserted"] += 1
                        if queued:
                            stats["downloaded_queued"] += 1
                next_page = page + 1
                stats["next_page"] = next_page
                await asyncio.to_thread(store.set_cursor, uid, next_page, oldest, "running")
                if since_ts and oldest and oldest < since_ts:
                    stats["stopped"] = "since"
                    stats["status"] = "done"
                    await asyncio.to_thread(store.clear_cursor, uid)
                    break
                if page >= page_limit:
                    logger.warning(f"回填到达安全页上限 {page_limit}，已暂停（uid={uid}）")
                    stats["stopped"] = "page-limit"
                    stats["status"] = "paused"
                    break
                page = next_page
                if delay > 0:
                    await asyncio.sleep(delay)
        finally:
            self._backfill_active = False
            if getattr(self, "_backfill_task", None) is current:
                self._backfill_task = None
            stats["elapsed"] = round(time.monotonic() - started, 2)
            # next_page 保留循环里最后一次写入的"下一页"，不要用当前页覆盖
            self._backfill_last_stats = dict(stats)
            logger.info(
                "回填结束 uid=%s pages=%s inserted=%s existing=%s stopped=%s",
                uid,
                stats["pages"],
                stats["inserted"],
                stats["existing"],
                stats["stopped"],
            )
        return stats

    # ------------------------------------------------------------------
    # JSON 种子导入
    # ------------------------------------------------------------------
    def _archive_find_file(self, path):
        """把用户给的路径解析成一个可读的种子文件，越界一律拒绝。

        这是管理员命令的入参，但"管理员"不等于"可以指着机器任意文件让 bot
        读进来归档"。允许的范围收敛为：数据目录内、插件目录内、以及当前
        工作目录（相对路径的老用法），其余绝对路径直接拒绝。
        """
        raw = str(path or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        roots: list[Path] = []
        base = getattr(self, "_data_dir", None)
        if base:
            roots.append(Path(base))
        roots.append(Path(__file__).resolve().parent.parent)
        roots.append(Path.cwd())

        def _inside(target: Path) -> Path | None:
            try:
                resolved = target.resolve()
            except OSError:
                return None
            for root in roots:
                try:
                    resolved.relative_to(root.resolve())
                except (OSError, ValueError):
                    continue
                return resolved if resolved.is_file() else None
            return None

        if candidate.is_absolute():
            return _inside(candidate)
        for root in roots:
            found = _inside(root / raw)
            if found is not None:
                return found
        return None

    @staticmethod
    def _archive_import_entries(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("posts", "data", "weibo"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return None

    @staticmethod
    def _archive_import_meta(file: Path) -> dict:
        """同目录 <文件名前缀数字>.meta.json 若存在则返回其内容。"""
        match = re.match(r"(\d+)", file.name)
        if not match:
            return {}
        meta_path = file.with_name(f"{match.group(1)}.meta.json")
        if not meta_path.is_file():
            return {}
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    async def archive_import_json(self, path: str) -> dict:
        """导入离线抓取的 JSON 种子（另一项目的导出格式）；失败返回 {"error":...}。"""
        store = self.archive_store()
        if store is None:
            return {"error": "归档未启用"}
        file = self._archive_find_file(path)
        if file is None:
            return {"error": f"文件不存在: {path}"}
        try:
            # 64MB 种子的读盘与 JSON 解析都放到线程，避免卡住轮询与推送
            text = await asyncio.to_thread(file.read_text, encoding="utf-8-sig")
            data = await asyncio.to_thread(json.loads, text)
        except (OSError, ValueError) as e:
            return {"error": f"读取失败: {e}"}
        entries = self._archive_import_entries(data)
        if entries is None:
            return {"error": "顶层不是列表，也没有 posts/data/weibo 列表"}
        meta = await asyncio.to_thread(self._archive_import_meta, file)
        uid = ""
        if isinstance(data, dict):
            uid = str(data.get("uid") or "").strip()
        if not uid:
            uid = str(meta.get("uid") or "").strip()
        if not uid:
            match = re.match(r"(\d+)", file.name)
            uid = match.group(1) if match else ""
        if not uid:
            return {"error": "无法判定 uid"}
        author_default = str(meta.get("screenName") or meta.get("screen_name") or "")
        stats = {
            "file": str(file),
            "uid": uid,
            "total": len(entries),
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "media_rows": 0,
        }
        for entry in entries:
            if not isinstance(entry, dict):
                stats["skipped"] += 1
                continue
            mid = str(entry.get("id") or entry.get("mid") or entry.get("post_id") or "").strip()
            if not mid:
                stats["skipped"] += 1
                continue
            try:
                stats["media_rows"] += await self._archive_import_one(
                    store, entry, mid, uid, author_default, stats
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"导入条目失败（mid={mid}）: {e}")
                stats["skipped"] += 1
        logger.info(
            "归档 JSON 导入完成 file=%s uid=%s total=%s inserted=%s updated=%s",
            file.name,
            uid,
            stats["total"],
            stats["inserted"],
            stats["updated"],
        )
        return stats

    async def _archive_import_one(
        self, store, entry: dict, mid: str, uid: str, author: str, stats: dict
    ) -> int:
        bid = str(entry.get("mb") or entry.get("bid") or "")
        created_raw = str(entry.get("c") or entry.get("created_at") or "")
        if entry.get("created_ts") not in (None, ""):
            created_at = _archive_int(entry.get("created_ts"), 0)
        else:
            created_at = int(_parse_created_epoch(created_raw) or 0)
        pics = [p for p in (entry.get("pics") or []) if isinstance(p, dict)]
        row = {
            "mid": mid,
            "uid": uid,
            "author": str(entry.get("author") or entry.get("screen_name") or author or ""),
            "bid": bid,
            "link": str(entry.get("link") or (f"https://weibo.com/{uid}/{bid}" if bid else "")),
            "created_at": created_at,
            "created_raw": created_raw,
            "text": str(entry.get("t") or entry.get("text") or ""),
            "text_raw": str(entry.get("text_raw") or ""),
            "is_long": 1 if (entry.get("lt") or entry.get("is_long")) else 0,
            "is_retweet": 1 if (entry.get("rt") or entry.get("is_retweet")) else 0,
            "orig_text": str(entry.get("orig_text") or ""),
            "pic_total": _archive_int(entry.get("pic_total"), -1),
            "video_src": str(entry.get("video_src") or ""),
            "raw_json": json.dumps(entry, ensure_ascii=False, default=str),
            "source": "json",
        }
        existed = await asyncio.to_thread(store.has_post, mid)
        if not existed and pics:
            # 首见才带图集：pic_total 保持 -1（未知）而不是被改写成实际图数。
            row["pics"] = self._archive_pic_rows(pics)
        await asyncio.to_thread(store.upsert_post, row)
        media_rows = 0
        if pics:
            media_rows = await asyncio.to_thread(
                store.upsert_media, mid, self._archive_media_items(pics)
            )
            if existed and not (await asyncio.to_thread(store.get_post, mid) or {}).get("pics"):
                await asyncio.to_thread(store.set_pics, mid, self._archive_pic_rows(pics))
        stats["updated" if existed else "inserted"] += 1
        return media_rows

    # ------------------------------------------------------------------
    # 查询与对账
    # ------------------------------------------------------------------
    async def archive_query(self, uid: str = "", *, limit: int = 10, offset: int = 0) -> list[dict]:
        store = self.archive_store()
        if store is None:
            return []
        try:
            limit = max(1, min(50, _archive_int(limit, 10)))
            offset = max(0, _archive_int(offset, 0))
            return (
                await asyncio.to_thread(
                    store.list_posts, str(uid or "").strip() or None, limit=limit, offset=offset
                )
                or []
            )
        except Exception as e:
            logger.warning(f"归档查询失败: {e}")
            return []

    async def archive_reconcile(self, uid: str = "") -> dict:
        """对账：账本 vs 期望（正文/自述图数/原图落盘）并给出缺口明细。"""
        store = self.archive_store()
        scope = str(uid or "").strip()
        if store is None:
            return {"uid": scope, "error": "归档未启用", "ok": False}
        try:
            return await asyncio.to_thread(self._archive_reconcile_sync, store, scope or None)
        except Exception as e:
            logger.warning(f"归档对账失败: {e}")
            return {"uid": scope, "error": str(e), "ok": False}

    def _archive_reconcile_sync(self, store, uid) -> dict:
        counts = store.reconcile_counts(uid) or {}
        mstats = store.media_stats(uid) or {}
        gap_keys = ("post_no_text", "pic_count_mismatch", "media_not_downloaded")
        gap_counts = dict.fromkeys(gap_keys, 0)
        buckets = {key: [] for key in gap_keys}
        seen = {"posts": 0, "posts_with_text": 0, "posts_with_media": 0}
        scanned = 0
        anchor: tuple[int, str] | None = None
        # 键集分页而不是 OFFSET：监控侧一直在插入新帖，OFFSET 会让整页后移、
        # 漏扫若干行，gap_counts 偏小从而给出"全部通过"的假结论。
        keyset = getattr(store, "iter_posts_keyset", None)
        while scanned < ARCHIVE_SCAN_LIMIT:
            if callable(keyset):
                batch = keyset(uid, after=anchor, limit=ARCHIVE_SCAN_PAGE) or []
            else:  # 兼容旧存储实现（测试替身等）
                batch = store.list_posts(uid, limit=ARCHIVE_SCAN_PAGE, offset=scanned) or []
            if not batch:
                break
            scanned += len(batch)
            with_anchor = batch[-1]
            anchor = (
                _archive_int(with_anchor.get("created_at"), 0),
                str(with_anchor.get("mid") or ""),
            )
            # 一页的媒体行一次查完（P2-3）：逐帖 list_media 会让 5000 上限的
            # 全库对账变成 5000 次 SQL，每页 200 次同步查询还会长时间占着
            # store 的全局锁，把监控侧的写入一起堵住。
            bulk_fn = getattr(store, "list_media_bulk", None)
            media_map = (
                bulk_fn([str(p.get("mid") or "") for p in batch if p.get("mid")]) or {}
                if callable(bulk_fn)
                else {}
            )
            for post in batch:
                seen["posts"] += 1
                mid = str(post.get("mid") or "")
                text = str(post.get("text") or "")
                if text.strip():
                    seen["posts_with_text"] += 1
                else:
                    gap_counts["post_no_text"] += 1
                    if len(buckets["post_no_text"]) < ARCHIVE_GAP_KEEP:
                        buckets["post_no_text"].append(
                            {"kind": "post_no_text", "mid": mid, "detail": "正文为空"}
                        )
                if callable(bulk_fn):
                    media = media_map.get(mid) or []
                else:  # 兼容旧存储实现（测试替身等）
                    media = store.list_media(mid) or []
                if media:
                    seen["posts_with_media"] += 1
                pic_total = _archive_int(post.get("pic_total"), -1)
                if pic_total >= 0 and pic_total != len(media):
                    gap_counts["pic_count_mismatch"] += 1
                    if len(buckets["pic_count_mismatch"]) < ARCHIVE_GAP_KEEP:
                        buckets["pic_count_mismatch"].append(
                            {
                                "kind": "pic_count_mismatch",
                                "mid": mid,
                                "detail": f"自述 {pic_total} 张，实际 {len(media)} 张",
                            }
                        )
                for row in media:
                    if str(row.get("state") or "pending") != "pending":
                        continue
                    gap_counts["media_not_downloaded"] += 1
                    if len(buckets["media_not_downloaded"]) < ARCHIVE_GAP_KEEP:
                        buckets["media_not_downloaded"].append(
                            {
                                "kind": "media_not_downloaded",
                                "mid": mid,
                                "pid": str(row.get("pid") or ""),
                                "detail": "原图未落盘",
                            }
                        )
            if len(batch) < ARCHIVE_SCAN_PAGE:
                break
        merged = [
            *buckets["post_no_text"],
            *buckets["pic_count_mismatch"],
            *buckets["media_not_downloaded"],
        ]
        gaps = merged[:ARCHIVE_GAP_LIMIT]
        posts = _archive_int(counts.get("posts"), seen["posts"])
        posts_with_text = _archive_int(counts.get("posts_with_text"), seen["posts_with_text"])
        posts_without_text = _archive_int(counts.get("posts_without_text"), 0) or max(
            0, posts - posts_with_text
        )
        report = {
            "uid": str(uid or ""),
            "posts": posts,
            "posts_with_text": posts_with_text,
            "posts_without_text": posts_without_text,
            "posts_with_media": _archive_int(
                counts.get("posts_with_media"), seen["posts_with_media"]
            ),
            "media_expected": _archive_int(
                counts.get("media_expected"), _archive_int(counts.get("pic_total_sum"), 0)
            ),
            "media_rows": _archive_int(
                counts.get("media_rows"), _archive_int(mstats.get("total"), 0)
            ),
            "media_missing_url": _archive_int(counts.get("media_missing_url"), 0),
            "downloaded": _archive_int(
                counts.get("downloaded"), _archive_int(mstats.get("downloaded"), 0)
            ),
            "album": _archive_int(counts.get("album"), _archive_int(mstats.get("album"), 0)),
            "bed": _archive_int(counts.get("bed"), _archive_int(mstats.get("bed"), 0)),
            "thumb": _archive_int(counts.get("thumb"), _archive_int(mstats.get("thumb"), 0)),
            "vector": _archive_int(counts.get("vector"), _archive_int(mstats.get("vector"), 0)),
            "gaps": gaps,
            "gap_counts": gap_counts,
            # 对账只扫前 ARCHIVE_SCAN_LIMIT 帖；超出时必须显式标注，不能把
            # "没扫到"当成"没有缺口"报告全绿。
            "scanned": seen["posts"],
            "partial": scanned >= ARCHIVE_SCAN_LIMIT,
            "ok": not any(gap_counts.values()) and not (scanned >= ARCHIVE_SCAN_LIMIT),
            # 同一页内容判断用满扫描上限：键集分页下 scanned 是已扫行数
            "scan_limit": ARCHIVE_SCAN_LIMIT,
        }
        report["uploaded_bed"] = _archive_int(counts.get("uploaded_bed"), report["bed"])
        report["thumbnail"] = _archive_int(counts.get("thumbnail"), report["thumb"])
        report["vectorized"] = _archive_int(counts.get("vectorized"), report["vector"])
        return report

    # ------------------------------------------------------------------
    # 文本格式化（纯函数，命令与面板共用）
    # ------------------------------------------------------------------
    @staticmethod
    def format_reconcile(report: dict) -> str:
        if not isinstance(report, dict):
            return "对账：无数据"
        if report.get("error"):
            return f"对账失败：{report['error']}"
        scope = report.get("uid") or "全库"
        counts = report.get("gap_counts") or {}
        gap_total = sum(_archive_int(v, 0) for v in counts.values())
        lines = [
            f"对账（{scope}）：{_archive_int(report.get('posts'), 0)} 帖 / "
            f"有正文 {_archive_int(report.get('posts_with_text'), 0)} / "
            f"缺正文 {_archive_int(report.get('posts_without_text'), 0)} / "
            f"有图 {_archive_int(report.get('posts_with_media'), 0)} 帖",
            f"媒体：自述 {_archive_int(report.get('media_expected'), 0)} 张 / "
            f"账本 {_archive_int(report.get('media_rows'), 0)} 行 / "
            f"缺直链 {_archive_int(report.get('media_missing_url'), 0)}",
            f"进度：已落盘 {_archive_int(report.get('downloaded'), 0)} / "
            f"群相册 {_archive_int(report.get('album'), 0)} / "
            f"图床 {_archive_int(report.get('bed'), 0)} / "
            f"缩略图 {_archive_int(report.get('thumb'), 0)} / "
            f"向量 {_archive_int(report.get('vector'), 0)}",
        ]
        if report.get("partial"):
            lines.append(
                f"注意：本次仅扫描前 {_archive_int(report.get('scanned'), 0)} 帖，"
                "更大范围的缺口未覆盖"
            )
        labels = {
            "post_no_text": "缺正文",
            "pic_count_mismatch": "图数不符",
            "media_not_downloaded": "原图未落盘",
        }
        gaps = report.get("gaps") or []
        for kind, label in labels.items():
            total = _archive_int(counts.get(kind), 0)
            items = [g for g in gaps if isinstance(g, dict) and g.get("kind") == kind]
            if not total and not items:
                continue
            lines.append(f"缺口·{label}：{total} 处")
            for gap in items[:5]:
                mid = str(gap.get("mid") or "")
                pid = str(gap.get("pid") or "")
                head = f"  · {mid}/{pid}" if pid else f"  · {mid}"
                detail = str(gap.get("detail") or "")
                lines.append(f"{head} {detail}".rstrip())
            rest = total - min(len(items), 5)
            if rest > 0:
                lines.append(f"  … 另有 {rest} 条同类缺口")
        if gap_total:
            lines.append(f"结论：存在 {gap_total} 处缺口")
        else:
            lines.append("结论：一致")
        return "\n".join(lines)

    @staticmethod
    def format_backfill(stats: dict) -> str:
        if not isinstance(stats, dict):
            return "回填：无数据"
        if stats.get("error"):
            return f"回填失败：{stats['error']}"
        reasons = {
            "end": "翻到底",
            "since": "到达 since 边界",
            "risk": "触发风控暂停",
            "error": "抓取异常中断",
            "page-limit": "到达安全页上限",
            "paused": "手动停止",
        }
        status_cn = {"done": "完成", "paused": "已暂停", "error": "异常中断"}.get(
            str(stats.get("status") or ""), str(stats.get("status") or "未知")
        )
        reason = reasons.get(str(stats.get("stopped") or ""), str(stats.get("stopped") or "-"))
        return "\n".join(
            [
                f"回填 {stats.get('uid') or '-'}：{status_cn}（{reason}）/ "
                f"{_archive_int(stats.get('pages'), 0)} 页 / "
                f"抓取 {_archive_int(stats.get('fetched'), 0)} 条 / "
                f"新增 {_archive_int(stats.get('inserted'), 0)} / "
                f"已有 {_archive_int(stats.get('existing'), 0)} / "
                f"跳过 {_archive_int(stats.get('skipped_since'), 0)} / "
                f"入队下载 {_archive_int(stats.get('downloaded_queued'), 0)}",
                f"下一页 {_archive_int(stats.get('next_page'), 1)}，"
                f"用时 {stats.get('elapsed') or 0}s",
            ]
        )

    @staticmethod
    def format_import(stats: dict) -> str:
        if not isinstance(stats, dict):
            return "导入：无数据"
        if stats.get("error"):
            return f"导入失败：{stats['error']}"
        return (
            f"导入 {stats.get('file') or '-'}：uid {stats.get('uid') or '-'} / "
            f"共 {_archive_int(stats.get('total'), 0)} 条 / "
            f"新增 {_archive_int(stats.get('inserted'), 0)} / "
            f"更新 {_archive_int(stats.get('updated'), 0)} / "
            f"跳过 {_archive_int(stats.get('skipped'), 0)} / "
            f"图片行 {_archive_int(stats.get('media_rows'), 0)}"
        )
