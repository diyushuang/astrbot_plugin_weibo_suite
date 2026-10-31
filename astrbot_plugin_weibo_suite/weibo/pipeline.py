"""图床 / 缩略图 / 向量入库管线（P4 轨道 C）。

PipelineMixin 只做编排：图床（ImageBedClient）、向量库（VectorEngine）、图库
（ImageLibrary）、特征提取（compute_features_with_thumbnail）都由 Lead 在
initialize 时通过 pipeline_bind 注入，单测用 fake 替换。

纪律与边界：

- 管线是旁路能力：任何失败都只记日志 / 落错误字段，不得抛给归档、命令或 worker；
- 不做网络下载：原图一律读归档本地库，缺失时最多补调一次 archive_download_post；
- CFB 模式（bed_mode 为空或 cloudflare_imgbed）由图床侧钩子自动入库，
  插件侧只补原图落盘，绝不推进 bed/thumb/vector 状态；
- trace 域对象按需惰性导入（计划 §2.2：跨域调用只发生在 main.py 与 pipeline.py），
  模块顶层不 import trace，避免微博域单独可测性被拖垮。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .pipeline_naming import (
    NamingContext,
    category_map,
    naming_templates,
    render_directory,
    render_filename,
    resolve_category,
    thumbnail_folder,
    thumbnail_name,
    title_from_text,
)

try:  # 常量由 Lead 维护；万一尚未落地也不能让模块 import 崩掉。
    from .constants import MEDIA_STATES, PIPELINE_QUEUE_MAX, PIPELINE_STAGE_DIR_NAME
except ImportError:  # pragma: no cover - 仅在常量缺失的过渡期触发
    MEDIA_STATES = ("pending", "downloaded", "album", "bed", "thumb", "vector")
    PIPELINE_QUEUE_MAX = 500
    PIPELINE_STAGE_DIR_NAME = "pipeline_stage"

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 真实插件里一定存在
    logger = logging.getLogger(__name__)

_CFB_MODES = ("", "cloudflare_imgbed")
_VECTOR_RANK = MEDIA_STATES.index("vector") if "vector" in MEDIA_STATES else len(MEDIA_STATES) - 1
_WEIBO_TZ = timezone(timedelta(hours=8))
_BACKOFF_MIN = 5.0
_BACKOFF_MAX = 60.0
_TRUE_TEXTS = ("1", "true", "yes", "on")
_FALSE_TEXTS = ("0", "false", "no", "off", "")
_MEDIA_STATES = tuple(MEDIA_STATES)


@dataclass
class PipelineServices:
    """管线依赖集合；由 Lead 在 initialize 注入，None 字段表示该能力不可用。"""

    bed: Any = None
    vector: Any = None
    library: Any = None
    hash_size: int = 16
    bed_mode: str = ""


@dataclass(frozen=True)
class _PipelinePoint:
    """trace.vector_search.VectorPointInput 的同构兜底（trace 未就绪时使用）。"""

    path: str
    point_key: str
    image_url: str
    thumb_url: str = ""
    file_size: int = 0
    width: int | None = None
    height: int | None = None
    mime: str = ""
    src: str = ""


def _pipeline_int(value: Any, default: int) -> int:
    """宽容 int：None / 空串 / 非数字一律回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pipeline_bool(value: Any, default: bool) -> bool:
    """宽容 bool：面板里可能存成 "true" / "1" / "on"。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TEXTS:
            return True
        if text in _FALSE_TEXTS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _row_get(row: Any, key: str, default: Any = "") -> Any:
    """sqlite3.Row / dict 通用取值。"""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, default)
    return default if value is None else value


def _media_rank(row: Any) -> int:
    """媒体行的六态 rank；没有 state_rank 列时按 state 反推。"""
    raw = _row_get(row, "state_rank", None)
    if raw is not None:
        return _pipeline_int(raw, 0)
    state = str(_row_get(row, "state", "pending") or "pending")
    try:
        return _MEDIA_STATES.index(state)
    except ValueError:
        return 0


def _pipeline_date(timestamp: Any) -> str:
    """把帖子 created_at（秒级时间戳）转成命名用的 YYYY-MM-DD（微博东八区）。"""
    try:
        seconds = int(timestamp)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        try:
            return datetime.fromtimestamp(seconds, _WEIBO_TZ).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(_WEIBO_TZ).strftime("%Y-%m-%d")


def _pipeline_compute_features(
    path: str, hash_size: int, *, max_side: int, quality: int
) -> tuple[Any, str, bytes]:
    """一次解码出特征 + 缩略图；trace 域惰性导入（单测 monkeypatch 此函数）。"""
    from ..trace.features import compute_features_with_thumbnail

    return compute_features_with_thumbnail(path, hash_size, max_side=max_side, quality=quality)


def _pipeline_point_input(**fields: Any) -> Any:
    """构造向量点入参：优先真类，trace 未就绪时退回同构数据类。"""
    try:
        from ..trace.vector_search import VectorPointInput
    except Exception:  # pragma: no cover - 微博域可脱离 trace 单独运行
        return _PipelinePoint(**fields)
    return VectorPointInput(**fields)


class PipelineMixin:
    """单帖管线 + 串行队列 worker + 统计与补跑（P4-C）。"""

    # ---------- 绑定与开关 ----------

    def pipeline_bind(self, services: PipelineServices | None) -> None:
        """注入 / 清空管线依赖（initialize 调用一次；测试可随时替换）。"""
        self._pipeline_services = services
        if services is None:
            self._pipeline_last_error = ""

    def pipeline_services(self) -> PipelineServices | None:
        return getattr(self, "_pipeline_services", None)

    def _pipeline_group(self) -> dict:
        """读取 self.config 的 pipeline 节；缺失 / 非 dict 一律空 dict。"""
        config = getattr(self, "config", None)
        getter = getattr(config, "get", None)
        if not callable(getter):
            return {}
        try:
            group = getter("pipeline", None)
        except Exception:
            return {}
        return dict(group) if isinstance(group, dict) else {}

    def _pipeline_bool(self, key: str, default: bool) -> bool:
        return _pipeline_bool(self._pipeline_group().get(key), default)

    def _pipeline_int(self, key: str, default: int) -> int:
        return _pipeline_int(self._pipeline_group().get(key), default)

    def _pipeline_enabled_uids(self) -> set[str]:
        """enabled_uids 白名单；空集合 = 全部博主。"""
        raw = self._pipeline_group().get("enabled_uids")
        if isinstance(raw, str):
            items = [part for part in raw.replace(",", " ").split() if part]
        elif isinstance(raw, (list, tuple, set)):
            items = [str(part) for part in raw]
        else:
            items = []
        return {item.strip() for item in items if item.strip()}

    def _pipeline_store(self) -> Any:
        getter = getattr(self, "archive_store", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def pipeline_enabled(self) -> bool:
        """总开关：配置 pipeline.enabled 且归档就绪。"""
        if not self._pipeline_bool("enabled", False):
            return False
        ready = getattr(self, "archive_ready", None)
        if callable(ready):
            try:
                return bool(ready())
            except Exception:
                return False
        return self._pipeline_store() is not None

    def _pipeline_bed_mode(self) -> str:
        services = self.pipeline_services()
        return str(getattr(services, "bed_mode", "") or "").strip()

    def _pipeline_cfb(self) -> bool:
        return self._pipeline_bed_mode() in _CFB_MODES

    # ---------- 队列与 worker ----------

    def _pipeline_queue_ref(self) -> asyncio.Queue:
        queue = getattr(self, "_pipeline_queue", None)
        if queue is None:
            queue = asyncio.Queue(maxsize=PIPELINE_QUEUE_MAX)
            self._pipeline_queue = queue
        return queue

    def _pipeline_pending_ref(self) -> set[str]:
        pending = getattr(self, "_pipeline_pending", None)
        if pending is None:
            pending = set()
            self._pipeline_pending = pending
        return pending

    def _pipeline_worker_task(self) -> asyncio.Task | None:
        task = getattr(self, "_pipeline_task", None)
        if task is None or task.done():
            return None
        return task

    def pipeline_start(self) -> None:
        """建队列 + 起单 worker；幂等，initialize 调用（无事件循环时安全跳过）。"""
        self._pipeline_queue_ref()
        if self._pipeline_worker_task() is not None:
            self._pipeline_started = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("图床管线：当前无事件循环，跳过 worker 启动")
            return
        self._pipeline_started = True
        try:
            self._pipeline_task = loop.create_task(self._pipeline_worker(), name="weibo-pipeline")
        except Exception as exc:
            self._pipeline_started = False
            self._pipeline_last_error = str(exc)[:200]
            logger.warning(f"图床管线 worker 启动失败：{exc}")

    async def pipeline_stop(self) -> None:
        """停止 worker 并丢弃残留队列；幂等，可重复 await。"""
        self._pipeline_started = False
        task = getattr(self, "_pipeline_task", None)
        self._pipeline_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._pipeline_pending_ref().clear()
        queue = getattr(self, "_pipeline_queue", None)
        if queue is None:
            return
        while not queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                with contextlib.suppress(ValueError):
                    queue.task_done()

    def pipeline_enqueue(self, mid: str) -> bool:
        """把帖子放进串行队列；未启用 / 未启动 / 队列满 / 已排队 → False。"""
        mid = str(mid or "").strip()
        if not mid:
            return False
        try:
            if not self.pipeline_enabled():
                return False
        except Exception:
            return False
        if not getattr(self, "_pipeline_started", False):
            return False
        pending = self._pipeline_pending_ref()
        if mid in pending:
            return False
        queue = self._pipeline_queue_ref()
        if queue.full():
            return False
        pending.add(mid)
        try:
            queue.put_nowait(mid)
        except asyncio.QueueFull:
            pending.discard(mid)
            return False
        return True

    def pipeline_queue_size(self) -> int:
        queue = getattr(self, "_pipeline_queue", None)
        return queue.qsize() if queue is not None else 0

    def pipeline_notify_archived(self, mid: str) -> bool:
        """归档落库后的同步回调：满足条件则入队，任何异常都只记日志。

        archive.py 在 _archive_save_post 里直接同步调用（未 await），因此这里
        不能 await、不做网络。四个拒绝分支：未 bind 服务 / 未启用 / auto_queue
        关闭 / 该博主不在 enabled_uids；其余情况交给 pipeline_enqueue 去重。
        """
        mid = str(mid or "").strip()
        try:
            if not mid or self.pipeline_services() is None:
                return False
            if not self.pipeline_enabled():
                return False
            if not self._pipeline_bool("auto_queue", True):
                return False
            uids = self._pipeline_enabled_uids()
            if uids:
                uid = self._pipeline_post_uid(mid)
                if not uid or uid not in uids:
                    return False
            return self.pipeline_enqueue(mid)
        except Exception as exc:
            self._pipeline_last_error = str(exc)[:200]
            logger.warning(f"图床管线入队失败（mid={mid}）：{exc}")
            return False

    def _pipeline_post_uid(self, mid: str) -> str:
        store = self._pipeline_store()
        getter = getattr(store, "get_post", None)
        if not callable(getter):
            return ""
        try:
            post = getter(mid)
        except Exception:
            return ""
        return str(_row_get(post, "uid", "") or "") if post else ""

    async def _pipeline_worker(self) -> None:
        """单 worker：串行消费队列；异常指数退避（5s→60s），成功即重置。"""
        queue = self._pipeline_queue_ref()
        pending = self._pipeline_pending_ref()
        delay = _BACKOFF_MIN
        while True:
            mid = await queue.get()
            pending.discard(str(mid))
            failed = False
            try:
                result = await self.pipeline_run_post(str(mid))
                failed = isinstance(result, dict) and bool(result.get("error"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self._pipeline_last_error = str(exc)[:200]
                logger.warning(f"图床管线单帖失败（mid={mid}）：{exc}")
            finally:
                with contextlib.suppress(ValueError):
                    queue.task_done()
            if failed:
                await asyncio.sleep(delay)
                delay = min(_BACKOFF_MAX, delay * 2)
            else:
                delay = _BACKOFF_MIN

    # ---------- 单帖流程 ----------

    async def pipeline_run_post(self, mid: str) -> dict:
        """单帖管线：本地优先 → 特征+缩略图 → 图床 → 一次批量写向量 → 图库登记。"""
        mid = str(mid or "").strip()
        stats: dict[str, Any] = {
            "mid": mid,
            "mode": "cfb" if self._pipeline_cfb() else "plugin",
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "exhausted": 0,
            "uploaded": 0,
            "thumbed": 0,
            "vectored": 0,
            "errors": [],
        }
        store = self._pipeline_store()
        if store is None:
            return {"mid": mid, "error": "归档未启用"}
        try:
            rows = list(await asyncio.to_thread(store.list_media, mid) or [])
        except Exception as exc:
            self._pipeline_last_error = str(exc)[:200]
            return {"mid": mid, "error": f"读取媒体失败：{str(exc)[:160]}"}
        if not rows:
            return stats
        if stats["mode"] == "cfb":
            stats["skipped"] = len(rows)
            await self._pipeline_ensure_local(mid, store, rows)
            return stats
        services = self.pipeline_services()
        if services is None or services.bed is None or services.vector is None:
            return {"mid": mid, "error": "图床管线未就绪"}
        try:
            return await self._pipeline_run_plugin(mid, store, rows, services, stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 兜底：异常绝不逃出 pipeline_run_post
            self._pipeline_last_error = str(exc)[:200]
            stats["error"] = f"管线内部错误：{str(exc)[:160]}"
            stats["errors"].append(stats["error"])
            logger.warning(f"图床管线单帖异常（mid={mid}）：{exc}")
            return stats

    async def _pipeline_ensure_local(self, mid: str, store: Any, rows: list) -> None:
        """CFB 模式：只保证原图已落盘（最多补下载一次）。"""
        if all(self._pipeline_local_path(store, mid, row) is not None for row in rows):
            return
        download = getattr(self, "archive_download_post", None)
        if not callable(download):
            return
        try:
            await download(mid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._pipeline_last_error = str(exc)[:200]
            logger.warning(f"图床管线补原图失败（mid={mid}）：{exc}")

    async def _pipeline_run_plugin(
        self, mid: str, store: Any, rows: list, services: PipelineServices, stats: dict
    ) -> dict:
        max_attempts = max(1, self._pipeline_int("max_attempts", 3))
        limit = self._pipeline_int("images_per_post_limit", 0)
        stats["skipped"] = sum(1 for row in rows if _media_rank(row) >= _VECTOR_RANK)
        pending_rows = [row for row in rows if _media_rank(row) < _VECTOR_RANK]
        if limit > 0:
            pending_rows = pending_rows[:limit]
        if not pending_rows:
            return stats

        post = await self._pipeline_post_row(store, mid)
        account = str(_row_get(post, "author", "") or "")
        uid = str(_row_get(post, "uid", "") or "")
        config = getattr(self, "config", None)
        config = config if callable(getattr(config, "get", None)) else {}
        dir_template, file_template = naming_templates(config)
        mapping = category_map(config)
        category = resolve_category(mapping, uid=uid, account=account)
        day = _pipeline_date(_row_get(post, "created_at", 0))
        title = title_from_text(str(_row_get(post, "text", "") or ""))
        hash_size = _pipeline_int(getattr(services, "hash_size", 16), 16)
        thumb_size = max(1, self._pipeline_int("thumb_size", 720))
        thumb_quality = min(100, max(1, self._pipeline_int("thumb_quality", 72)))

        if any(self._pipeline_local_path(store, mid, row) is None for row in pending_rows):
            await self._pipeline_ensure_local(mid, store, pending_rows)
            pending_rows = await self._pipeline_reload_rows(store, mid, pending_rows)

        entries: list[Any] = []
        planned: list[dict[str, Any]] = []
        staging = self._pipeline_stage_dir(mid)
        try:
            for position, row in enumerate(pending_rows, start=1):
                pid = str(_row_get(row, "pid", ""))
                try:
                    local = self._pipeline_local_path(store, mid, row)
                    if local is None:
                        raise RuntimeError("原图缺失")
                    features, mime, thumb_bytes = await asyncio.to_thread(
                        _pipeline_compute_features,
                        str(local),
                        hash_size,
                        max_side=thumb_size,
                        quality=thumb_quality,
                    )
                    phash = str(getattr(features, "phash", "") or "")
                    if phash and services.library is not None:
                        hit = services.library.find_by_phash(phash)
                        if hit is not None and str(_row_get(hit, "image_url", "") or "").strip():
                            await asyncio.to_thread(store.mark_media, mid, pid, "vector")
                            stats["skipped"] += 1
                            continue
                    index = _pipeline_int(_row_get(row, "idx", position - 1), position - 1) + 1
                    context = NamingContext(
                        account=account,
                        category=category,
                        date=day,
                        index=index,
                        title=title,
                        ext=str(_row_get(row, "ext", "") or "jpg"),
                    )
                    directory = render_directory(dir_template, context)
                    filename = render_filename(file_template, context)
                    staged = self._pipeline_stage_original(staging, local, filename)
                    result = await services.bed.store(str(staged), folder=directory)
                    original_url = str(getattr(result, "url", "") or "")
                    if not getattr(result, "ok", False) or not original_url:
                        raise RuntimeError(str(getattr(result, "message", "") or "图床返回失败"))
                    await asyncio.to_thread(store.mark_media, mid, pid, "bed")
                    stats["uploaded"] += 1
                    thumb_path = staging / thumbnail_name(filename)
                    thumb_path.write_bytes(thumb_bytes)
                    thumb_result = await services.bed.store(
                        str(thumb_path), folder=thumbnail_folder()
                    )
                    thumb_url = str(getattr(thumb_result, "url", "") or "")
                    if not getattr(thumb_result, "ok", False):
                        raise RuntimeError(
                            str(getattr(thumb_result, "message", "") or "缩略图上传失败")
                        )
                    await asyncio.to_thread(store.mark_media, mid, pid, "thumb")
                    stats["thumbed"] += 1
                    file_size = self._pipeline_file_size(local)
                    width = _pipeline_int(getattr(features, "width", 0), 0)
                    height = _pipeline_int(getattr(features, "height", 0), 0)
                    entries.append(
                        _pipeline_point_input(
                            path=str(local),
                            point_key=f"phash:{phash}",
                            image_url=original_url,
                            thumb_url=thumb_url,
                            file_size=file_size,
                            width=width,
                            height=height,
                            mime=str(mime or ""),
                            src="weibo",
                        )
                    )
                    planned.append(
                        {
                            "pid": pid,
                            "phash": phash,
                            "dhash": str(getattr(features, "dhash", "") or ""),
                            "ahash": str(getattr(features, "ahash", "") or ""),
                            "width": width,
                            "height": height,
                            "file_size": file_size,
                            "image_url": original_url,
                            "path": str(local),
                        }
                    )
                    stats["processed"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = str(exc)[:200]
                    stats["errors"].append(f"{pid}: {error}")
                    attempts = await self._pipeline_record_failure(store, mid, pid, row, error)
                    if attempts >= max_attempts:
                        stats["exhausted"] += 1
                    else:
                        stats["failed"] += 1
            if entries:
                await self._pipeline_flush_vectors(mid, store, services, entries, planned, stats)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return stats

    async def _pipeline_flush_vectors(
        self,
        mid: str,
        store: Any,
        services: PipelineServices,
        entries: list,
        planned: list[dict[str, Any]],
        stats: dict,
    ) -> None:
        """整帖一次批量 upsert；成功后推进 vector 并登记图库。"""
        try:
            await services.vector.upsert_file_batch(entries)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"向量批量写入失败：{str(exc)[:160]}"
            self._pipeline_last_error = message
            stats["error"] = message
            stats["errors"].append(message)
            for item in planned:
                await self._pipeline_record_failure(
                    store, mid, item["pid"], {"state": "thumb"}, str(exc)
                )
                stats["failed"] += 1
            return
        for item in planned:
            pid = item["pid"]
            try:
                await asyncio.to_thread(store.mark_media, mid, pid, "vector")
            except Exception as exc:
                stats["errors"].append(f"{pid}: mark vector 失败：{str(exc)[:120]}")
                continue
            stats["vectored"] += 1
            await self._pipeline_library_add(services.library, item, mid)

    async def _pipeline_library_add(self, library: Any, item: dict, mid: str) -> None:
        if library is None:
            return
        row = {
            "phash": item["phash"],
            "dhash": item["dhash"],
            "ahash": item["ahash"],
            "width": item["width"],
            "height": item["height"],
            "file_size": item["file_size"],
            "image_url": item["image_url"],
            "file_path": item["path"],
            "source": "weibo",
            "note": mid,
        }
        try:
            await asyncio.to_thread(library.add_if_absent, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"图库登记失败（mid={mid}）：{exc}")

    async def _pipeline_record_failure(
        self, store: Any, mid: str, pid: str, row: Any, error: str
    ) -> int:
        """记一次失败：bump attempts + 原地写 error（不推进状态）。"""
        attempts = 0
        try:
            attempts = int(await asyncio.to_thread(store.bump_media_attempt, mid, pid, error) or 0)
        except Exception as exc:
            logger.warning(f"图床管线 bump attempts 失败（mid={mid}）：{exc}")
            attempts = _pipeline_int(_row_get(row, "attempts", 0), 0) + 1
        state = str(_row_get(row, "state", "pending") or "pending")
        if state not in _MEDIA_STATES:
            state = "pending"
        try:
            await asyncio.to_thread(store.mark_media, mid, pid, state, error=error)
        except Exception as exc:
            logger.warning(f"图床管线记录错误失败（mid={mid}）：{exc}")
        return attempts

    async def _pipeline_post_row(self, store: Any, mid: str) -> dict:
        getter = getattr(store, "get_post", None)
        if not callable(getter):
            return {}
        try:
            return dict(getter(mid) or {})
        except Exception:
            return {}

    async def _pipeline_reload_rows(self, store: Any, mid: str, rows: list) -> list:
        """补下载后重读 media，按 pid 对齐（保留原顺序）。"""
        try:
            fresh = list(await asyncio.to_thread(store.list_media, mid) or [])
        except Exception:
            return rows
        by_pid = {str(_row_get(row, "pid", "")): row for row in fresh}
        return [by_pid.get(str(_row_get(row, "pid", "")), row) for row in rows]

    # ---------- 本地文件 / staging ----------

    def _pipeline_local_path(self, store: Any, mid: str, row: Any) -> Path | None:
        raw = str(_row_get(row, "local_path", "") or "").strip()
        if raw:
            path = Path(raw)
            if not path.is_absolute():
                root = getattr(store, "media_root", None)
                if root:
                    path = Path(root) / path
            if path.is_file():
                return path
        getter = getattr(store, "media_file", None)
        if callable(getter):
            try:
                found = getter(mid, str(_row_get(row, "pid", "")))
            except Exception:
                found = None
            if found is not None:
                candidate = Path(found)
                if candidate.is_file():
                    return candidate
        return None

    def _pipeline_stage_dir(self, mid: str = "") -> Path:
        """单帖临时目录（staging）：优先 data_dir/pipeline_stage，失败回落系统临时目录。"""
        base = getattr(self, "_data_dir", None)
        tag = "".join(ch for ch in str(mid) if ch.isalnum())[-24:] or "post"
        if base:
            root = Path(base) / PIPELINE_STAGE_DIR_NAME
            try:
                root.mkdir(parents=True, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=str(root)))
            except OSError:
                pass
        return Path(tempfile.mkdtemp(prefix=f"{tag}-"))

    @staticmethod
    def _pipeline_stage_original(staging: Path, local: Path, filename: str) -> Path:
        """把原图放进 staging（同盘优先硬链接，跨盘回落 copy2）。"""
        dest = staging / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(local, dest)
            return dest
        except OSError:
            pass
        shutil.copy2(local, dest)
        return dest

    @staticmethod
    def _pipeline_file_size(path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    # ---------- 补跑与统计 ----------

    async def pipeline_backfill(self, limit: int = 200) -> dict:
        """把「还没跑到 vector」的帖子重新灌进队列（按 enabled_uids 过滤）。"""
        store = self._pipeline_store()
        if store is None:
            return {"scanned": 0, "queued": 0, "error": "归档未启用"}
        limit = max(1, _pipeline_int(limit, 200))
        try:
            rows = list(
                await asyncio.to_thread(
                    store.list_posts_for_pipeline, None, done_state="vector", limit=limit
                )
                or []
            )
        except Exception as exc:
            return {"scanned": 0, "queued": 0, "error": f"读取缺口帖失败：{str(exc)[:160]}"}
        allowed = self._pipeline_enabled_uids()
        scanned = 0
        queued = 0
        for row in rows:
            mid = str(_row_get(row, "mid", "") or "").strip()
            if not mid:
                continue
            scanned += 1
            if allowed and str(_row_get(row, "uid", "") or "") not in allowed:
                continue
            if self.pipeline_enqueue(mid):
                queued += 1
        return {"scanned": scanned, "queued": queued}

    def pipeline_stats(self) -> dict:
        store = self._pipeline_store()
        stats: dict[str, Any] = {
            "enabled": self.pipeline_enabled(),
            "mode": "cfb" if self._pipeline_cfb() else "plugin",
            "queue": self.pipeline_queue_size(),
            "pending": len(self._pipeline_pending_ref()),
            "posts": 0,
            "media": {},
            "last_error": str(getattr(self, "_pipeline_last_error", "") or ""),
        }
        if store is None:
            return stats
        try:
            rows = store.list_posts_for_pipeline(None, done_state="vector", limit=200)
            stats["posts"] = len(rows or [])
        except Exception as exc:
            stats["last_error"] = str(exc)[:200]
        try:
            stats["media"] = store.media_stats()
        except Exception as exc:
            stats["last_error"] = str(exc)[:200]
        return stats

    def format_pipeline_status(self) -> str:
        """给面板命令用的多行状态文本。"""
        stats = self.pipeline_stats()
        mode = "图床侧钩子（CFB）" if stats["mode"] == "cfb" else "插件写入（bed/thumb/vector）"
        lines = [
            f"图床管线：{'已启用' if stats['enabled'] else '未启用'}（{mode}）",
            f"队列：{stats['queue']} 个排队，{stats['pending']} 个待出队",
            f"缺口帖子：{stats['posts']} 篇",
        ]
        media = stats.get("media") or {}
        if media:
            counts = "，".join(f"{state} {media.get(state, 0)}" for state in _MEDIA_STATES)
            lines.append(f"媒体：共 {media.get('total', 0)} 张（{counts}）")
            if media.get("failed"):
                lines.append(f"失败待重试：{media['failed']} 张")
        lines.append(f"最近错误：{stats.get('last_error') or '无'}")
        return "\n".join(lines)
