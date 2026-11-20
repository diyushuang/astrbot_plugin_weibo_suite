"""图床反向收录巡检的启动补全、状态持久化与手动指令。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from ..shared.common import as_int, bool_or
    from .bed_audit import (
        BedAuditService,
        BedAuditStats,
        format_bed_audit,
        parse_audit_args,
    )
    from .features import compute_features_with_mime
    from .storage_utils import BedListUnsupported
except ImportError:  # 兼容插件以独立模块方式加载
    from bed_audit import (  # type: ignore[no-redef]
        BedAuditService,
        BedAuditStats,
        format_bed_audit,
        parse_audit_args,
    )
    from features import compute_features_with_mime  # type: ignore[no-redef]
    from storage_utils import BedListUnsupported  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        as_int,
        bool_or,
    )


class BedAuditBusy(RuntimeError):
    """已有巡检在跑；手动命令不应叠加第二次全量扫描。"""


class BedAuditMixin:
    """图床 → 向量库的反向补录。"""

    # ------------------------------------------------------------------
    # 配置与状态
    # ------------------------------------------------------------------

    def _bed_audit_config(self) -> tuple[bool, int]:
        section = self._dict_cfg("limits")
        enabled = bool_or(section.get("bed_audit_on_start"), True)
        limit = max(0, as_int(section.get("bed_audit_max_files"), 0))
        return enabled, limit

    def _bed_audit_signature(self) -> str:
        """图床/向量配置指纹：换图床或换集合后应重新做一次全量补录。

        只把非敏感定位字段纳入哈希；Token / 密码绝不落进状态文件。
        """
        bed = self._bed_cfg if isinstance(self._bed_cfg, dict) else {}
        payload = {
            "mode": self._bed_mode(),
            "cfi_base": str(bed.get("cfi_base_url") or "").rstrip("/"),
            "local_dir": str(bed.get("local_dir") or ""),
            "r2_bucket": str(bed.get("r2_bucket") or ""),
            "oci_bucket": str(bed.get("oci_bucket") or ""),
            "qdrant": str(getattr(self.vector, "qdrant_url", "") or "").rstrip("/"),
            "collection": str(getattr(self.vector, "collection", "") or ""),
            "embed_model": str(getattr(self.vector, "embed_model", "") or ""),
            "hash_size": int(getattr(self, "hash_size", 0) or 0),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def _bed_audit_state_path(self) -> str:
        return os.path.join(self.data_dir, "bed_audit.json")

    def _load_bed_audit_state(self) -> dict:
        path = self._bed_audit_state_path
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            logger.warning(f"图床巡检状态读取失败，将按首次启动处理: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _save_bed_audit_state_sync(self, state: dict) -> None:
        path = self._bed_audit_state_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    async def _save_bed_audit_state(self, state: dict) -> None:
        try:
            await asyncio.to_thread(self._save_bed_audit_state_sync, state)
        except Exception as exc:
            logger.warning(f"图床巡检状态写入失败: {exc}")

    def _update_bed_audit_state(self, status: str, **fields) -> dict:
        state = {
            "signature": self._bed_audit_signature(),
            "status": status,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **fields,
        }
        return state

    # ------------------------------------------------------------------
    # 启动补全
    # ------------------------------------------------------------------

    def _bed_audit_auto_should_run(self) -> bool:
        enabled, _limit = self._bed_audit_config()
        if not enabled:
            return False
        if self._bed_mode() not in ("cloudflare_imgbed", "local"):
            logger.debug(f"图床模式 {self._bed_mode()!r} 不支持枚举，跳过自动收录检查")
            return False
        if self._bed_mode() == "cloudflare_imgbed" and not (
            bool(getattr(self.vector, "qdrant_ready", False))
            and bool(getattr(self.vector, "enabled", False))
        ):
            logger.info("图床收录检查：向量库/embedding 未配置，跳过自动补录")
            return False
        state = self._load_bed_audit_state()
        if state.get("signature") != self._bed_audit_signature():
            return True
        status = str(state.get("status") or "")
        if status in ("done", "done_with_errors", "unsupported"):
            return False
        if status == "error":
            retry_at = float(state.get("next_retry_at") or 0)
            return time.time() >= retry_at
        return True

    def _maybe_start_bed_audit(self) -> None:
        """首次启动（或图床配置变更后）后台补录一次，不阻塞插件加载。"""
        if self._bed_audit_task is not None and not self._bed_audit_task.done():
            return
        if not self._bed_audit_auto_should_run():
            return
        _enabled, limit = self._bed_audit_config()
        self._bed_audit_task = asyncio.create_task(self._run_bed_audit_background(limit=limit))
        logger.info("图床收录检查：检测到首次启动/配置变更，开始在后台补录未收录微博图")

    async def _run_bed_audit_background(self, *, limit: int) -> None:
        try:
            stats = await self.run_bed_audit(
                dry_run=False,
                limit=limit,
                source="首次启动",
                mark_state=False,
            )
            status = "done_with_errors" if stats.errors else "done"
            state = self._update_bed_audit_state(
                status,
                completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                stats=stats.to_dict(),
                next_retry_at=0,
            )
            await self._save_bed_audit_state(state)
            logger.info(
                "图床收录检查完成："
                f"图床 {stats.listed} 个文件，微博图 {stats.weibo} 张，"
                f"补录向量 {stats.added_vector} 张、图库 {stats.added_library} 条，"
                f"失败 {stats.errors} 张"
            )
        except asyncio.CancelledError:
            raise
        except BedAuditBusy as exc:
            logger.debug(f"启动巡检跳过：{exc}")
        except BedListUnsupported as exc:
            state = self._update_bed_audit_state("unsupported", last_error=str(exc)[:300])
            await self._save_bed_audit_state(state)
            logger.warning(f"图床收录检查无法执行：{exc}")
        except Exception as exc:
            state = self._update_bed_audit_state(
                "error",
                last_error=str(exc)[:300],
                next_retry_at=time.time() + 3600,
            )
            await self._save_bed_audit_state(state)
            logger.warning(f"图床收录检查失败（1 小时后重试）: {exc}")

    async def _stop_bed_audit(self, errors: list) -> None:
        task = self._bed_audit_task
        self._bed_audit_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception as exc:
            errors.append(f"图床巡检任务: {exc}")

    # ------------------------------------------------------------------
    # 执行与指令
    # ------------------------------------------------------------------

    async def run_bed_audit(
        self,
        *,
        dry_run: bool = False,
        limit: int = 0,
        source: str = "手动",
        mark_state: bool = True,
    ) -> BedAuditStats:
        if self._bed_audit_lock.locked():
            raise BedAuditBusy("已有图床收录检查正在执行")
        async with self._bed_audit_lock:
            service = BedAuditService(
                bed=self.bed,
                vector=self.vector,
                library=self.library,
                download=self._download,
                compute_features=compute_features_with_mime,
                remove_file=self._remove_quiet,
                hash_size=self.hash_size,
            )
            stats = await service.run(dry_run=dry_run, limit=limit)
            if mark_state and not dry_run:
                # 只有「枚举被上限截断」才是 partial；图床文件数少于上限说明已经
                # 扫完，仍标 partial 会让每次启动都自动再跑一遍（无谓的全量列表）。
                truncated = limit > 0 and stats.listed >= limit
                status = (
                    "partial" if truncated else ("done_with_errors" if stats.errors else "done")
                )
                state = self._update_bed_audit_state(
                    status,
                    last_source=source,
                    last_run_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    stats=stats.to_dict(),
                )
                await self._save_bed_audit_state(state)
            return stats

    async def bed_audit(self, event: AstrMessageEvent, args_text: str = ""):
        """`微博 图床检查 [dry] [数量上限]`：检查并补录未收录的微博图。"""
        dry_run, limit = parse_audit_args(args_text)
        try:
            stats = await self.run_bed_audit(dry_run=dry_run, limit=limit)
        except BedAuditBusy as exc:
            yield event.plain_result(f"⏳ {exc}，请等本轮结束后再试。")
            return
        except BedListUnsupported as exc:
            yield event.plain_result(f"⛔ 当前图床无法枚举文件：{exc}")
            return
        except Exception as exc:
            logger.warning(f"图床收录检查失败: {exc}")
            yield event.plain_result(f"⚠️ 图床收录检查失败：{str(exc)[:200]}")
            return
        yield event.plain_result(
            format_bed_audit(stats, source="只查不写" if dry_run else "手动补录")
        )
